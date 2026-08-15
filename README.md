# ProducersCenter Backend

FastAPI backend for YouTube audio stream URL resolution and proxy health management.

Search architecture:

- YouTube Music catalog search uses `ytmusicapi` and returns song/video IDs from YouTube Music.
- YouTube all-video search and SoundCloud search use `yt-dlp` search prefixes.
- Playback, stream URL resolving and offline downloads use `yt-dlp` against the selected item URL.
- Search metadata is cached in two layers:
  - `audio_metadata_cache`: unique track metadata by `provider + provider_media_id`.
  - `search_queries_cache`: normalized query strings by `provider + mode + search_query`, storing ordered result IDs.
- The backend never stores cached direct `stream_url` in the search cache. Stream URLs stay fresh and are resolved separately.

## Run locally

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8010
```

PostgreSQL local database:

```bash
cd backend
docker compose up -d postgres
cp .env.example .env
# .env default points at localhost:5432
./run.sh
```

The app accepts both `postgres://...` and `postgresql://...` URLs and normalizes them to the `psycopg` SQLAlchemy driver. On first boot it creates the current tables automatically.
It reads both `PRODUCERSCENTER_BACKEND_DATABASE_URL` and Render's standard `DATABASE_URL`.

Render PostgreSQL:

```txt
# Recommended: set Environment to Docker and let Render use backend/Dockerfile.
# It installs ffmpeg, Deno and yt-dlp-ejs reproducibly.

# If this existing Render service uses Native Runtime instead:
Build Command: bash render-build.sh
Start Command: bash render-start.sh
```

### Deno and YouTube JavaScript challenges

The backend installs `yt-dlp-ejs` and Deno (2.3+; Docker uses a pinned 2.9.4
binary). This lets yt-dlp execute the current YouTube player JavaScript needed
to resolve signatures and `n` parameters. Deno is selected by yt-dlp by default
when it is available on `PATH`.

It improves normal stream resolution and downloading, but it is **not** a bot
restriction bypass: a Render datacenter IP can still receive a sign-in, CAPTCHA
or rate-limit response. Keep using the verified proxy pool and direct-first
playback; do not put browser cookies or a user API key into the PWA.

Environment:

```env
# Use the same pooled PostgreSQL/Neon URL on every resolver node. The backend
# creates its proxy/cache tables automatically on first boot.
PRODUCERSCENTER_BACKEND_DATABASE_URL=<shared pooled PostgreSQL URL>
PRODUCERSCENTER_BACKEND_CORS_ORIGINS=http://localhost:8787,https://producerscenter.app
PRODUCERSCENTER_BACKEND_DIRECT_FIRST=true
PRODUCERSCENTER_BACKEND_STREAM_RESOLVE_CONCURRENCY=4
PRODUCERSCENTER_BACKEND_PROXY_ATTEMPTS=3
# Temporary Googlevideo URLs are cached for 15 minutes by default.
PRODUCERSCENTER_BACKEND_STREAM_CACHE_HOURS=0.25
# Maximum permitted cache age for a URL without a signed `expire` parameter.
# Googlevideo's own expiry takes precedence. Direct cached links are disabled
# by default because PWA and Render use different egress IP addresses.
PRODUCERSCENTER_BACKEND_STREAM_CACHE_MAX_HOURS=6
PRODUCERSCENTER_BACKEND_STREAM_CACHE_EXPIRY_SAFETY_SECONDS=120
PRODUCERSCENTER_BACKEND_PROXY_CACHE_HEALTH_SECONDS=300
PRODUCERSCENTER_BACKEND_STREAM_CACHE_REUSE_DIRECT=false
PRODUCERSCENTER_BACKEND_STREAM_RESOLVE_TIMEOUT_SECONDS=35
# Fast path used by PWA playback. A proxy fallback is one short attempt; it
# must not hold track switching for the full admin-resolution timeout.
PRODUCERSCENTER_BACKEND_PLAYBACK_RESOLVE_TIMEOUT_SECONDS=15
PRODUCERSCENTER_BACKEND_PLAYBACK_PROXY_ATTEMPTS=1
PRODUCERSCENTER_BACKEND_PLAYBACK_PROXY_CONNECT_TIMEOUT_SECONDS=6
PRODUCERSCENTER_BACKEND_PLAYBACK_PROXY_READ_TIMEOUT_SECONDS=15
PRODUCERSCENTER_BACKEND_YTDLP_SOCKET_TIMEOUT_SECONDS=8
PRODUCERSCENTER_BACKEND_YTDLP_RETRIES=0
```

## Deployment security

All resolver, proxy, catalogue, media and database-health endpoints require
`Authorization: Bearer <PRODUCERSCENTER_BACKEND_API_KEY>` from the Gateway.
The browser dashboard uses its own password and an HttpOnly session cookie.
Only `GET /api/health` remains public for monitoring. Set a
long random `PRODUCERSCENTER_BACKEND_API_KEY` in every Render service and set
that node token only in the server-side Resolver Gateway pool; never expose it
through a `NEXT_PUBLIC_*` variable.

Set a different `PRODUCERSCENTER_BACKEND_DASHBOARD_PASSWORD` in every Render
service to enable `/dashboard/login`. It must not be a node API key, Gateway
key, registry key or any other shared secret. The login creates a 12-hour
HttpOnly, `SameSite=Strict` session; use **Sign out** when finished.

Why Render can behave differently from a local backend:

- YouTube signs stream URLs for the resolver/proxy network path. A URL resolved from a Render egress IP is not equivalent to one resolved from your Mac.
- Render datacenter IPs are more likely to hit YouTube bot/sign-in checks than residential/local traffic.
- Public proxies that work from your Mac can be blocked, slow, or TLS-flaky from Render's network.
- `yt-dlp` extraction is blocking work. Keep `PROXY_ATTEMPTS * YTDLP_SOCKET_TIMEOUT_SECONDS` below `STREAM_RESOLVE_TIMEOUT_SECONDS`.
- For normal PWA playback, prefer direct-first (`DIRECT_FIRST=true`): Render only resolves a temporary URL and the browser streams from the CDN. Proxy playback remains a fallback when a direct URL is rejected. The dashboard's **deep** proxy check resolves a test audio URL and reads up to 256 KiB through that proxy.

Database check:

```bash
python -m app.manage init-db
python -m app.manage check-db
curl https://<backend-host>/api/health/db
```

SQLite remains available only as a local fallback:

```env
PRODUCERSCENTER_BACKEND_DATABASE_URL=sqlite:///./storage/backend.db
```

Or:

```bash
./run.sh
```

Dashboard login:

```txt
http://localhost:8010/dashboard/login
```

Stream API:

```txt
GET /api/stream?url=https://youtu.be/57Ykv1D0qEE
```

## Main endpoints

```txt
GET  /api/health
GET  /api/stats
GET  /api/stream?url=<youtube_url>&use_proxy=true
GET  /api/proxies
GET  /api/proxies/top
POST /api/proxies/import
POST /api/proxies/import-url
POST /api/proxies/{id}/check
POST /api/proxies/check-batch?limit=20&status=new
GET  /api/client-proxies?format=json&limit=100
GET  /api/client-proxies?format=txt&limit=100
GET  /api/proxy-sources
POST /api/proxy-sources/defaults
POST /api/proxy-sources/fetch
```

`/api/proxies/import` and `/api/proxies/import-url` support:

```json
{
  "check_before_add": true,
  "check_limit": 100
}
```

Every new proxy is saved immediately with status `new`, before its check begins.
Each completed check is then written independently; an interrupted large import
therefore leaves resumable `new` records instead of losing its whole result.
Existing normalized proxy URLs are skipped without resetting their health history;
the response reports them as `skipped_existing`. Failed checks are retained with
their status/error for diagnostics, while only verified active proxies are used
for stream resolving.

Proxy checks use three layers:

1. HTTP ping through proxy.
2. YouTube reachability through proxy.
3. `yt-dlp` extraction and a small audio sample download through the same proxy.

## Resilient MVP checklist

Backend MVP is considered usable only when these flows are stable and bounded by timeouts:

- YouTube direct stream URL: `GET /api/stream?url=<youtube-url>`
- SoundCloud direct stream URL: `GET /api/stream?url=<soundcloud-url>`
- Browser playback proxy: `GET /api/playback?url=<youtube-or-soundcloud-url>`
- Offline download proxy: `POST /download` with `{ "url": "..." }`
- Search modes:
  - `POST /search` with `{ "query": "...", "mode": "youtube-all" }`
  - `POST /search` with `{ "query": "...", "mode": "youtube-music" }` through `ytmusicapi`
  - `POST /search` with `{ "query": "...", "mode": "soundcloud", "source": "soundcloud" }`
- Dashboard must remain responsive while search/stream/download requests are running.

Runtime guardrails:

- Search and playlist extraction run in a worker thread and time out via `PRODUCERSCENTER_BACKEND_SEARCH_TIMEOUT_SECONDS`.
- Stream resolution runs in a worker thread and times out via `PRODUCERSCENTER_BACKEND_STREAM_RESOLVE_TIMEOUT_SECONDS`.
- Search does not verify every SoundCloud result with full extraction. Play/download verifies the selected item instead.
