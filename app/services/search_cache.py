import json
import re
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AudioMetadataCache, SearchQueryCache


def normalize_search_query(query: str) -> str:
    normalized = re.sub(r"\s+", " ", query.strip().lower())
    return normalized[:512]


def provider_for_mode(mode: str) -> str:
    return "soundcloud" if (mode or "").strip().lower() == "soundcloud" else "youtube"


def cached_search_items(db: Session, query: str, mode: str, limit: int) -> list[dict] | None:
    search_query = normalize_search_query(query)
    provider = provider_for_mode(mode)
    normalized_mode = normalize_mode(mode)
    if not search_query:
        return None

    row = (
        db.query(SearchQueryCache)
        .filter(SearchQueryCache.provider == provider)
        .filter(SearchQueryCache.mode == normalized_mode)
        .filter(SearchQueryCache.search_query == search_query)
        .first()
    )
    if not row:
        return None

    try:
        result_ids = json.loads(row.result_ids_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(result_ids, list) or not result_ids:
        return None

    rows = (
        db.query(AudioMetadataCache)
        .filter(AudioMetadataCache.provider == provider)
        .filter(AudioMetadataCache.provider_media_id.in_(result_ids))
        .all()
    )
    metadata_by_id = {item.provider_media_id: item for item in rows}
    cached_items: list[dict] = []
    for media_id in result_ids[:limit]:
        metadata_row = metadata_by_id.get(str(media_id))
        if not metadata_row:
            continue
        try:
            item = json.loads(metadata_row.metadata_json)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            cached_items.append(item)

    if not cached_items:
        return None

    row.last_requested_at = datetime.utcnow()
    db.commit()
    return cached_items


def store_search_items(db: Session, query: str, mode: str, items: list[dict]) -> None:
    search_query = normalize_search_query(query)
    provider = provider_for_mode(mode)
    normalized_mode = normalize_mode(mode)
    if not search_query:
        return

    now = datetime.utcnow()
    result_ids: list[str] = []
    seen_ids: set[str] = set()
    for item in items:
        provider_media_id = str(item.get("id") or "").strip()
        if not provider_media_id or provider_media_id in seen_ids:
            continue
        seen_ids.add(provider_media_id)
        result_ids.append(provider_media_id)
        metadata_row = (
            db.query(AudioMetadataCache)
            .filter(AudioMetadataCache.provider == provider)
            .filter(AudioMetadataCache.provider_media_id == provider_media_id)
            .first()
        )
        metadata_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if metadata_row:
            metadata_row.origin_url = str(item.get("url") or metadata_row.origin_url)
            metadata_row.metadata_json = merge_metadata_json(metadata_row.metadata_json, item)
            metadata_row.updated_at = now
        else:
            try:
                with db.begin_nested():
                    db.add(
                        AudioMetadataCache(
                            provider=provider,
                            provider_media_id=provider_media_id,
                            origin_url=str(item.get("url") or ""),
                            metadata_json=metadata_json,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            except IntegrityError:
                metadata_row = (
                    db.query(AudioMetadataCache)
                    .filter(AudioMetadataCache.provider == provider)
                    .filter(AudioMetadataCache.provider_media_id == provider_media_id)
                    .first()
                )
                if metadata_row:
                    metadata_row.origin_url = str(item.get("url") or metadata_row.origin_url)
                    metadata_row.metadata_json = merge_metadata_json(metadata_row.metadata_json, item)
                    metadata_row.updated_at = now

    if not result_ids:
        db.commit()
        return

    query_row = (
        db.query(SearchQueryCache)
        .filter(SearchQueryCache.provider == provider)
        .filter(SearchQueryCache.mode == normalized_mode)
        .filter(SearchQueryCache.search_query == search_query)
        .first()
    )
    result_ids_json = json.dumps(result_ids, ensure_ascii=False, separators=(",", ":"))
    if query_row:
        query_row.result_ids_json = result_ids_json
        query_row.last_requested_at = now
    else:
        try:
            with db.begin_nested():
                db.add(
                    SearchQueryCache(
                        provider=provider,
                        mode=normalized_mode,
                        search_query=search_query,
                        result_ids_json=result_ids_json,
                        created_at=now,
                        last_requested_at=now,
                    )
                )
        except IntegrityError:
            query_row = (
                db.query(SearchQueryCache)
                .filter(SearchQueryCache.provider == provider)
                .filter(SearchQueryCache.mode == normalized_mode)
                .filter(SearchQueryCache.search_query == search_query)
                .first()
            )
            if query_row:
                query_row.result_ids_json = result_ids_json
                query_row.last_requested_at = now
    db.commit()


def normalize_mode(mode: str) -> str:
    normalized = (mode or "youtube-music").strip().lower()
    if normalized == "soundcloud":
        return "soundcloud"
    if normalized in {"youtube-all", "youtube-video", "youtube"}:
        return "youtube-all"
    return "youtube-music"


def merge_metadata_json(existing_json: str, patch: dict) -> str:
    try:
        existing = json.loads(existing_json)
    except json.JSONDecodeError:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    merged = {**existing, **{key: value for key, value in patch.items() if value not in (None, "")}}
    return json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
