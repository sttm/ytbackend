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
Build Command: pip install -r requirements.txt
Start Command: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Environment:

```env
DATABASE_URL=<Render internal PostgreSQL URL>
PRODUCERSCENTER_BACKEND_CORS_ORIGINS=http://localhost:8787,https://producerscenter.app
```

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

Dashboard:

```txt
http://localhost:8010/dashboard
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

When `check_before_add` is enabled, the backend checks each proxy before saving it.
Dead proxies are skipped, duplicates are counted, and only verified proxies are stored.

Proxy checks use three layers:

1. HTTP ping through proxy.
2. YouTube reachability through proxy.
3. `yt-dlp` extraction through proxy.

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
