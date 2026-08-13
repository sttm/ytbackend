from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import check_db
from app.security import require_api_key
from app.services.capacity import capacity

router = APIRouter()


@router.get("/api/health")
@router.get("/health")
def health():
    settings = get_settings()
    snapshot = capacity.snapshot()
    return JSONResponse({
        "ok": snapshot["available"],
        "service": settings.name,
        "version": settings.version,
        "capacity": snapshot,
    }, status_code=200 if snapshot["available"] else 503, headers={"Cache-Control": "no-store"})


@router.get("/api/health/db")
def health_db(_authenticated: None = Depends(require_api_key)):
    return check_db()
