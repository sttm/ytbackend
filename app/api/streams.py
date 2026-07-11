from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.stream_service import resolve_stream

router = APIRouter()


@router.get("/api/stream")
def stream(
    url: str = Query(...),
    use_proxy: bool = True,
    force_refresh: bool = False,
    db: Session = Depends(get_db),
):
    try:
        return {
            "status": "success",
            **resolve_stream(db, url, use_proxy=use_proxy, force_refresh=force_refresh),
        }
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

