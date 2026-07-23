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
    stream_url: str | None = None
    title: str | None = None
    artist: str | None = None
    video_id: str | None = None
    ext: str | None = None
    mime_type: str | None = None
    filesize: int | None = None


class YoutubeSearchRequest(BaseModel):
    query: str
    limit: int = 40
    mode: str = "youtube-music"
    source: str | None = None
    filter: str | None = None


class YoutubePlaylistRequest(BaseModel):
    url: str
    limit: int = 100


class TrackUsageRequest(BaseModel):
    provider: str
    id: str
    url: str = ""
    action: str = "play"
    metadata: dict = Field(default_factory=dict)


class TrackMetadataLookupRequest(BaseModel):
    fingerprintHash: str | None = None
    provider: str | None = None
    providerMediaId: str | None = None
    id: str | None = None
    title: str | None = None
    artist: str | None = None
    duration: float | None = None


class TrackMetadataSubmitRequest(BaseModel):
    fingerprintHash: str | None = None
    fingerprintVersion: str | None = None
    chromaprintFingerprint: str | None = None
    provider: str | None = None
    providerMediaId: str | None = None
    id: str | None = None
    url: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    genreConfidence: float | None = None
    genreModel: str | None = None
    genreTags: list[dict] | None = None
    bpm: float | None = None
    key: str | None = None
    lufs: float | None = None
    duration: float | None = None
    sampleRate: int | None = None
    bitrate: int | None = None
    thumbnail: str | None = None
    metadata: dict = Field(default_factory=dict)


class ProxyCheckResult(BaseModel):
    proxy_url: str
    status: str
    layer: str
    latency_ms: int = 0
    error: str = ""
    checked_at: datetime
