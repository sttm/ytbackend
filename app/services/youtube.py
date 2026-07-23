from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse
import yt_dlp

try:
    from ytmusicapi import YTMusic
except Exception:  # pragma: no cover - optional dependency guard for degraded installs
    YTMusic = None


MAX_SEARCH_TRACKS = 30
MAX_SEARCH_CONTAINERS = 10
MAX_SEARCH_RESULTS = MAX_SEARCH_TRACKS + MAX_SEARCH_CONTAINERS


class QuietYtDlpLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


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
        "logger": QuietYtDlpLogger(),
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


def search_media(query: str, limit: int = 10, mode: str = "youtube-music") -> list[dict]:
    normalized_mode = (mode or "youtube-music").strip().lower()
    limit = max(1, min(limit, MAX_SEARCH_RESULTS))
    if normalized_mode == "youtube-music":
        music_items = search_youtube_music(query, limit)
        if music_items:
            return music_items

    provider = "soundcloud" if normalized_mode == "soundcloud" else "youtube"
    prefix = "scsearch" if provider == "soundcloud" else "ytsearch"
    search_limit = max(MAX_SEARCH_TRACKS * 2, 40) if provider == "soundcloud" else MAX_SEARCH_RESULTS
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
        "logger": QuietYtDlpLogger(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{prefix}{search_limit}:{query}", download=False)
    entries = info.get("entries") or []

    if normalized_mode == "youtube-music":
        music_entries = [
            entry
            for entry in entries
            if "topic" in f"{entry.get('channel') or ''} {entry.get('uploader') or ''}".lower()
            or "music" in f"{entry.get('channel') or ''} {entry.get('uploader') or ''}".lower()
        ]
        if music_entries:
            entries = music_entries

    items = [
        {
            "id": entry.get("id"),
            "title": entry.get("title") or entry.get("id") or "",
            "artist": entry.get("uploader") or entry.get("channel") or "",
            "duration": entry.get("duration"),
            "thumbnail": entry.get("thumbnail") or small_thumbnail(entry),
            "url": search_entry_url(entry, provider),
            "provider": provider,
        }
        for entry in entries
        if entry.get("id")
    ]

    containers = [item for item in items if item.get("kind") in {"album", "playlist"}][:MAX_SEARCH_CONTAINERS]
    tracks = [item for item in items if item.get("kind") not in {"album", "playlist"}]
    track_limit = MAX_SEARCH_TRACKS
    return (containers + tracks[:track_limit])[:limit]


def search_youtube(query: str, limit: int = 10) -> list[dict]:
    return search_media(query, limit, "youtube-all")


def search_youtube_music(query: str, limit: int = 10) -> list[dict]:
    if YTMusic is None:
        return []
    limit = max(1, min(limit, MAX_SEARCH_RESULTS))

    try:
        client = YTMusic()
        results = client.search(query, filter="songs", limit=MAX_SEARCH_TRACKS)
        album_results = client.search(query, filter="albums", limit=MAX_SEARCH_CONTAINERS)
        playlist_results = client.search(query, filter="playlists", limit=MAX_SEARCH_CONTAINERS)
    except Exception:
        return []

    items: list[dict] = []
    for entry in results or []:
        video_id = entry.get("videoId")
        if not video_id:
            continue
        artists = entry.get("artists") or []
        artist = ", ".join(
            artist_entry.get("name", "")
            for artist_entry in artists
            if isinstance(artist_entry, dict) and artist_entry.get("name")
        )
        thumbnail = best_ytmusic_thumbnail(entry.get("thumbnails") or [])
        items.append(
            {
                "id": video_id,
                "title": entry.get("title") or video_id,
                "artist": artist or entry.get("artist") or "YouTube Music",
                "duration": entry.get("duration_seconds") or parse_duration(entry.get("duration")),
                "thumbnail": thumbnail or f"https://i.ytimg.com/vi/{video_id}/default.jpg",
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "provider": "youtube",
                "source": "youtube-music",
            }
        )

    for entry in album_results or []:
        browse_id = entry.get("browseId")
        playlist_id = entry.get("playlistId") or entry.get("audioPlaylistId")
        if not browse_id and not playlist_id:
            continue
        artists = entry.get("artists") or []
        artist = ", ".join(
            artist_entry.get("name", "")
            for artist_entry in artists
            if isinstance(artist_entry, dict) and artist_entry.get("name")
        )
        thumbnail = best_ytmusic_thumbnail(entry.get("thumbnails") or [])
        url = f"https://music.youtube.com/playlist?list={playlist_id}" if playlist_id else f"https://music.youtube.com/browse/{browse_id}"
        items.append(
            {
                "id": playlist_id or browse_id,
                "title": entry.get("title") or playlist_id or browse_id,
                "artist": artist or entry.get("artist") or "YouTube Music",
                "duration": None,
                "thumbnail": thumbnail,
                "url": url,
                "provider": "youtube",
                "source": "youtube-music",
                "kind": "album",
            }
        )

    for entry in playlist_results or []:
        browse_id = entry.get("browseId")
        playlist_id = entry.get("playlistId") or entry.get("audioPlaylistId")
        if not playlist_id and not browse_id:
            continue
        thumbnail = best_ytmusic_thumbnail(entry.get("thumbnails") or [])
        url = f"https://music.youtube.com/playlist?list={playlist_id}" if playlist_id else f"https://music.youtube.com/browse/{browse_id}"
        items.append(
            {
                "id": playlist_id or browse_id,
                "title": entry.get("title") or playlist_id or browse_id,
                "artist": entry.get("author") or entry.get("artist") or "YouTube Music",
                "duration": None,
                "thumbnail": thumbnail,
                "url": url,
                "provider": "youtube",
                "source": "youtube-music",
                "kind": "playlist",
            }
        )

    containers = [item for item in items if item.get("kind") in {"album", "playlist"}][:MAX_SEARCH_CONTAINERS]
    tracks = [item for item in items if item.get("kind") not in {"album", "playlist"}]
    track_limit = MAX_SEARCH_TRACKS
    return (containers + tracks[:track_limit])[:limit]


def best_ytmusic_thumbnail(thumbnails: list[dict]) -> str | None:
    if not thumbnails:
        return None
    return max(
        thumbnails,
        key=lambda item: (item.get("width") or 0, item.get("height") or 0),
    ).get("url")


def parse_duration(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.split(":")
    if not all(part.isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def search_entry_url(entry: dict, provider: str) -> str:
    raw_url = str(entry.get("webpage_url") or entry.get("url") or "")
    if raw_url.startswith("http"):
        return raw_url
    if provider == "soundcloud":
        return raw_url
    return f"https://www.youtube.com/watch?v={entry.get('id')}"


def extract_playlist_items(url: str, limit: int = 100) -> list[dict]:
    ytmusic_items = extract_ytmusic_playlist_items(url, limit)
    if ytmusic_items:
        return ytmusic_items

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
        "logger": QuietYtDlpLogger(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries") or []
    return [
        {
            "id": entry.get("id"),
            "title": entry.get("title") or entry.get("id") or "",
            "artist": entry.get("uploader") or entry.get("channel") or "",
            "duration": entry.get("duration"),
            "thumbnail": entry.get("thumbnail") or small_thumbnail(entry),
            "url": entry.get("webpage_url") or (entry.get("url") if str(entry.get("url") or "").startswith("http") else f"https://www.youtube.com/watch?v={entry.get('id')}"),
            "provider": "youtube",
            "kind": "track",
        }
        for entry in entries
        if entry.get("id")
    ]


def extract_playlist_metadata(url: str) -> dict:
    ytmusic_metadata = extract_ytmusic_playlist_metadata(url)
    if ytmusic_metadata:
        return ytmusic_metadata

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": 1,
        "logger": QuietYtDlpLogger(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    provider = "soundcloud" if "soundcloud.com" in url else "youtube"
    return {
        "id": info.get("id") or info.get("playlist_id") or url,
        "title": info.get("title") or info.get("playlist_title") or "Playlist",
        "artist": info.get("uploader") or info.get("channel") or info.get("creator") or provider,
        "thumbnail": info.get("thumbnail") or small_thumbnail(info) or "",
        "url": url,
        "provider": provider,
        "kind": "playlist",
    }


def extract_ytmusic_playlist_items(url: str, limit: int = 100) -> list[dict]:
    if YTMusic is None or "music.youtube.com" not in url:
        return []

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    playlist_id = (query.get("list") or [""])[0]
    browse_id = parsed.path.split("/")[-1] if parsed.path.startswith("/browse/") else ""
    try:
        client = YTMusic()
        if browse_id:
            data = client.get_album(browse_id)
            tracks = data.get("tracks") or []
        elif playlist_id:
            data = client.get_playlist(playlist_id, limit=limit)
            tracks = data.get("tracks") or []
        else:
            return []
    except Exception:
        return []

    items: list[dict] = []
    for entry in tracks[:limit]:
        video_id = entry.get("videoId")
        if not video_id:
            continue
        artists = entry.get("artists") or []
        artist = ", ".join(
            artist_entry.get("name", "")
            for artist_entry in artists
            if isinstance(artist_entry, dict) and artist_entry.get("name")
        )
        items.append(
            {
                "id": video_id,
                "title": entry.get("title") or video_id,
                "artist": artist or "YouTube Music",
                "duration": entry.get("duration_seconds") or parse_duration(entry.get("duration")),
                "thumbnail": best_ytmusic_thumbnail(entry.get("thumbnails") or []),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "provider": "youtube",
                "kind": "track",
            }
        )
    return items


def extract_ytmusic_playlist_metadata(url: str) -> dict:
    if YTMusic is None or "music.youtube.com" not in url:
        return {}

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    playlist_id = (query.get("list") or [""])[0]
    browse_id = parsed.path.split("/")[-1] if parsed.path.startswith("/browse/") else ""
    try:
        client = YTMusic()
        if browse_id:
            data = client.get_album(browse_id)
            kind = "album"
        elif playlist_id:
            data = client.get_playlist(playlist_id, limit=1)
            kind = "playlist"
        else:
            return {}
    except Exception:
        return {}

    artists = data.get("artists") or []
    artist = ", ".join(
        artist_entry.get("name", "")
        for artist_entry in artists
        if isinstance(artist_entry, dict) and artist_entry.get("name")
    )
    first_track = (data.get("tracks") or [{}])[0] if isinstance(data.get("tracks"), list) else {}
    if not artist and isinstance(first_track, dict):
        track_artists = first_track.get("artists") or []
        artist = ", ".join(
            artist_entry.get("name", "")
            for artist_entry in track_artists
            if isinstance(artist_entry, dict) and artist_entry.get("name")
        )
    thumbnail = best_ytmusic_thumbnail(data.get("thumbnails") or [])
    if not thumbnail and isinstance(first_track, dict):
        thumbnail = best_ytmusic_thumbnail(first_track.get("thumbnails") or [])
    return {
        "id": playlist_id or browse_id,
        "title": data.get("title") or playlist_id or browse_id or "YouTube Music playlist",
        "artist": artist or data.get("author") or data.get("channel") or "YouTube Music",
        "thumbnail": thumbnail,
        "url": url,
        "provider": "youtube",
        "source": "youtube-music",
        "kind": kind,
    }
