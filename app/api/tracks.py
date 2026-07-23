import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AudioMetadataCache, SearchQueryCache, TrackUsageEvent
from app.schemas import TrackUsageRequest
from app.services.track_metadata import upsert_provider_metadata

router = APIRouter()


@router.get("/api/tracks")
def tracks(
    provider: str = Query("", description="youtube or soundcloud"),
    q: str = Query("", description="Search cached title/artist text"),
    sort: str = Query("popular", description="popular, recent, title"),
    limit: int = Query(50, ge=1, le=50),
    offset: int = Query(0, ge=0, le=50),
    db: Session = Depends(get_db),
):
    query = db.query(AudioMetadataCache)
    if provider:
        query = query.filter(AudioMetadataCache.provider == provider)
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.filter(AudioMetadataCache.metadata_json.ilike(like))

    rows = query.all()
    popularity = build_popularity_index(db)
    serialized = [serialize_track(row, popularity.get(cache_key(row.provider, row.provider_media_id), {})) for row in rows]

    if sort == "recent":
        serialized.sort(key=lambda item: item.get("last_requested_at") or item.get("updated_at") or "", reverse=True)
    elif sort == "title":
        serialized.sort(key=lambda item: (item.get("title") or "").lower())
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
    try:
        metadata = json.loads(row.metadata_json)
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    last_requested_at = popularity.get("last_requested_at")
    return {
        "id": row.provider_media_id,
        "provider": row.provider,
        "url": metadata.get("url") or row.origin_url,
        "title": metadata.get("title") or row.provider_media_id,
        "artist": metadata.get("artist") or metadata.get("uploader") or "",
        "duration": metadata.get("duration"),
        "thumbnail": metadata.get("thumbnail") or "",
        "source": metadata.get("source") or metadata.get("provider") or row.provider,
        "genre": metadata.get("genre"),
        "bpm": metadata.get("bpm"),
        "key": metadata.get("key"),
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
