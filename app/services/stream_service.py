from __future__ import annotations

import time
import asyncio
import logging
import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Proxy, StreamCache
from app.services.limits import stream_resolve_semaphore
from app.services.proxy_store import apply_check_result, best_proxies
from app.services.proxy_utils import classify_error
from app.services.track_metadata import lookup_track_metadata
from app.services.youtube import extract_best_audio, extract_video_id


settings = get_settings()
logger = logging.getLogger("producerscenter.stream")


def _stream_cache_key(url: str) -> str:
    return extract_video_id(url) or url.strip()


def _cached_stream(db: Session, cache_key: str | None) -> StreamCache | None:
    if not cache_key:
        return None
    rows = (
        db.query(StreamCache)
        .filter(StreamCache.video_id == cache_key)
        .filter(StreamCache.expires_at > datetime.utcnow())
        .order_by(StreamCache.created_at.desc())
        .all()
    )
    for row in rows:
        if _can_reuse_cached_stream(db, row):
            return row
    return None


def _can_reuse_cached_stream(db: Session, row: StreamCache) -> bool:
    now = datetime.utcnow()
    signed_expiry = _signed_stream_expiry(row.stream_url)
    if signed_expiry and signed_expiry <= now + timedelta(seconds=settings.stream_cache_expiry_safety_seconds):
        logger.info("stream cache skipped video_id=%s reason=signed_url_expiring", row.video_id)
        return False

    proxy_url = (row.proxy_used or "").strip()
    if not proxy_url:
        if not settings.stream_cache_reuse_direct:
            logger.info("stream cache skipped video_id=%s reason=direct_egress_mismatch", row.video_id)
            return False
        return True

    proxy = db.query(Proxy).filter(Proxy.proxy_url == proxy_url).first()
    if proxy is None or not proxy.is_active or not proxy.is_verified:
        logger.info("stream cache skipped video_id=%s reason=proxy_not_verified", row.video_id)
        return False
    if proxy.cooldown_until and proxy.cooldown_until > now:
        logger.info("stream cache skipped video_id=%s reason=proxy_in_cooldown", row.video_id)
        return False
    last_good_at = proxy.last_success_at or proxy.last_checked_at
    if last_good_at is None or last_good_at < now - timedelta(seconds=settings.proxy_cache_health_seconds):
        logger.info("stream cache skipped video_id=%s reason=proxy_health_stale", row.video_id)
        return False
    return True


def _signed_stream_expiry(stream_url: str) -> datetime | None:
    """Return Googlevideo's signed URL deadline when it is present and valid."""
    try:
        raw_expiry = parse_qs(urlparse(stream_url).query).get("expire", [""])[0]
        expiry = int(raw_expiry)
        if expiry <= 0:
            return None
        return datetime.utcfromtimestamp(expiry)
    except (TypeError, ValueError, OverflowError):
        return None


def _cache_result(db: Session, youtube_url: str, result: dict, proxy_used: str = "") -> StreamCache | None:
    # Do not retain direct Googlevideo links unless an operator explicitly opts
    # in. A later PWA request originates from a different egress IP, so this
    # cache is more likely to create a failed playback than save useful work.
    if not proxy_used and not settings.stream_cache_reuse_direct:
        return None
    now = datetime.utcnow()
    cache_deadline = now + timedelta(hours=min(settings.stream_cache_hours, settings.stream_cache_max_hours))
    signed_expiry = _signed_stream_expiry(result["stream_url"])
    if signed_expiry:
        cache_deadline = min(
            cache_deadline,
            signed_expiry - timedelta(seconds=settings.stream_cache_expiry_safety_seconds),
        )
    # Do not create an immediately expired row when the resolver returned an
    # almost-expired URL. The next request must perform a fresh extraction.
    if cache_deadline <= now:
        cache_deadline = now
    row = StreamCache(
        video_id=result.get("video_id") or _stream_cache_key(youtube_url),
        youtube_url=youtube_url,
        title=result.get("title") or "",
        uploader=result.get("uploader") or "",
        artist=result.get("artist") or result.get("uploader") or "",
        artists_json=json.dumps(result.get("artists") or [], ensure_ascii=False, separators=(",", ":")),
        album=result.get("album") or "",
        track=result.get("track") or "",
        release_year=result.get("release_year") or 0,
        duration=result.get("duration") or 0,
        thumbnail=result.get("thumbnail") or "",
        stream_url=result["stream_url"],
        format_id=result.get("format_id") or "",
        audio_codec=result.get("audio_codec") or "",
        ext=result.get("ext") or "",
        bitrate=result.get("bitrate") or 0,
        sample_rate=result.get("sample_rate") or 0,
        filesize=result.get("filesize") or 0,
        proxy_used=proxy_used,
        expires_at=cache_deadline,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _response_from_cache(row: StreamCache) -> dict:
    artists = []
    try:
        parsed_artists = json.loads(row.artists_json or "[]")
        if isinstance(parsed_artists, list):
            artists = parsed_artists
    except json.JSONDecodeError:
        artists = []
    return _enrich_stream_response(None, {
        "cached": True,
        "video_id": row.video_id,
        "url": row.youtube_url,
        "title": row.title,
        "uploader": row.uploader,
        "artist": row.artist or row.uploader,
        "artists": artists,
        "album": row.album or None,
        "track": row.track or None,
        "release_year": row.release_year or None,
        "duration": row.duration,
        "thumbnail": row.thumbnail,
        "stream_url": row.stream_url,
        "format_id": row.format_id,
        "audio_codec": row.audio_codec,
        "ext": row.ext,
        "bitrate": row.bitrate,
        "sample_rate": row.sample_rate,
        "filesize": row.filesize,
        "proxy_used": row.proxy_used,
    })


def _response_from_result(result: dict, cached: bool, proxy_used: str = "") -> dict:
    return _enrich_stream_response(None, {
        "cached": cached,
        "video_id": result.get("video_id"),
        "url": result.get("url"),
        "title": result.get("title"),
        "uploader": result.get("uploader"),
        "artist": result.get("artist"),
        "artists": result.get("artists"),
        "album": result.get("album"),
        "track": result.get("track"),
        "release_year": result.get("release_year"),
        "duration": result.get("duration"),
        "thumbnail": result.get("thumbnail"),
        "stream_url": result["stream_url"],
        "format_id": result.get("format_id"),
        "audio_codec": result.get("audio_codec"),
        "ext": result.get("ext"),
        "bitrate": result.get("bitrate"),
        "sample_rate": result.get("sample_rate"),
        "filesize": result.get("filesize"),
        "proxy_used": proxy_used,
    })


def _enrich_stream_response(db: Session | None, response: dict) -> dict:
    if db is None:
        return response
    provider = "soundcloud" if "soundcloud.com" in str(response.get("url") or response.get("youtube_url") or "").lower() else "youtube"
    media_id = str(response.get("video_id") or "").strip()
    if not media_id:
        return response
    lookup = lookup_track_metadata(db, {
        "provider": provider,
        "providerMediaId": media_id,
        "title": response.get("title"),
        "artist": response.get("uploader"),
        "duration": response.get("duration"),
    })
    metadata = lookup.get("metadata") if lookup.get("matched") else None
    if not isinstance(metadata, dict):
        return response
    for source_key, target_key in (
        ("artist", "artist"),
        ("artists", "artists"),
        ("album", "album"),
        ("year", "year"),
        ("releaseYear", "release_year"),
        ("release_year", "release_year"),
        ("genre", "genre"),
        ("bpm", "bpm"),
        ("key", "key"),
        ("lufs", "lufs"),
        ("sampleRate", "sample_rate"),
        ("bitrate", "bitrate"),
        ("fingerprintHash", "fingerprint_hash"),
        ("fingerprintVersion", "fingerprint_version"),
        ("chromaprintFingerprint", "chromaprint_fingerprint"),
        ("metadataSource", "metadata_source"),
        ("metadataConfidence", "metadata_confidence"),
    ):
        value = metadata.get(source_key)
        if value not in (None, ""):
            response[target_key] = value
    return response


async def resolve_stream(
    db: Session,
    youtube_url: str,
    use_proxy: bool = True,
    force_refresh: bool = False,
    client_ip: str | None = None,
    timeout_seconds: int | None = None,
    proxy_attempts: int | None = None,
    prefer_proxy: bool = False,
    allow_direct_fallback: bool = True,
) -> dict:
    async with stream_resolve_semaphore:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _resolve_stream_locked,
                db,
                youtube_url,
                use_proxy,
                force_refresh,
                client_ip,
                proxy_attempts,
                prefer_proxy,
                allow_direct_fallback,
            ),
            timeout=timeout_seconds or settings.stream_resolve_timeout_seconds,
        )


def _resolve_stream_locked(
    db: Session,
    youtube_url: str,
    use_proxy: bool = True,
    force_refresh: bool = False,
    client_ip: str | None = None,
    proxy_attempts: int | None = None,
    prefer_proxy: bool = False,
    allow_direct_fallback: bool = True,
) -> dict:
    video_id = _stream_cache_key(youtube_url)
    effective_proxy_attempts = max(1, proxy_attempts or settings.proxy_attempts)
    started_total = time.perf_counter()
    logger.info(
        "stream resolve start video_id=%s use_proxy=%s force_refresh=%s proxy_attempts=%s",
        video_id or "-",
        use_proxy,
        force_refresh,
        effective_proxy_attempts,
    )
    if not force_refresh:
        cached = _cached_stream(db, video_id)
        if cached:
            logger.info("stream resolve cache hit video_id=%s proxy_used=%s", video_id or "-", bool(cached.proxy_used))
            return _enrich_stream_response(db, _response_from_cache(cached))

    errors: list[str] = []

    if not use_proxy or (settings.direct_first and not prefer_proxy):
        try:
            logger.info("stream resolve direct attempt video_id=%s", video_id or "-")
            result = extract_best_audio(youtube_url, client_ip=client_ip)
            _cache_result(db, youtube_url, result, "")
            result_response = _response_from_result(result, cached=False, proxy_used="")
            result_response["url"] = youtube_url
            logger.info(
                "stream resolve direct success video_id=%s elapsed_ms=%s",
                video_id or "-",
                int((time.perf_counter() - started_total) * 1000),
            )
            return _enrich_stream_response(db, result_response)
        except Exception as error:
            errors.append(f"direct:{classify_error(error)}:{error}")
            logger.warning("stream resolve direct failed video_id=%s kind=%s error=%s", video_id or "-", classify_error(error), error)
            if not use_proxy:
                raise

    if use_proxy:
        proxies = best_proxies(db, effective_proxy_attempts)
        logger.info("stream resolve proxy candidates video_id=%s count=%s", video_id or "-", len(proxies))
        for proxy in proxies:
            try:
                started = time.perf_counter()
                logger.info("stream resolve proxy attempt video_id=%s proxy=%s", video_id or "-", proxy.proxy_url)
                result = extract_best_audio(youtube_url, proxy.proxy_url, client_ip=client_ip)
                resolve_ms = int((time.perf_counter() - started) * 1000)
                apply_check_result(
                    db,
                    proxy,
                    {
                        "status": "verified",
                        "latency_ms": resolve_ms,
                        "download_ms": resolve_ms,
                        "error": "",
                    },
                )
                _cache_result(db, youtube_url, result, proxy.proxy_url)
                result_response = _response_from_result(result, cached=False, proxy_used=proxy.proxy_url)
                result_response["url"] = youtube_url
                logger.info(
                    "stream resolve proxy success video_id=%s proxy=%s elapsed_ms=%s",
                    video_id or "-",
                    proxy.proxy_url,
                    resolve_ms,
                )
                return _enrich_stream_response(db, result_response)
            except Exception as error:
                errors.append(f"{proxy.proxy_url}:{classify_error(error)}:{error}")
                logger.warning(
                    "stream resolve proxy failed video_id=%s proxy=%s kind=%s error=%s",
                    video_id or "-",
                    proxy.proxy_url,
                    classify_error(error),
                    error,
                )
                apply_check_result(
                    db,
                    proxy,
                    {
                        "status": "youtube_blocked" if classify_error(error) in {"youtube_bot", "youtube_rate_limit", "captcha"} else "dead",
                        "latency_ms": proxy.latency_ms,
                        "error": str(error),
                    },
                )

    if use_proxy and ((not settings.direct_first and not prefer_proxy) or (prefer_proxy and allow_direct_fallback)):
        try:
            logger.info("stream resolve fallback direct attempt video_id=%s", video_id or "-")
            result = extract_best_audio(youtube_url, client_ip=client_ip)
            _cache_result(db, youtube_url, result, "")
            result_response = _response_from_result(result, cached=False, proxy_used="")
            result_response["url"] = youtube_url
            logger.info(
                "stream resolve fallback direct success video_id=%s elapsed_ms=%s",
                video_id or "-",
                int((time.perf_counter() - started_total) * 1000),
            )
            return _enrich_stream_response(db, result_response)
        except Exception as error:
            errors.append(f"direct:{classify_error(error)}:{error}")
            logger.warning("stream resolve fallback direct failed video_id=%s kind=%s error=%s", video_id or "-", classify_error(error), error)

    logger.error("stream resolve failed video_id=%s elapsed_ms=%s errors=%s", video_id or "-", int((time.perf_counter() - started_total) * 1000), " | ".join(errors[-5:]))
    raise RuntimeError("No YouTube stream resolved. " + " | ".join(errors[-5:]))
