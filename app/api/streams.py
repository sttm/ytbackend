import ssl
import asyncio
import time

import aiohttp
import certifi
from aiohttp_socks import ProxyConnector
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Proxy
from app.schemas import YoutubePlaylistRequest, YoutubeSearchRequest, YoutubeUrlRequest
from app.config import get_settings
from app.services.proxy_store import apply_check_result
from app.services.proxy_utils import classify_error
from app.services.stream_service import resolve_stream
from app.services.search_cache import cached_search_items, store_search_items
from app.services.track_metadata import enrich_items_with_cached_metadata
from app.services.youtube import extract_playlist_items, extract_playlist_metadata, search_media

router = APIRouter()
settings = get_settings()


def stream_fetch_headers(range_header: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 ProducersCenter/1.0",
        "Accept": "audio/*,*/*;q=0.8",
    }
    if range_header:
        headers["Range"] = range_header
    return headers


def client_session_for_proxy(proxy_url: str | None) -> tuple[aiohttp.ClientSession, dict[str, str]]:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
    normalized_proxy = (proxy_url or "").strip()
    request_kwargs: dict[str, str] = {}
    ssl_context = ssl.create_default_context(cafile=certifi.where())

    if normalized_proxy.startswith(("socks4://", "socks5://", "socks5h://")):
        connector = ProxyConnector.from_url(normalized_proxy, ssl=ssl_context)
        return aiohttp.ClientSession(timeout=timeout, connector=connector), request_kwargs

    connector = aiohttp.TCPConnector(ssl=ssl_context)
    if normalized_proxy.startswith(("http://", "https://")):
        request_kwargs["proxy"] = normalized_proxy
    return aiohttp.ClientSession(timeout=timeout, connector=connector), request_kwargs


def mark_proxy_media_failure(db: Session, proxy_url: str | None, error: Exception) -> None:
    normalized_proxy = (proxy_url or "").strip()
    if not normalized_proxy:
        return
    proxy = db.query(Proxy).filter(Proxy.proxy_url == normalized_proxy).first()
    if not proxy:
        return
    kind = classify_error(error)
    text = str(error)
    if kind == "timeout":
        status = "timeout"
    elif kind in {"youtube_bot", "youtube_rate_limit", "captcha"} or "403" in text or "forbidden" in text.lower():
        status = "youtube_blocked"
    else:
        status = "dead"
    apply_check_result(
        db,
        proxy,
        {
            "status": status,
            "latency_ms": proxy.latency_ms,
            "error": f"media fetch failed: {text}",
        },
    )


def mark_proxy_media_success(db: Session, proxy_url: str | None, elapsed_ms: int) -> None:
    normalized_proxy = (proxy_url or "").strip()
    if not normalized_proxy:
        return
    proxy = db.query(Proxy).filter(Proxy.proxy_url == normalized_proxy).first()
    if not proxy:
        return
    apply_check_result(
        db,
        proxy,
        {
            "status": "verified",
            "latency_ms": proxy.latency_ms,
            "download_ms": elapsed_ms,
            "error": "",
        },
    )


@router.get("/api/stream")
async def stream(
    url: str = Query(...),
    use_proxy: bool = True,
    force_refresh: bool = False,
    db: Session = Depends(get_db),
):
    try:
        return {
            "status": "success",
            **await resolve_stream(db, url, use_proxy=use_proxy, force_refresh=force_refresh),
        }
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail="Stream resolve timed out. Try again or refresh the proxy pool.") from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/streams/resolve")
async def resolve_stream_post(payload: YoutubeUrlRequest, db: Session = Depends(get_db)):
    try:
        return {
            "status": "success",
            **await resolve_stream(
                db,
                payload.url,
                use_proxy=payload.use_proxy,
                force_refresh=payload.force_refresh,
            ),
        }
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail="Stream resolve timed out. Try again or refresh the proxy pool.") from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/stream")
async def stream_compat(payload: YoutubeUrlRequest, db: Session = Depends(get_db)):
    return await resolve_stream_post(payload, db)


@router.post("/search")
async def search(payload: YoutubeSearchRequest, db: Session = Depends(get_db)):
    try:
        mode = payload.mode
        if payload.source == "soundcloud":
            mode = "soundcloud"
        elif payload.filter == "music":
            mode = "youtube-music"
        effective_limit = min(40, max(payload.limit, 15 if mode == "youtube-music" else 10))
        cached_items = cached_search_items(db, payload.query, mode, effective_limit)
        if cached_items is not None and not _is_stale_search_cache(mode, cached_items, effective_limit):
            return {"items": enrich_items_with_cached_metadata(db, cached_items), "cached": True}

        items = await asyncio.wait_for(
            asyncio.to_thread(search_media, payload.query, effective_limit, mode),
            timeout=settings.search_timeout_seconds,
        )
        store_search_items(db, payload.query, mode, items)
        return {"items": enrich_items_with_cached_metadata(db, items), "cached": False}
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail="Search timed out. Try a narrower query.") from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


def _is_stale_search_cache(mode: str, items: list[dict], effective_limit: int) -> bool:
    expected_min = 30 if mode != "youtube-music" else min(effective_limit, 30)
    if len(items) < expected_min:
        return True
    if mode != "youtube-music":
        return False
    has_container = any(item.get("kind") in {"album", "playlist"} for item in items)
    return not has_container


@router.post("/playlist")
async def playlist(payload: YoutubePlaylistRequest):
    try:
        items = await asyncio.wait_for(
            asyncio.to_thread(extract_playlist_items, payload.url, payload.limit),
            timeout=settings.search_timeout_seconds,
        )
        return {"items": items}
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail="Playlist extraction timed out.") from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/playlist-info")
async def playlist_info(payload: YoutubePlaylistRequest):
    try:
        metadata = await asyncio.wait_for(
            asyncio.to_thread(extract_playlist_metadata, payload.url),
            timeout=settings.search_timeout_seconds,
        )
        return {"item": metadata}
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail="Playlist metadata extraction timed out.") from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/streams/download")
async def download_stream_post(payload: YoutubeUrlRequest, db: Session = Depends(get_db)):
    return await download(payload, db)


@router.get("/api/playback")
async def playback(
    request: Request,
    url: str = Query(...),
    use_proxy: bool = True,
    force_refresh: bool = False,
    db: Session = Depends(get_db),
):
    try:
        metadata = await resolve_stream(db, url, use_proxy=use_proxy, force_refresh=force_refresh)
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail="Playback stream resolve timed out. Try again or refresh the proxy pool.") from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    stream_url = metadata.get("stream_url")
    if not stream_url:
        raise HTTPException(status_code=502, detail="stream URL was not resolved")

    range_header = request.headers.get("range")

    upstream_headers = stream_fetch_headers(range_header)
    response = None
    session = None
    last_error: Exception | None = None
    for attempt in range(2):
        session, request_kwargs = client_session_for_proxy(metadata.get("proxy_used"))
        try:
            started = time.perf_counter()
            response = await session.get(stream_url, headers=upstream_headers, **request_kwargs)
            response.raise_for_status()
            mark_proxy_media_success(db, metadata.get("proxy_used"), int((time.perf_counter() - started) * 1000))
            break
        except Exception as error:
            last_error = error
            await session.close()
            mark_proxy_media_failure(db, metadata.get("proxy_used"), error)
            if attempt == 0 and use_proxy:
                try:
                    metadata = await resolve_stream(db, url, use_proxy=True, force_refresh=True)
                    stream_url = metadata.get("stream_url")
                    if stream_url:
                        continue
                except Exception as retry_error:
                    last_error = retry_error
            response = None
            session = None
            break
    if response is None or session is None:
        raise HTTPException(status_code=502, detail=f"audio playback upstream failed: {last_error}") from last_error

    ext = metadata.get("ext") or "m4a"
    media_type = "audio/webm" if ext == "webm" else "audio/mpeg" if ext == "mp3" else "audio/mp4"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "X-Track-Id": str(metadata.get("video_id") or ""),
        "X-Track-Title": str(metadata.get("title") or "online-audio"),
        "X-Track-Artist": str(metadata.get("uploader") or ""),
        "X-File-Ext": str(ext),
    }
    for header_name in ("Content-Length", "Content-Range", "Accept-Ranges"):
        value = response.headers.get(header_name)
        if value:
            headers[header_name] = value

    async def chunks():
        try:
            async for chunk in response.content.iter_chunked(1024 * 256):
                yield chunk
        finally:
            response.close()
            await session.close()

    return StreamingResponse(chunks(), media_type=media_type, headers=headers, status_code=response.status)


@router.get("/playback")
async def playback_compat(
    request: Request,
    url: str = Query(...),
    use_proxy: bool = True,
    force_refresh: bool = False,
    db: Session = Depends(get_db),
):
    return await playback(request, url, use_proxy, force_refresh, db)


@router.post("/download")
async def download(payload: YoutubeUrlRequest, db: Session = Depends(get_db)):
    if payload.url:
        try:
            metadata = await resolve_stream(
                db,
                payload.url,
                use_proxy=payload.use_proxy,
                force_refresh=payload.force_refresh,
            )
        except TimeoutError as error:
            raise HTTPException(status_code=504, detail="Download stream resolve timed out. Try again or refresh the proxy pool.") from error
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
    elif payload.stream_url:
        metadata = {
            "stream_url": payload.stream_url,
            "video_id": payload.video_id or "",
            "title": payload.title or "online-audio",
            "uploader": payload.artist or "",
            "ext": payload.ext or "m4a",
            "filesize": payload.filesize or 0,
            "proxy_used": "",
        }
    else:
        raise HTTPException(status_code=400, detail="url or stream_url is required")

    stream_url = metadata.get("stream_url")
    if not stream_url:
        raise HTTPException(status_code=502, detail="stream URL was not resolved")

    upstream_headers = stream_fetch_headers()
    response = None
    session = None
    last_error: Exception | None = None
    for attempt in range(2):
        session, request_kwargs = client_session_for_proxy(metadata.get("proxy_used"))
        try:
            started = time.perf_counter()
            response = await session.get(stream_url, headers=upstream_headers, **request_kwargs)
            response.raise_for_status()
            mark_proxy_media_success(db, metadata.get("proxy_used"), int((time.perf_counter() - started) * 1000))
            break
        except Exception as error:
            last_error = error
            await session.close()
            mark_proxy_media_failure(db, metadata.get("proxy_used"), error)
            if attempt == 0 and payload.url and payload.use_proxy:
                try:
                    metadata = await resolve_stream(db, payload.url, use_proxy=True, force_refresh=True)
                    stream_url = metadata.get("stream_url")
                    if stream_url:
                        continue
                except Exception as retry_error:
                    last_error = retry_error
            response = None
            session = None
            break
    if response is None or session is None:
        raise HTTPException(status_code=502, detail=f"audio download upstream failed: {last_error}") from last_error

    ext = metadata.get("ext") or "m4a"
    media_type = "audio/webm" if ext == "webm" else "audio/mpeg" if ext == "mp3" else "audio/mp4"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "X-Track-Id": str(metadata.get("video_id") or ""),
        "X-Track-Title": str(metadata.get("title") or "youtube-audio"),
        "X-Track-Artist": str(metadata.get("uploader") or ""),
        "X-File-Ext": str(ext),
    }
    content_length = response.headers.get("Content-Length") or str(metadata.get("filesize") or "")
    if content_length:
        headers["Content-Length"] = content_length
    for header_name in ("Content-Range", "Accept-Ranges"):
        value = response.headers.get(header_name)
        if value:
            headers[header_name] = value

    async def chunks():
        try:
            async for chunk in response.content.iter_chunked(1024 * 512):
                yield chunk
        finally:
            response.close()
            await session.close()

    return StreamingResponse(chunks(), media_type=media_type, headers=headers, status_code=response.status)
