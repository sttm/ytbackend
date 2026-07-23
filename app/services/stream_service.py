from __future__ import annotations

import time
import asyncio
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import StreamCache
from app.services.limits import stream_resolve_semaphore
from app.services.proxy_store import apply_check_result, best_proxies
from app.services.proxy_utils import classify_error
from app.services.track_metadata import lookup_track_metadata
from app.services.youtube import extract_best_audio, extract_video_id


settings = get_settings()


def _cached_stream(db: Session, video_id: str | None) -> StreamCache | None:
    if not video_id:
        return None
    return (
        db.query(StreamCache)
        .filter(StreamCache.video_id == video_id)
        .filter(StreamCache.expires_at > datetime.utcnow())
        .order_by(StreamCache.created_at.desc())
        .first()
    )


def _cache_result(db: Session, youtube_url: str, result: dict, proxy_used: str = "") -> StreamCache:
    row = StreamCache(
        video_id=result.get("video_id") or extract_video_id(youtube_url) or "",
        youtube_url=youtube_url,
        title=result.get("title") or "",
        uploader=result.get("uploader") or "",
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
        expires_at=datetime.utcnow() + timedelta(hours=settings.stream_cache_hours),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _response_from_cache(row: StreamCache) -> dict:
    return _enrich_stream_response(None, {
        "cached": True,
        "video_id": row.video_id,
        "url": row.youtube_url,
        "title": row.title,
        "uploader": row.uploader,
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


async def resolve_stream(db: Session, youtube_url: str, use_proxy: bool = True, force_refresh: bool = False) -> dict:
    async with stream_resolve_semaphore:
        return await asyncio.wait_for(
            asyncio.to_thread(_resolve_stream_locked, db, youtube_url, use_proxy, force_refresh),
            timeout=settings.stream_resolve_timeout_seconds,
        )


def _resolve_stream_locked(db: Session, youtube_url: str, use_proxy: bool = True, force_refresh: bool = False) -> dict:
    video_id = extract_video_id(youtube_url)
    if not force_refresh:
        cached = _cached_stream(db, video_id)
        if cached:
            return _enrich_stream_response(db, _response_from_cache(cached))

    errors: list[str] = []

    if settings.direct_first or not use_proxy:
        try:
            result = extract_best_audio(youtube_url)
            _cache_result(db, youtube_url, result, "")
            result_response = _response_from_result(result, cached=False, proxy_used="")
            result_response["url"] = youtube_url
            return _enrich_stream_response(db, result_response)
        except Exception as error:
            errors.append(f"direct:{classify_error(error)}:{error}")
            if not use_proxy:
                raise

    for proxy in best_proxies(db, settings.proxy_attempts):
        try:
            started = time.perf_counter()
            result = extract_best_audio(youtube_url, proxy.proxy_url)
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
            return _enrich_stream_response(db, result_response)
        except Exception as error:
            errors.append(f"{proxy.proxy_url}:{classify_error(error)}:{error}")
            apply_check_result(
                db,
                proxy,
                {
                    "status": "youtube_blocked" if classify_error(error) in {"youtube_bot", "youtube_rate_limit", "captcha"} else "dead",
                    "latency_ms": proxy.latency_ms,
                    "error": str(error),
                },
            )

    raise RuntimeError("No YouTube stream resolved. " + " | ".join(errors[-5:]))
