from __future__ import annotations

import asyncio
import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Proxy, ProxySource
from app.schemas import ProxyCheckRequest, ProxyImportRequest, ProxySourceCreate
from app.services.proxy_checker import check_proxy
from app.services.proxy_store import apply_check_result, best_proxies, upsert_proxy

router = APIRouter()


def serialize_proxy(row: Proxy) -> dict:
    return {
        "id": row.id,
        "proxy_url": row.proxy_url,
        "protocol": row.protocol,
        "host": row.host,
        "port": row.port,
        "source": row.source,
        "status": row.status,
        "is_active": row.is_active,
        "is_verified": row.is_verified,
        "score": row.score,
        "latency_ms": row.latency_ms,
        "success_count": row.success_count,
        "fail_count": row.fail_count,
        "youtube_success": row.youtube_success,
        "youtube_fail": row.youtube_fail,
        "bot_block_count": row.bot_block_count,
        "last_error": row.last_error,
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "cooldown_until": row.cooldown_until.isoformat() if row.cooldown_until else None,
    }


@router.get("/api/proxies")
def list_proxies(
    status: str | None = None,
    verified: bool | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(Proxy)
    if status:
        query = query.filter(Proxy.status == status)
    if verified is not None:
        query = query.filter(Proxy.is_verified == verified)
    total = query.count()
    rows = query.order_by(Proxy.score.desc(), Proxy.latency_ms.asc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "proxies": [serialize_proxy(row) for row in rows],
    }


@router.get("/api/proxies/top")
def top_proxies(limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)):
    return {"proxies": [serialize_proxy(row) for row in best_proxies(db, limit)]}


@router.post("/api/proxies/import")
def import_proxies(payload: ProxyImportRequest, db: Session = Depends(get_db)):
    created = 0
    updated = 0
    errors: list[str] = []
    for item in payload.proxies:
        try:
            _, was_created = upsert_proxy(db, item, payload.source, payload.protocol)
            if was_created:
                created += 1
            else:
                updated += 1
        except Exception as error:
            errors.append(f"{item}: {error}")
    return {
        "created": created,
        "updated": updated,
        "errors": errors[:20],
    }


@router.post("/api/proxies/check")
async def check_raw_proxy(payload: ProxyCheckRequest, db: Session = Depends(get_db)):
    row, _ = upsert_proxy(db, payload.proxy)
    result = await check_proxy(row.proxy_url)
    row = apply_check_result(db, row, result)
    return {
        "result": result,
        "proxy": serialize_proxy(row),
    }


@router.post("/api/proxies/{proxy_id}/check")
async def check_proxy_by_id(proxy_id: int, db: Session = Depends(get_db)):
    row = db.query(Proxy).filter(Proxy.id == proxy_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="proxy not found")
    result = await check_proxy(row.proxy_url)
    row = apply_check_result(db, row, result)
    return {
        "result": result,
        "proxy": serialize_proxy(row),
    }


@router.post("/api/proxies/check-batch")
async def check_batch(
    limit: int = Query(20, ge=1, le=200),
    status: str = "new",
    db: Session = Depends(get_db),
):
    rows = (
        db.query(Proxy)
        .filter(Proxy.status == status)
        .order_by(Proxy.created_at.asc())
        .limit(limit)
        .all()
    )

    async def worker(row: Proxy):
        result = await check_proxy(row.proxy_url)
        return row.id, result

    results = await asyncio.gather(*(worker(row) for row in rows))
    output = []
    for proxy_id, result in results:
        row = db.query(Proxy).filter(Proxy.id == proxy_id).first()
        if row:
            output.append(serialize_proxy(apply_check_result(db, row, result)))
    return {
        "processed": len(output),
        "proxies": output,
    }


@router.delete("/api/proxies/{proxy_id}")
def delete_proxy(proxy_id: int, db: Session = Depends(get_db)):
    row = db.query(Proxy).filter(Proxy.id == proxy_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="proxy not found")
    db.delete(row)
    db.commit()
    return {"deleted": True}


@router.get("/api/proxy-sources")
def list_sources(db: Session = Depends(get_db)):
    rows = db.query(ProxySource).order_by(ProxySource.created_at.desc()).all()
    return {
        "sources": [
            {
                "id": row.id,
                "name": row.name,
                "url": row.url,
                "protocol": row.protocol,
                "enabled": row.enabled,
            }
            for row in rows
        ]
    }


@router.post("/api/proxy-sources")
def add_source(payload: ProxySourceCreate, db: Session = Depends(get_db)):
    row = db.query(ProxySource).filter(ProxySource.url == payload.url).first()
    if not row:
        row = ProxySource(name=payload.name, url=payload.url, protocol=payload.protocol)
        db.add(row)
    else:
        row.name = payload.name
        row.protocol = payload.protocol
        row.enabled = True
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "url": row.url}


@router.post("/api/proxy-sources/fetch")
async def fetch_sources(db: Session = Depends(get_db)):
    sources = db.query(ProxySource).filter(ProxySource.enabled == True).all()  # noqa: E712
    imported = 0
    errors: list[str] = []
    async with aiohttp.ClientSession() as session:
        for source in sources:
            try:
                async with session.get(source.url, timeout=20) as response:
                    text = await response.text()
                proxies = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
                for proxy in proxies:
                    upsert_proxy(db, proxy, source.name, source.protocol)
                    imported += 1
            except Exception as error:
                errors.append(f"{source.name}: {error}")
    return {
        "sources": len(sources),
        "imported": imported,
        "errors": errors,
    }


@router.post("/api/proxy-sources/defaults")
def add_default_sources(db: Session = Depends(get_db)):
    defaults = [
        ("SpeedX SOCKS5", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt", "socks5"),
        ("SpeedX SOCKS4", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt", "socks4"),
        ("SpeedX HTTP", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt", "http"),
    ]
    created = 0
    for name, url, protocol in defaults:
        exists = db.query(ProxySource).filter(ProxySource.url == url).first()
        if not exists:
            db.add(ProxySource(name=name, url=url, protocol=protocol))
            created += 1
    db.commit()
    return {"created": created}
