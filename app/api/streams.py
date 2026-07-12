import ssl

import aiohttp
import certifi
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import YoutubePlaylistRequest, YoutubeSearchRequest, YoutubeUrlRequest
from app.services.stream_service import resolve_stream
from app.services.youtube import extract_playlist_items, search_youtube

router = APIRouter()


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
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/stream")
async def stream_compat(payload: YoutubeUrlRequest, db: Session = Depends(get_db)):
    return await resolve_stream_post(payload, db)


@router.post("/search")
async def search(payload: YoutubeSearchRequest):
    try:
        return {"items": search_youtube(payload.query, payload.limit)}
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/playlist")
async def playlist(payload: YoutubePlaylistRequest):
    try:
        return {"items": extract_playlist_items(payload.url, payload.limit)}
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/api/streams/download")
async def download_stream_post(payload: YoutubeUrlRequest, db: Session = Depends(get_db)):
    return await download(payload, db)


@router.post("/download")
async def download(payload: YoutubeUrlRequest, db: Session = Depends(get_db)):
    try:
        metadata = await resolve_stream(
            db,
            payload.url,
            use_proxy=payload.use_proxy,
            force_refresh=True,
        )
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    stream_url = metadata.get("stream_url")
    if not stream_url:
        raise HTTPException(status_code=502, detail="stream URL was not resolved")

    async def chunks():
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(stream_url) as response:
                response.raise_for_status()
                async for chunk in response.content.iter_chunked(1024 * 256):
                    yield chunk

    ext = metadata.get("ext") or "m4a"
    media_type = "audio/webm" if ext == "webm" else "audio/mp4"
    headers = {
        "Cache-Control": "no-store",
        "X-Track-Id": str(metadata.get("video_id") or ""),
        "X-Track-Title": str(metadata.get("title") or "youtube-audio"),
        "X-Track-Artist": str(metadata.get("uploader") or ""),
        "X-File-Ext": str(ext),
    }
    return StreamingResponse(chunks(), media_type=media_type, headers=headers)
