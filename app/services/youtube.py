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

