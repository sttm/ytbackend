import json
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AudioMetadataCache, SearchQueryCache, TrackFingerprintCache, TrackUsageEvent
from app.schemas import TrackUsageRequest
from app.services.track_metadata import upsert_provider_metadata

router = APIRouter()


@router.get("/api/tracks")
@router.get("/api/tracks/search")
def tracks(
    provider: str = Query("", description="youtube, soundcloud, cache, or empty for all"),
    q: str = Query("", description="Search cached title/artist/album/genre text"),
    genre: str = Query("", description="Primary genre or Discogs subgenre label"),
    subgenre: str = Query("", description="Discogs subgenre label"),
    artist: str = Query("", description="Artist filter"),
    key: str = Query("", description="Musical key filter"),
    bpm_min: float | None = Query(None, ge=0),
    bpm_max: float | None = Query(None, ge=0),
    year_min: int | None = Query(None, ge=1800),
    year_max: int | None = Query(None, ge=1800),
    sort: str = Query("popular", description="popular, recent, title, bpm, relevance"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1000),
    db: Session = Depends(get_db),
):
    provider_filter = provider.strip().lower()
    popularity = build_popularity_index(db)
    serialized: list[dict[str, Any]] = []

    if provider_filter != "cache":
        query = db.query(AudioMetadataCache)
        if provider_filter in {"youtube", "soundcloud"}:
            query = query.filter(AudioMetadataCache.provider == provider_filter)
        rows = query.all()
        serialized.extend(serialize_track(row, popularity.get(cache_key(row.provider, row.provider_media_id), {})) for row in rows)

    if provider_filter in {"", "cache", "fingerprint"}:
        rows = db.query(TrackFingerprintCache).all()
        serialized.extend(serialize_fingerprint_track(row) for row in rows)

    serialized = dedupe_tracks(serialized)
    serialized = [
        item for item in serialized
        if matches_filters(
            item,
            q=q,
            genre=genre,
            subgenre=subgenre,
            artist=artist,
            key=key,
            bpm_min=bpm_min,
            bpm_max=bpm_max,
            year_min=year_min,
            year_max=year_max,
        )
    ]

    if sort == "recent":
        serialized.sort(key=lambda item: item.get("last_requested_at") or item.get("updated_at") or "", reverse=True)
    elif sort == "title":
        serialized.sort(key=lambda item: (item.get("title") or "").lower())
    elif sort == "bpm":
        serialized.sort(key=lambda item: (number_or_none(item.get("bpm")) is None, number_or_none(item.get("bpm")) or 0, (item.get("title") or "").lower()))
    elif sort == "relevance":
        serialized.sort(key=lambda item: relevance_score(item, q), reverse=True)
    else:
        serialized = [item for item in serialized if (item.get("popularity") or 0) > 0]
        serialized.sort(key=lambda item: (item.get("popularity") or 0, item.get("last_requested_at") or ""), reverse=True)

    total = len(serialized)
    top_items = serialized[:100]
    total = len(top_items)
    page_items = top_items[offset : offset + limit]
    return {
        "tracks": page_items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "page": offset // limit + 1,
        "pages": max(1, (total + limit - 1) // limit),
    }


@router.post("/api/tracks/usage")
def track_usage(payload: TrackUsageRequest, db: Session = Depends(get_db)):
    provider = payload.provider.strip().lower()
    media_id = payload.id.strip()
    action = payload.action.strip().lower()
    if provider not in {"youtube", "soundcloud"}:
        return {"status": "ignored", "reason": "unsupported provider"}
    if action not in {"play", "offline_download"}:
        return {"status": "ignored", "reason": "unsupported action"}
    if not media_id:
        return {"status": "ignored", "reason": "missing media id"}

    metadata = payload.metadata if isinstance(payload.metadata, dict) else {}
    if metadata:
        upsert_provider_metadata(db, provider, media_id, payload.url.strip(), metadata)

    event = TrackUsageEvent(
        provider=provider,
        provider_media_id=media_id,
        origin_url=payload.url.strip(),
        action=action,
    )
    db.add(event)
    db.commit()
    return {"status": "ok"}


@router.delete("/api/tracks/{provider}/{media_id}")
def delete_track_cache_item(provider: str, media_id: str, db: Session = Depends(get_db)):
    normalized_provider = provider.strip().lower()
    normalized_media_id = media_id.strip()
    if normalized_provider not in {"youtube", "soundcloud"} or not normalized_media_id:
        raise HTTPException(status_code=400, detail="Invalid provider or media id.")

    metadata_deleted = (
        db.query(AudioMetadataCache)
        .filter(AudioMetadataCache.provider == normalized_provider)
        .filter(AudioMetadataCache.provider_media_id == normalized_media_id)
        .delete(synchronize_session=False)
    )
    usage_deleted = (
        db.query(TrackUsageEvent)
        .filter(TrackUsageEvent.provider == normalized_provider)
        .filter(TrackUsageEvent.provider_media_id == normalized_media_id)
        .delete(synchronize_session=False)
    )
    queries_updated = remove_media_id_from_search_queries(db, normalized_provider, normalized_media_id)
    db.commit()
    return {
        "status": "ok",
        "provider": normalized_provider,
        "media_id": normalized_media_id,
        "metadata_deleted": metadata_deleted,
        "usage_deleted": usage_deleted,
        "queries_updated": queries_updated,
    }


@router.delete("/api/tracks")
def clear_tracks_cache(db: Session = Depends(get_db)):
    usage_deleted = db.query(TrackUsageEvent).delete(synchronize_session=False)
    queries_deleted = db.query(SearchQueryCache).delete(synchronize_session=False)
    metadata_deleted = db.query(AudioMetadataCache).delete(synchronize_session=False)
    db.commit()
    return {
        "status": "ok",
        "metadata_deleted": metadata_deleted,
        "queries_deleted": queries_deleted,
        "usage_deleted": usage_deleted,
    }


def remove_media_id_from_search_queries(db: Session, provider: str, media_id: str) -> int:
    updated = 0
    for row in db.query(SearchQueryCache).filter(SearchQueryCache.provider == provider).all():
        try:
            result_ids = json.loads(row.result_ids_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(result_ids, list):
            continue
        next_ids = [item for item in result_ids if str(item) != media_id]
        if len(next_ids) == len(result_ids):
            continue
        if next_ids:
            row.result_ids_json = json.dumps(next_ids, ensure_ascii=False, separators=(",", ":"))
        else:
            db.delete(row)
        updated += 1
    return updated


def build_popularity_index(db: Session) -> dict[str, dict]:
    index: dict[str, dict] = {}
    rows = db.query(TrackUsageEvent).all()
    for row in rows:
        key = cache_key(row.provider, row.provider_media_id)
        entry = index.setdefault(key, {"play_count": 0, "download_count": 0, "weighted_score": 0, "last_requested_at": None})
        if row.action == "offline_download":
            entry["download_count"] += 1
            entry["weighted_score"] += 5
        elif row.action == "play":
            entry["play_count"] += 1
            entry["weighted_score"] += 1
        created_at = row.created_at
        if isinstance(created_at, datetime):
            current = entry.get("last_requested_at")
            if not current or created_at > current:
                entry["last_requested_at"] = created_at
    return index


def serialize_track(row: AudioMetadataCache, popularity: dict) -> dict:
    metadata = safe_json(row.metadata_json)

    last_requested_at = popularity.get("last_requested_at")
    return {
        "id": row.provider_media_id,
        "provider": row.provider,
        "url": metadata.get("url") or row.origin_url,
        "title": metadata.get("title") or row.provider_media_id,
        "artist": metadata.get("artist") or metadata.get("uploader") or "",
        "album": metadata.get("album") or "",
        "duration": metadata.get("duration"),
        "thumbnail": metadata.get("thumbnail") or "",
        "source": metadata.get("source") or metadata.get("provider") or row.provider,
        "genre": metadata.get("genre"),
        "genreTags": metadata.get("genreTags") if isinstance(metadata.get("genreTags"), list) else [],
        "genreConfidence": metadata.get("genreConfidence"),
        "genreModel": metadata.get("genreModel"),
        "bpm": metadata.get("bpm"),
        "key": metadata.get("key"),
        "year": parse_year(metadata),
        "lufs": metadata.get("lufs"),
        "sampleRate": metadata.get("sampleRate") or metadata.get("sample_rate"),
        "bitrate": metadata.get("bitrate"),
        "fingerprintHash": metadata.get("fingerprintHash") or metadata.get("fingerprint_hash"),
        "fingerprintVersion": metadata.get("fingerprintVersion") or metadata.get("fingerprint_version"),
        "chromaprintFingerprint": metadata.get("chromaprintFingerprint") or metadata.get("chromaprint_fingerprint"),
        "metadataSource": metadata.get("metadataSource"),
        "metadataConfidence": metadata.get("metadataConfidence"),
        "popularity": popularity.get("weighted_score", 0),
        "play_count": popularity.get("play_count", 0),
        "download_count": popularity.get("download_count", 0),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_requested_at": last_requested_at.isoformat() if isinstance(last_requested_at, datetime) else None,
    }


def cache_key(provider: str, provider_media_id: str) -> str:
    return f"{provider}:{provider_media_id}"


def serialize_fingerprint_track(row: TrackFingerprintCache) -> dict:
    metadata = safe_json(row.metadata_json)
    return {
        "id": f"fp-{row.fingerprint_hash[:16]}",
        "provider": metadata.get("provider") or "cache",
        "url": metadata.get("url") or metadata.get("originUrl") or metadata.get("origin_url") or "",
        "title": row.title or metadata.get("title") or "Untitled track",
        "artist": row.artist or metadata.get("artist") or "",
        "album": row.album or metadata.get("album") or "",
        "duration": row.duration if row.duration is not None else metadata.get("duration"),
        "thumbnail": row.artwork_url or metadata.get("thumbnail") or metadata.get("artworkUrl") or metadata.get("artwork_url") or "",
        "source": "fingerprint-cache",
        "genre": row.genre or metadata.get("genre"),
        "genreTags": metadata.get("genreTags") if isinstance(metadata.get("genreTags"), list) else [],
        "genreConfidence": metadata.get("genreConfidence"),
        "genreModel": metadata.get("genreModel"),
        "bpm": row.bpm if row.bpm is not None else metadata.get("bpm"),
        "key": row.musical_key or metadata.get("key"),
        "year": parse_year(metadata),
        "lufs": row.lufs if row.lufs is not None else metadata.get("lufs"),
        "sampleRate": row.sample_rate or metadata.get("sampleRate") or metadata.get("sample_rate"),
        "bitrate": row.bitrate or metadata.get("bitrate"),
        "fingerprintHash": row.fingerprint_hash,
        "fingerprintVersion": metadata.get("fingerprintVersion") or metadata.get("fingerprint_version"),
        "chromaprintFingerprint": metadata.get("chromaprintFingerprint") or metadata.get("chromaprint_fingerprint"),
        "metadataSource": metadata.get("metadataSource") or "fingerprint",
        "metadataConfidence": metadata.get("metadataConfidence") or row.confidence,
        "popularity": 0,
        "play_count": 0,
        "download_count": 0,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_requested_at": None,
    }


def dedupe_tracks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        fingerprint = str(item.get("fingerprintHash") or "").strip()
        provider = str(item.get("provider") or "").strip()
        media_id = str(item.get("id") or "").strip()
        key = f"fp:{fingerprint}" if fingerprint else f"provider:{provider}:{media_id}"
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def matches_filters(
    item: dict[str, Any],
    *,
    q: str,
    genre: str,
    subgenre: str,
    artist: str,
    key: str,
    bpm_min: float | None,
    bpm_max: float | None,
    year_min: int | None,
    year_max: int | None,
) -> bool:
    if q and normalize(q) not in searchable_text(item):
        return False
    if artist and normalize(artist) not in normalize(str(item.get("artist") or "")):
        return False
    if key and normalize_key(key) != normalize_key(str(item.get("key") or "")):
        return False

    genre_filter = genre or subgenre
    if genre_filter and normalize(genre_filter) not in genre_text(item):
        return False

    bpm = number_or_none(item.get("bpm"))
    if bpm_min is not None and (bpm is None or bpm < bpm_min):
        return False
    if bpm_max is not None and (bpm is None or bpm > bpm_max):
        return False

    year = number_or_none(item.get("year"))
    if year_min is not None and (year is None or year < year_min):
        return False
    if year_max is not None and (year is None or year > year_max):
        return False
    return True


def searchable_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("title"),
        item.get("artist"),
        item.get("album"),
        item.get("genre"),
        item.get("key"),
        item.get("year"),
        *genre_tag_labels(item),
    ]
    return normalize(" ".join(str(part) for part in parts if part not in (None, "")))


def genre_text(item: dict[str, Any]) -> str:
    parts = [item.get("genre"), *genre_tag_labels(item)]
    return normalize(" ".join(str(part) for part in parts if part not in (None, "")))


def genre_tag_labels(item: dict[str, Any]) -> list[str]:
    tags = item.get("genreTags")
    if not isinstance(tags, list):
        return []
    labels: list[str] = []
    for tag in tags:
        if isinstance(tag, dict) and tag.get("label"):
            labels.append(str(tag["label"]))
        elif isinstance(tag, str):
            labels.append(tag)
    return labels


def relevance_score(item: dict[str, Any], q: str) -> float:
    needle = normalize(q)
    score = float(item.get("popularity") or 0) * 0.01
    if not needle:
        return score + float(number_or_none(item.get("metadataConfidence")) or 0)
    title = normalize(str(item.get("title") or ""))
    artist = normalize(str(item.get("artist") or ""))
    album = normalize(str(item.get("album") or ""))
    genre = genre_text(item)
    if needle == title:
        score += 10
    elif needle in title:
        score += 6
    if needle == artist:
        score += 5
    elif needle in artist:
        score += 3
    if needle and needle in album:
        score += 2
    if needle and needle in genre:
        score += 1
    return score


def parse_year(metadata: dict[str, Any]) -> int | None:
    for key in ("year", "releaseYear", "release_year", "date", "upload_date"):
        value = metadata.get(key)
        if value in (None, ""):
            continue
        match = re.search(r"(19|20)\d{2}", str(value))
        if match:
            return int(match.group(0))
    return None


def safe_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def normalize_key(value: str) -> str:
    return normalize(value).replace("♯", "#").replace("♭", "b").replace(" minor", "m").replace(" major", "")
