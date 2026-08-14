from __future__ import annotations

import asyncio
import time
from datetime import datetime

import aiohttp
from aiohttp_socks import ProxyConnector

from app.services.proxy_utils import classify_error
from app.services.youtube import extract_best_audio

PING_URL = "https://www.google.com/generate_204"
YOUTUBE_URL = "https://www.youtube.com"
TEST_VIDEO = "https://youtu.be/57Ykv1D0qEE"


async def _session_for(proxy_url: str) -> tuple[aiohttp.ClientSession, dict]:
    if proxy_url.startswith(("socks4://", "socks5://")):
        connector = ProxyConnector.from_url(proxy_url)
        return aiohttp.ClientSession(connector=connector), {}
    return aiohttp.ClientSession(), {"proxy": proxy_url}


async def _http_get(proxy_url: str, url: str, timeout: int) -> tuple[bool, int, str]:
    started = time.perf_counter()
    try:
        session, kwargs = await _session_for(proxy_url)
        async with session:
            async with session.get(url, timeout=timeout, ssl=False, **kwargs) as response:
                latency = int((time.perf_counter() - started) * 1000)
                return 200 <= response.status < 400, latency, f"HTTP {response.status}"
    except Exception as error:
        latency = int((time.perf_counter() - started) * 1000)
        return False, latency, str(error)


async def _audio_probe(proxy_url: str) -> tuple[bool, int, str]:
    """Verify both extraction and a real audio-byte fetch through the proxy."""
    started = time.perf_counter()
    try:
        loop = asyncio.get_running_loop()
        metadata = await loop.run_in_executor(None, lambda: extract_best_audio(TEST_VIDEO, proxy_url))
        stream_url = str(metadata.get("stream_url") or "")
        if not stream_url:
            return False, int((time.perf_counter() - started) * 1000), "No audio URL resolved"
        session, kwargs = await _session_for(proxy_url)
        async with session:
            async with session.get(
                stream_url,
                headers={"Range": "bytes=0-1023", "Accept": "audio/*,*/*;q=0.8"},
                timeout=20,
                ssl=False,
                **kwargs,
            ) as response:
                chunk = await response.content.read(1024)
                return response.status in {200, 206} and bool(chunk), int((time.perf_counter() - started) * 1000), f"HTTP {response.status}"
    except Exception as error:
        return False, int((time.perf_counter() - started) * 1000), str(error)


async def check_proxy(proxy_url: str) -> dict:
    ping_ok, ping_ms, ping_error = await _http_get(proxy_url, PING_URL, 8)
    if not ping_ok:
        return {
            "proxy_url": proxy_url,
            "status": "dead",
            "layer": "ping",
            "latency_ms": ping_ms,
            "error": ping_error,
            "checked_at": datetime.utcnow(),
        }

    youtube_ok, youtube_ms, youtube_error = await _http_get(proxy_url, YOUTUBE_URL, 12)
    if not youtube_ok:
        return {
            "proxy_url": proxy_url,
            "status": "youtube_unreachable",
            "layer": "youtube",
            "latency_ms": youtube_ms,
            "error": youtube_error,
            "checked_at": datetime.utcnow(),
        }

    audio_ok, audio_ms, audio_error = await _audio_probe(proxy_url)
    if audio_ok:
        return {
            "proxy_url": proxy_url,
            "status": "verified",
            "layer": "audio-byte",
            "latency_ms": max(ping_ms, youtube_ms, audio_ms),
            "download_ms": audio_ms,
            "error": "",
            "checked_at": datetime.utcnow(),
        }
    kind = classify_error(Exception(audio_error))
    status = "youtube_blocked" if kind in {"youtube_rate_limit", "youtube_bot", "captcha"} else "timeout" if kind == "timeout" else "dead"
    return {
        "proxy_url": proxy_url,
        "status": status,
        "layer": "audio-byte",
        "latency_ms": max(ping_ms, youtube_ms, audio_ms),
        "error": audio_error,
        "checked_at": datetime.utcnow(),
    }


async def check_proxy_fast(proxy_url: str, url: str = PING_URL, timeout: int = 5) -> dict:
    ok, latency_ms, error = await _http_get(proxy_url, url, timeout)
    return {
        "proxy_url": proxy_url,
        "status": "verified" if ok else "dead",
        "layer": "fast-ping",
        "latency_ms": latency_ms,
        "error": "" if ok else error,
        "checked_at": datetime.utcnow(),
    }
