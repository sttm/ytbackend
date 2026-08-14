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
        validation_alias=AliasChoices("POSTGRES_DB_URL", "PRODUCERSCENTER_BACKEND_DATABASE_URL", "DATABASE_URL"),
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
    direct_first: bool = True
    search_timeout_seconds: int = 15
    stream_resolve_timeout_seconds: int = 35
    ytdlp_socket_timeout_seconds: int = 8
    ytdlp_retries: int = 0
    max_concurrent_requests: int = 4


@lru_cache
def get_settings() -> Settings:
    return Settings()
