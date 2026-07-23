from __future__ import annotations

import ssl
from urllib.parse import urlparse

import aiohttp
import certifi
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

router = APIRouter()

ALLOWED_IMAGE_HOSTS = {
    "i.ytimg.com",
    "yt3.googleusercontent.com",
    "i1.sndcdn.com",
    "i2.sndcdn.com",
    "i3.sndcdn.com",
    "i4.sndcdn.com",
}


@router.get("/api/media/artwork")
async def artwork(url: str = Query(...)):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Unsupported artwork URL.")
    if parsed.hostname not in ALLOWED_IMAGE_HOSTS:
        raise HTTPException(status_code=400, detail="Unsupported artwork host.")

    timeout = aiohttp.ClientTimeout(total=12, sock_connect=5, sock_read=8)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    try:
        response = await session.get(
            url,
            headers={
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "User-Agent": "Mozilla/5.0 ProducersCenter/0.1",
            },
        )
        response.raise_for_status()
    except Exception as error:
        await session.close()
        raise HTTPException(status_code=502, detail=f"Artwork fetch failed: {error}") from error

    headers = {
        "Cache-Control": "public, max-age=86400",
        "Content-Type": response.headers.get("Content-Type") or "image/jpeg",
    }
    content_length = response.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length

    async def chunks():
        try:
            async for chunk in response.content.iter_chunked(1024 * 64):
                yield chunk
        finally:
            response.close()
            await session.close()

    return StreamingResponse(chunks(), media_type=headers["Content-Type"], headers=headers)
