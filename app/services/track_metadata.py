from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import AudioMetadataCache, TrackFingerprintCache, TrackProviderAlias


KNOWN_METADATA_KEYS = {
    "title",
    "artist",
    "album",
    "genre",
    "bpm",
    "key",
    "lufs",
    "duration",
    "sampleRate",
    "sample_rate",
    "bitrate",
    "thumbnail",
    "artworkUrl",
    "artwork_url",
    "url",
    "provider",
    "id",
    "fingerprintHash",
    "fingerprint_hash",
    "fingerprintVersion",
    "fingerprint_version",
    "chromaprintFingerprint",
    "chromaprint_fingerprint",
    "metadataSource",
    "metadataConfidence",
}


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def metadata_from_row(row: TrackFingerprintCache, source: str, confidence: float | None = None) -> dict[str, Any]:
    metadata = safe_json(row.metadata_json)
    return {
        **metadata,
        "title": row.title or metadata.get("title"),
        "artist": row.artist or metadata.get("artist"),
        "album": row.album or metadata.get("album"),
        "genre": row.genre or metadata.get("genre"),
        "bpm": row.bpm if row.bpm is not None else metadata.get("bpm"),
        "key": row.musical_key or metadata.get("key"),
        "lufs": row.lufs if row.lufs is not None else metadata.get("lufs"),
        "duration": row.duration if row.duration is not None else metadata.get("duration"),
        "sampleRate": row.sample_rate or metadata.get("sampleRate") or metadata.get("sample_rate"),
        "bitrate": row.bitrate or metadata.get("bitrate"),
        "thumbnail": row.artwork_url or metadata.get("thumbnail") or metadata.get("artworkUrl"),
        "fingerprintHash": row.fingerprint_hash,
        "fingerprintVersion": metadata.get("fingerprintVersion") or metadata.get("fingerprint_version"),
        "metadataSource": source,
        "metadataConfidence": confidence if confidence is not None else row.confidence,
    }


def upsert_provider_metadata(
    db: Session,
    provider: str,
    provider_media_id: str,
    origin_url: str,
    metadata: dict[str, Any],
    fingerprint_hash: str | None = None,
) -> None:
    normalized_provider = provider.strip().lower()
    media_id = provider_media_id.strip()
    if not normalized_provider or not media_id:
        return

    now = datetime.utcnow()
    metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    cache_row = (
        db.query(AudioMetadataCache)
        .filter(AudioMetadataCache.provider == normalized_provider)
        .filter(AudioMetadataCache.provider_media_id == media_id)
        .first()
    )
    if cache_row:
        cache_row.origin_url = origin_url or cache_row.origin_url
        cache_row.metadata_json = merge_json(cache_row.metadata_json, metadata)
        cache_row.updated_at = now
    else:
        db.add(
            AudioMetadataCache(
                provider=normalized_provider,
                provider_media_id=media_id,
                origin_url=origin_url,
                metadata_json=metadata_json,
                created_at=now,
                updated_at=now,
            )
        )

    alias = (
        db.query(TrackProviderAlias)
        .filter(TrackProviderAlias.provider == normalized_provider)
        .filter(TrackProviderAlias.provider_media_id == media_id)
        .first()
    )
    if alias:
        alias.origin_url = origin_url or alias.origin_url
        alias.fingerprint_hash = fingerprint_hash or alias.fingerprint_hash
        alias.metadata_json = merge_json(alias.metadata_json, metadata)
        alias.updated_at = now
    else:
        db.add(
            TrackProviderAlias(
                provider=normalized_provider,
                provider_media_id=media_id,
                origin_url=origin_url,
                fingerprint_hash=fingerprint_hash or "",
                metadata_json=metadata_json,
                created_at=now,
                updated_at=now,
            )
        )


def submit_track_metadata(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    fingerprint_hash = str(payload.get("fingerprintHash") or payload.get("fingerprint_hash") or "").strip()
    provider = str(payload.get("provider") or "").strip().lower()
    provider_media_id = str(payload.get("providerMediaId") or payload.get("provider_media_id") or payload.get("id") or "").strip()
    origin_url = str(payload.get("url") or payload.get("originUrl") or payload.get("origin_url") or "").strip()
    metadata = normalized_metadata(payload)

    if not fingerprint_hash and not (provider and provider_media_id):
        raise ValueError("fingerprintHash or provider/providerMediaId is required")

    if fingerprint_hash:
        upsert_fingerprint_metadata(db, fingerprint_hash, metadata)
    if provider and provider_media_id:
        upsert_provider_metadata(db, provider, provider_media_id, origin_url, metadata, fingerprint_hash or None)
    db.commit()
    return {"status": "ok", "fingerprintHash": fingerprint_hash or None, "provider": provider or None, "providerMediaId": provider_media_id or None}


def upsert_fingerprint_metadata(db: Session, fingerprint_hash: str, metadata: dict[str, Any]) -> TrackFingerprintCache:
    now = datetime.utcnow()
    row = db.query(TrackFingerprintCache).filter(TrackFingerprintCache.fingerprint_hash == fingerprint_hash).first()
    if row:
        row.title = str(metadata.get("title") or row.title or "")
        row.artist = str(metadata.get("artist") or row.artist or "")
        row.album = str(metadata.get("album") or row.album or "")
        row.genre = str(metadata.get("genre") or row.genre or "")
        row.bpm = number_or_none(metadata.get("bpm")) if metadata.get("bpm") is not None else row.bpm
        row.musical_key = str(metadata.get("key") or row.musical_key or "")
        row.lufs = number_or_none(metadata.get("lufs")) if metadata.get("lufs") is not None else row.lufs
        row.duration = number_or_none(metadata.get("duration")) if metadata.get("duration") is not None else row.duration
        row.sample_rate = int(number_or_none(metadata.get("sampleRate") or metadata.get("sample_rate")) or row.sample_rate or 0)
        row.bitrate = int(number_or_none(metadata.get("bitrate")) or row.bitrate or 0)
        row.artwork_url = str(metadata.get("thumbnail") or metadata.get("artworkUrl") or row.artwork_url or "")
        row.confidence = max(row.confidence or 0, float(number_or_none(metadata.get("metadataConfidence")) or 0.75))
        row.source_count += 1
        row.metadata_json = merge_json(row.metadata_json, metadata)
        row.updated_at = now
        return row

    row = TrackFingerprintCache(
        fingerprint_hash=fingerprint_hash,
        title=str(metadata.get("title") or ""),
        artist=str(metadata.get("artist") or ""),
        album=str(metadata.get("album") or ""),
        genre=str(metadata.get("genre") or ""),
        bpm=number_or_none(metadata.get("bpm")),
        musical_key=str(metadata.get("key") or ""),
        lufs=number_or_none(metadata.get("lufs")),
        duration=number_or_none(metadata.get("duration")),
        sample_rate=int(number_or_none(metadata.get("sampleRate") or metadata.get("sample_rate")) or 0),
        bitrate=int(number_or_none(metadata.get("bitrate")) or 0),
        artwork_url=str(metadata.get("thumbnail") or metadata.get("artworkUrl") or ""),
        confidence=float(number_or_none(metadata.get("metadataConfidence")) or 0.75),
        source_count=1,
        metadata_json=json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    return row


def lookup_track_metadata(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    provider = str(payload.get("provider") or "").strip().lower()
    provider_media_id = str(payload.get("providerMediaId") or payload.get("provider_media_id") or payload.get("id") or "").strip()
    fingerprint_hash = str(payload.get("fingerprintHash") or payload.get("fingerprint_hash") or "").strip()

    if provider and provider_media_id:
        alias = (
            db.query(TrackProviderAlias)
            .filter(TrackProviderAlias.provider == provider)
            .filter(TrackProviderAlias.provider_media_id == provider_media_id)
            .first()
        )
        if alias and alias.fingerprint_hash:
            row = db.query(TrackFingerprintCache).filter(TrackFingerprintCache.fingerprint_hash == alias.fingerprint_hash).first()
            if row:
                return {"matched": True, "matchType": "provider-fingerprint", "metadata": metadata_from_row(row, "provider-fingerprint", 0.95)}
        cache_row = (
            db.query(AudioMetadataCache)
            .filter(AudioMetadataCache.provider == provider)
            .filter(AudioMetadataCache.provider_media_id == provider_media_id)
            .first()
        )
        if cache_row:
            metadata = safe_json(cache_row.metadata_json)
            return {"matched": True, "matchType": "provider", "metadata": {**metadata, "metadataSource": "provider", "metadataConfidence": 0.82}}

    if fingerprint_hash:
        row = db.query(TrackFingerprintCache).filter(TrackFingerprintCache.fingerprint_hash == fingerprint_hash).first()
        if row:
            return {"matched": True, "matchType": "fingerprint", "metadata": metadata_from_row(row, "fingerprint", 0.98)}

    title = clean_text(str(payload.get("title") or ""))
    artist = clean_text(str(payload.get("artist") or ""))
    duration = number_or_none(payload.get("duration"))
    if title:
        candidate = best_soft_match(db, title, artist, duration)
        if candidate:
            return {"matched": True, "matchType": "soft", "metadata": metadata_from_row(candidate, "soft", 0.62)}

    return {"matched": False, "metadata": None}


def enrich_items_with_cached_metadata(db: Session, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return items
    enriched: list[dict[str, Any]] = []
    for item in items:
        provider = str(item.get("provider") or "youtube").lower()
        media_id = str(item.get("id") or "").strip()
        if item.get("kind") in {"album", "playlist", "container"} or not media_id:
            enriched.append(item)
            continue
        lookup = lookup_track_metadata(db, {
            "provider": provider,
            "providerMediaId": media_id,
            "title": item.get("title"),
            "artist": item.get("artist"),
            "duration": item.get("duration"),
        })
        metadata = lookup.get("metadata") if lookup.get("matched") else None
        if isinstance(metadata, dict):
            enriched.append(merge_track_item(item, metadata))
        else:
            enriched.append(item)
    return enriched


def merge_track_item(item: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    next_item = {**item}
    for key in ("genre", "bpm", "key", "lufs", "sampleRate", "bitrate", "fingerprintHash", "fingerprintVersion", "metadataSource", "metadataConfidence"):
        value = metadata.get(key)
        if value not in (None, ""):
            next_item[key] = value
    if not next_item.get("thumbnail") and metadata.get("thumbnail"):
        next_item["thumbnail"] = metadata["thumbnail"]
    return next_item


def best_soft_match(db: Session, title: str, artist: str, duration: float | None) -> TrackFingerprintCache | None:
    rows = db.query(TrackFingerprintCache).order_by(TrackFingerprintCache.updated_at.desc()).limit(500).all()
    best: tuple[float, TrackFingerprintCache] | None = None
    for row in rows:
        score = 0.0
        if title and clean_text(row.title) == title:
            score += 0.55
        elif title and (title in clean_text(row.title) or clean_text(row.title) in title):
            score += 0.35
        if artist and clean_text(row.artist) == artist:
            score += 0.25
        elif artist and (artist in clean_text(row.artist) or clean_text(row.artist) in artist):
            score += 0.12
        if duration and row.duration:
            delta = abs(float(row.duration) - float(duration))
            if delta <= 1.5:
                score += 0.2
            elif delta <= 5:
                score += 0.1
        if score >= 0.55 and (best is None or score > best[0]):
            best = (score, row)
    return best[1] if best else None


def normalized_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    merged = {**metadata}
    for key in KNOWN_METADATA_KEYS:
        if key in payload and payload[key] not in (None, ""):
            merged[key] = payload[key]
    return merged


def safe_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def merge_json(existing_json: str, patch: dict[str, Any]) -> str:
    merged = {**safe_json(existing_json), **{key: value for key, value in patch.items() if value not in (None, "")}}
    return json.dumps(merged, ensure_ascii=False, separators=(",", ":"))


def number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None
