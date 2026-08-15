from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.streams import client_session_for_proxy, stream_fetch_headers
from app.config import get_settings
from app.database import get_db
from app.services.stream_service import resolve_stream


router = APIRouter()
settings = get_settings()
PlaytestMode = Literal["direct", "proxy", "auto"]


def _resolve_options(mode: PlaytestMode) -> dict[str, object]:
    if mode == "direct":
        return {"use_proxy": False, "prefer_proxy": False, "allow_direct_fallback": False}
    if mode == "proxy":
        return {"use_proxy": True, "prefer_proxy": True, "allow_direct_fallback": False}
    return {"use_proxy": True, "prefer_proxy": False, "allow_direct_fallback": True}


async def _resolve_for_playtest(db: Session, url: str, mode: PlaytestMode, force_refresh: bool) -> tuple[dict, int]:
    started = time.perf_counter()
    result = await resolve_stream(
        db,
        url,
        force_refresh=force_refresh,
        timeout_seconds=settings.stream_resolve_timeout_seconds,
        proxy_attempts=settings.proxy_attempts,
        **_resolve_options(mode),
    )
    return result, round((time.perf_counter() - started) * 1000)


def _result_payload(result: dict, *, mode: PlaytestMode, elapsed_ms: int) -> dict:
    return {
        "ok": True,
        "mode": mode,
        "elapsedMs": elapsed_ms,
        "cached": bool(result.get("cached")),
        "transport": "proxy" if result.get("proxy_used") else "direct",
        "streamUrl": result.get("stream_url"),
        "title": result.get("title"),
        "duration": result.get("duration"),
        "ext": result.get("ext"),
        "formatId": result.get("format_id"),
        "size": result.get("filesize"),
        # Never reveal credentials that might be embedded in a proxy URL.
        "proxyUsed": bool(result.get("proxy_used")),
    }


@router.get("/api/playtest/resolve")
async def playtest_resolve(
    url: str = Query(..., min_length=8),
    mode: PlaytestMode = Query("auto"),
    force_refresh: bool = True,
    db: Session = Depends(get_db),
):
    """Resolve one URL using an explicit route without fetching audio bytes."""
    try:
        result, elapsed_ms = await _resolve_for_playtest(db, url, mode, force_refresh)
        return _result_payload(result, mode=mode, elapsed_ms=elapsed_ms)
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail=f"{mode} resolve timed out") from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/api/playtest/probe")
async def playtest_probe(
    request: Request,
    url: str = Query(..., min_length=8),
    mode: PlaytestMode = Query("auto"),
    force_refresh: bool = True,
    bytes_to_read: int = Query(65_536, ge=1_024, le=262_144),
    db: Session = Depends(get_db),
):
    """Resolve and read a small range through the selected executor route."""
    try:
        result, resolve_ms = await _resolve_for_playtest(db, url, mode, force_refresh)
        stream_url = str(result.get("stream_url") or "")
        if not stream_url:
            raise RuntimeError("Resolver returned no stream URL")
        started = time.perf_counter()
        session, request_kwargs = client_session_for_proxy(result.get("proxy_used"), playback=True)
        try:
            headers = stream_fetch_headers(f"bytes=0-{bytes_to_read - 1}")
            response = await session.get(stream_url, headers=headers, **request_kwargs)
            response.raise_for_status()
            payload = await response.content.read(bytes_to_read)
            status = response.status
            content_type = response.headers.get("Content-Type")
            content_range = response.headers.get("Content-Range")
        finally:
            if "response" in locals():
                response.close()
            await session.close()
        probe_ms = round((time.perf_counter() - started) * 1000)
        return {
            **_result_payload(result, mode=mode, elapsed_ms=resolve_ms),
            "probe": {
                "ok": True,
                "status": status,
                "bytesRead": len(payload),
                "elapsedMs": probe_ms,
                "contentType": content_type,
                "contentRange": content_range,
                "requestId": request.headers.get("cf-ray"),
            },
        }
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail=f"{mode} probe timed out") from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
