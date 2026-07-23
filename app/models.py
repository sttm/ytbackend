from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Proxy(Base):
    __tablename__ = "proxies"
    __table_args__ = (UniqueConstraint("proxy_url", name="uq_proxy_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proxy_url: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    protocol: Mapped[str] = mapped_column(String(24), default="http", index=True)
    host: Mapped[str] = mapped_column(String(255), default="")
    port: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(255), default="manual")

    status: Mapped[str] = mapped_column(String(48), default="new", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    download_ms: Mapped[int] = mapped_column(Integer, default=0)

    success_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    youtube_success: Mapped[int] = mapped_column(Integer, default=0)
    youtube_fail: Mapped[int] = mapped_column(Integer, default=0)
    bot_block_count: Mapped[int] = mapped_column(Integer, default=0)
    timeout_count: Mapped[int] = mapped_column(Integer, default=0)

    last_error: Mapped[str] = mapped_column(Text, default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProxySource(Base):
    __tablename__ = "proxy_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    protocol: Mapped[str] = mapped_column(String(24), default="auto")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StreamCache(Base):
    __tablename__ = "stream_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(String(128), index=True)
    youtube_url: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(512), default="")
    uploader: Mapped[str] = mapped_column(String(512), default="")
    duration: Mapped[int] = mapped_column(Integer, default=0)
    thumbnail: Mapped[str] = mapped_column(String(1024), default="")
    stream_url: Mapped[str] = mapped_column(Text)
    format_id: Mapped[str] = mapped_column(String(64), default="")
    audio_codec: Mapped[str] = mapped_column(String(64), default="")
    ext: Mapped[str] = mapped_column(String(32), default="")
    bitrate: Mapped[float] = mapped_column(Float, default=0)
    sample_rate: Mapped[int] = mapped_column(Integer, default=0)
    filesize: Mapped[int] = mapped_column(Integer, default=0)
    proxy_used: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AudioMetadataCache(Base):
    __tablename__ = "audio_metadata_cache"
    __table_args__ = (UniqueConstraint("provider", "provider_media_id", name="uq_audio_metadata_provider_media_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_media_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    origin_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SearchQueryCache(Base):
    __tablename__ = "search_queries_cache"
    __table_args__ = (UniqueConstraint("provider", "mode", "search_query", name="uq_search_query_provider_mode_query"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    search_query: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    result_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TrackUsageEvent(Base):
    __tablename__ = "track_usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_media_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    origin_url: Mapped[str] = mapped_column(String(1024), default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class TrackFingerprintCache(Base):
    __tablename__ = "track_fingerprint_cache"
    __table_args__ = (UniqueConstraint("fingerprint_hash", name="uq_track_fingerprint_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    artist: Mapped[str] = mapped_column(String(512), default="")
    album: Mapped[str] = mapped_column(String(512), default="")
    genre: Mapped[str] = mapped_column(String(255), default="")
    bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    musical_key: Mapped[str] = mapped_column(String(64), default="")
    lufs: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_rate: Mapped[int] = mapped_column(Integer, default=0)
    bitrate: Mapped[int] = mapped_column(Integer, default=0)
    artwork_url: Mapped[str] = mapped_column(String(1024), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.75)
    source_count: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class TrackProviderAlias(Base):
    __tablename__ = "track_provider_aliases"
    __table_args__ = (UniqueConstraint("provider", "provider_media_id", name="uq_track_provider_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_media_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    origin_url: Mapped[str] = mapped_column(String(1024), default="")
    fingerprint_hash: Mapped[str] = mapped_column(String(128), default="", index=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
