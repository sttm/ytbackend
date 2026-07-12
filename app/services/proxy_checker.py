from __future__ import annotations

import asyncio
import time
from datetime import datetime

import aiohttp
import yt_dlp
from aiohttp_socks import ProxyConnector

from app.services.proxy_utils import classify_error

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


def _run_ytdlp_probe(proxy_url: str) -> tuple[str, str]:
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "proxy": proxy_url,
            "socket_timeout": 20,
            "extract_flat": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(TEST_VIDEO, download=False)
        return "verified", ""
    except Exception as error:
        kind = classify_error(error)
        if kind in {"youtube_rate_limit", "youtube_bot", "captcha"}:
            return "youtube_blocked", str(error)
        if kind == "timeout":
            return "timeout", str(error)
        return "dead", str(error)


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

    loop = asyncio.get_running_loop()
    status, error = await loop.run_in_executor(None, _run_ytdlp_probe, proxy_url)
    return {
        "proxy_url": proxy_url,
        "status": status,
        "layer": "yt-dlp",
        "latency_ms": max(ping_ms, youtube_ms),
        "error": error,
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
