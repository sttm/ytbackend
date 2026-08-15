from functools import lru_cache
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PRODUCERSCENTER_BACKEND_",
        extra="ignore",
    )

    name: str = "ProducersCenter Backend"
    version: str = "0.1.0"
    # Production must fail closed unless an explicit development environment
    # opts into debug behaviour.
    debug: bool = False
    database_url: str = Field(
        "sqlite:///./storage/backend.db",
        # The backend-specific setting is intentional: it must win over a
        # generic/stale DATABASE_URL inherited by a Render service.
        validation_alias=AliasChoices("PRODUCERSCENTER_BACKEND_DATABASE_URL", "POSTGRES_DB_URL", "DATABASE_URL"),
    )
    api_key: str = ""
    # Browser-only dashboard access. This is intentionally separate from the
    # gateway-to-resolver API key and is never sent to frontend JavaScript.
    dashboard_password: str = ""
    dashboard_session_ttl_seconds: int = 43_200
    cors_origins: str = "http://localhost:3000,http://localhost:8787"
    proxy_check_concurrency: int = 30
    stream_resolve_concurrency: int = 4
    proxy_attempts: int = 3
    # Googlevideo URLs are short-lived. Resolve directly first so the PWA can
    # play client-to-CDN without proxying media through Render.
    stream_cache_hours: float = 0.25
    # A signed URL's own `expire` value is authoritative. This is only a hard
    # upper bound for URLs that do not expose one; it is not a promise that a
    # YouTube URL will remain usable for six hours.
    stream_cache_max_hours: float = 6.0
    stream_cache_expiry_safety_seconds: int = 120
    # A cached URL obtained through a proxy is reusable only while that exact
    # proxy has a recent successful YouTube/media check.
    proxy_cache_health_seconds: int = 300
    # A direct URL was resolved from a Render egress but is consumed by a PWA
    # browser from another egress. Keep this off by default.
    stream_cache_reuse_direct: bool = False
    direct_first: bool = True
    search_timeout_seconds: int = 15
    stream_resolve_timeout_seconds: int = 35
    # The playback fallback must fail fast. The PWA tries the direct CDN URL
    # first, so a proxy path is recovery rather than the normal data plane.
    playback_resolve_timeout_seconds: int = 15
    playback_proxy_attempts: int = 1
    playback_proxy_connect_timeout_seconds: int = 6
    playback_proxy_read_timeout_seconds: int = 15
    ytdlp_socket_timeout_seconds: int = 8
    ytdlp_retries: int = 0
    max_concurrent_requests: int = 4


@lru_cache
def get_settings() -> Settings:
    return Settings()
