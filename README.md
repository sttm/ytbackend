# ProducersCenter Backend

FastAPI backend for YouTube audio stream URL resolution and proxy health management.

## Run locally

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8010
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
POST /api/proxies/{id}/check
POST /api/proxies/check-batch?limit=20&status=new
GET  /api/proxy-sources
POST /api/proxy-sources/defaults
POST /api/proxy-sources/fetch
```

Proxy checks use three layers:

1. HTTP ping through proxy.
2. YouTube reachability through proxy.
3. `yt-dlp` extraction through proxy.
