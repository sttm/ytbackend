from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PRODUCERSCENTER_BACKEND_",
        extra="ignore",
    )

    name: str = "ProducersCenter Backend"
    version: str = "0.1.0"
    debug: bool = True
    database_url: str = "sqlite:///./storage/backend.db"
    api_key: str = ""
    cors_origins: str = "http://localhost:3000,http://localhost:8787"
    proxy_check_concurrency: int = 30
    stream_resolve_concurrency: int = 1
    proxy_attempts: int = 8
    stream_cache_hours: int = 6
    direct_first: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
