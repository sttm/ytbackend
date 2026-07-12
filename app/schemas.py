from datetime import datetime
from pydantic import BaseModel, Field


class ProxyImportRequest(BaseModel):
	proxies: list[str] = Field(default_factory=list)
	source: str = "manual"
	protocol: str = "auto"
	check_before_add: bool = False
	check_mode: str = "fast"
	check_limit: int = 100


class ProxyUrlImportRequest(BaseModel):
	url: str
	source: str | None = None
	protocol: str = "auto"
	check_before_add: bool = True
	check_mode: str = "fast"
	check_limit: int = 100


class ProxySourceCreate(BaseModel):
    name: str
    url: str
    protocol: str = "auto"


class ProxyCheckRequest(BaseModel):
    proxy: str


class StreamResponse(BaseModel):
    status: str = "success"
    cached: bool
    video_id: str | None
    title: str | None
    uploader: str | None
    duration: int | None
    thumbnail: str | None
    stream_url: str
    format_id: str | None
    audio_codec: str | None
    ext: str | None
    bitrate: float | None
    sample_rate: int | None
    filesize: int | None
    proxy_used: str | None


class YoutubeUrlRequest(BaseModel):
    url: str
    use_proxy: bool = True
    force_refresh: bool = False


class YoutubeSearchRequest(BaseModel):
    query: str
    limit: int = 10


class YoutubePlaylistRequest(BaseModel):
    url: str
    limit: int = 100


class ProxyCheckResult(BaseModel):
    proxy_url: str
    status: str
    layer: str
    latency_ms: int = 0
    error: str = ""
    checked_at: datetime
