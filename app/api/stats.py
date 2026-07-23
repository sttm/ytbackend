from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AudioMetadataCache, Proxy, SearchQueryCache, StreamCache

router = APIRouter()


@router.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    total = db.query(Proxy).count()
    verified = db.query(Proxy).filter(Proxy.is_verified == True).count()  # noqa: E712
    active = db.query(Proxy).filter(Proxy.is_active == True).count()  # noqa: E712
    blocked = db.query(Proxy).filter(Proxy.status.in_(["youtube_blocked", "captcha"])).count()
    dead = db.query(Proxy).filter(Proxy.is_active == False).count()  # noqa: E712
    cached = db.query(StreamCache).count()
    metadata_cached = db.query(AudioMetadataCache).count()
    search_queries_cached = db.query(SearchQueryCache).count()
    avg_latency = db.query(func.avg(Proxy.latency_ms)).filter(Proxy.is_verified == True).scalar()  # noqa: E712
    return {
        "proxies": {
            "total": total,
            "active": active,
            "verified": verified,
            "blocked": blocked,
            "dead": dead,
            "avg_latency_ms": round(avg_latency or 0),
        },
        "streams": {
            "cached": cached,
        },
        "search_cache": {
            "metadata": metadata_cached,
            "queries": search_queries_cached,
        },
    }
