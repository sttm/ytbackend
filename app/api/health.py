from fastapi import APIRouter

from app.config import get_settings
from app.database import check_db

router = APIRouter()


@router.get("/api/health")
def health():
    settings = get_settings()
    return {
        "ok": True,
        "service": settings.name,
        "version": settings.version,
    }


@router.get("/api/health/db")
def health_db():
    return check_db()
