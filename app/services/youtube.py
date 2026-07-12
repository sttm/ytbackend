from __future__ import annotations

import re
import yt_dlp


def extract_video_id(url: str) -> str | None:
    patterns = [
        r"(?:v=)([^&]+)",
        r"youtu\.be/([^?&/]+)",
        r"youtube\.com/shorts/([^?&/]+)",
        r"youtube\.com/live/([^?&/]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def small_thumbnail(info: dict) -> str | None:
    thumbnails = info.get("thumbnails") or []
    if not thumbnails:
        return info.get("thumbnail")
    small = [thumb for thumb in thumbnails if (thumb.get("width") or 0) <= 120]
    if small:
        return max(small, key=lambda thumb: thumb.get("width") or 0).get("url")
    return min(thumbnails, key=lambda thumb: thumb.get("width") or 99999).get("url")


def extract_best_audio(youtube_url: str, proxy_url: str | None = None) -> dict:
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "socket_timeout": 18,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "skip_unavailable_fragments": True,
        "noplaylist": True,
    }
    if proxy_url:
        opts["proxy"] = proxy_url

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)

    formats = [
        fmt
        for fmt in info.get("formats", [])
        if fmt.get("url") and fmt.get("acodec") and fmt.get("acodec") != "none"
    ]
    if not formats:
        raise RuntimeError("No audio formats found")

    best = max(
        formats,
        key=lambda fmt: (
            fmt.get("abr") or 0,
            fmt.get("asr") or 0,
            fmt.get("filesize") or fmt.get("filesize_approx") or 0,
        ),
    )

    return {
        "video_id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": small_thumbnail(info),
        "stream_url": best["url"],
        "format_id": best.get("format_id"),
        "audio_codec": best.get("acodec"),
        "ext": best.get("ext"),
        "bitrate": best.get("abr") or 0,
        "sample_rate": best.get("asr") or 0,
        "filesize": best.get("filesize") or best.get("filesize_approx") or 0,
    }


def search_youtube(query: str, limit: int = 10) -> list[dict]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    entries = info.get("entries") or []
    return [
        {
            "id": entry.get("id"),
            "title": entry.get("title") or entry.get("id") or "",
            "artist": entry.get("uploader") or entry.get("channel") or "",
            "duration": entry.get("duration"),
            "thumbnail": entry.get("thumbnail") or small_thumbnail(entry),
            "url": entry.get("url") if str(entry.get("url") or "").startswith("http") else f"https://www.youtube.com/watch?v={entry.get('id')}",
        }
        for entry in entries
        if entry.get("id")
    ]


def extract_playlist_items(url: str, limit: int = 100) -> list[dict]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries") or []
    return [
        {
            "id": entry.get("id"),
            "title": entry.get("title") or entry.get("id") or "",
            "url": entry.get("url") if str(entry.get("url") or "").startswith("http") else f"https://www.youtube.com/watch?v={entry.get('id')}",
        }
        for entry in entries
        if entry.get("id")
    ]
