from fastapi import APIRouter

from app.config import get_settings

router = APIRouter()


@router.get("/api/health")
def health():
    settings = get_settings()
    return {
        "ok": True,
        "service": settings.name,
        "version": settings.version,
    }

