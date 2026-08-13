from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.api.media import router as media_router
from app.api.metadata import router as metadata_router
from app.api.proxies import router as proxies_router
from app.api.stats import router as stats_router
from app.api.streams import router as streams_router
from app.api.tracks import router as tracks_router
from app.config import get_settings
from app.database import init_db
from app.security import require_api_key
from app.services.capacity import ResolverCapacityMiddleware, capacity

settings = get_settings()
static_dir = Path(__file__).resolve().parent / "static"

init_db()

app = FastAPI(title=settings.name, version=settings.version)
app.add_middleware(ResolverCapacityMiddleware, capacity=capacity)

origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
private_router_options = {"dependencies": [Depends(require_api_key)]}
app.include_router(stats_router, **private_router_options)
app.include_router(proxies_router, **private_router_options)
app.include_router(streams_router, **private_router_options)
app.include_router(tracks_router, **private_router_options)
app.include_router(metadata_router, **private_router_options)
app.include_router(media_router, **private_router_options)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def root():
    return {
        "service": settings.name,
        "version": settings.version,
        "dashboard": "/dashboard",
    }


@app.get("/dashboard", dependencies=[Depends(require_api_key)])
def dashboard():
    return FileResponse(static_dir / "index.html")
