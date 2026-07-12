from __future__ import annotations

import asyncio
import logging
import ssl
import aiohttp
import certifi
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Proxy, ProxySource
from app.schemas import ProxyCheckRequest, ProxyImportRequest, ProxySourceCreate, ProxyUrlImportRequest
from app.services.proxy_checker import check_proxy, check_proxy_fast
from app.services.limits import proxy_check_semaphore
from app.services.proxy_store import apply_check_result, best_proxies, upsert_proxy
from app.services.proxy_utils import normalize_proxy

router = APIRouter()
logger = logging.getLogger("producerscenter.backend.proxies")


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
        "download_ms": row.download_ms,
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
    q: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(Proxy)
    if status:
        query = query.filter(Proxy.status == status)
    if verified is not None:
        query = query.filter(Proxy.is_verified == verified)
    if q:
        query = query.filter(Proxy.proxy_url.contains(q))
    total = query.count()
    rows = query.order_by(Proxy.score.desc(), Proxy.latency_ms.asc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "page": int(offset / limit) + 1,
        "pages": max(1, (total + limit - 1) // limit),
        "proxies": [serialize_proxy(row) for row in rows],
    }


@router.get("/api/proxies/top")
def top_proxies(limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)):
    return {"proxies": [serialize_proxy(row) for row in best_proxies(db, limit)]}


@router.post("/api/proxies/import")
async def import_proxies(payload: ProxyImportRequest, db: Session = Depends(get_db)):
    return await import_proxy_values(
        db,
        payload.proxies,
        source=payload.source,
        protocol=payload.protocol,
        check_before_add=payload.check_before_add,
        check_mode=payload.check_mode,
        check_limit=payload.check_limit,
    )


@router.post("/api/proxies/import-url")
async def import_proxies_from_url(payload: ProxyUrlImportRequest, db: Session = Depends(get_db)):
    proxies = await fetch_proxy_text_url(payload.url)
    return await import_proxy_values(
        db,
        proxies,
        source=payload.source or payload.url,
        protocol=payload.protocol,
        check_before_add=payload.check_before_add,
        check_mode=payload.check_mode,
        check_limit=payload.check_limit,
    )


async def import_proxy_values(
    db: Session,
    proxies: list[str],
    source: str,
    protocol: str,
    check_before_add: bool,
    check_mode: str,
    check_limit: int,
):
    created = 0
    updated = 0
    duplicates = 0
    checked = 0
    alive = 0
    dead = 0
    skipped_unchecked = 0
    errors: list[str] = []
    seen: set[str] = set()
    candidates: list[str] = []
    for item in proxies:
        try:
            normalized = normalize_proxy(item, protocol)
        except Exception as error:
            errors.append(f"{item}: {error}")
            continue
        if normalized in seen:
            duplicates += 1
            continue
        seen.add(normalized)
        candidates.append(normalized)

    logger.info(
        "proxy import parsed loaded=%s unique=%s duplicates=%s check_before_add=%s mode=%s limit=%s",
        len(proxies),
        len(candidates),
        duplicates,
        check_before_add,
        check_mode,
        check_limit,
    )

    if check_before_add:
        to_check = candidates[:check_limit]
        skipped_unchecked = max(0, len(candidates) - len(to_check))
        checked = len(to_check)

        async def check_candidate(normalized: str) -> tuple[str, dict | None, str | None]:
            try:
                async with proxy_check_semaphore:
                    result = await check_proxy_fast(normalized) if check_mode == "fast" else await check_proxy(normalized)
                return normalized, result, None
            except Exception as error:
                return normalized, None, str(error)

        logger.info("proxy import check start count=%s concurrency=semaphore", len(to_check))
        checked_results = await asyncio.gather(*(check_candidate(candidate) for candidate in to_check))
        logger.info("proxy import check complete count=%s", len(checked_results))

        for normalized, result, error in checked_results:
            if error:
                dead += 1
                errors.append(f"{normalized}: {error}")
                continue
            if not result or result["status"] != "verified":
                dead += 1
                if result and result.get("error"):
                    errors.append(f"{normalized}: {result.get('error')}")
                continue
            try:
                row, was_created = upsert_proxy(db, normalized, source, "auto")
                apply_check_result(db, row, result)
                alive += 1
                if was_created:
                    created += 1
                else:
                    updated += 1
            except Exception as error:
                errors.append(f"{normalized}: {error}")
    else:
        for normalized in candidates:
            try:
                _, was_created = upsert_proxy(db, normalized, source, "auto")
            except Exception as error:
                errors.append(f"{normalized}: {error}")
                continue
            if was_created:
                created += 1
            else:
                updated += 1

    logger.info(
        "proxy import stored created=%s updated=%s alive=%s dead=%s skipped=%s errors=%s",
        created,
        updated,
        alive,
        dead,
        skipped_unchecked,
        len(errors),
    )

    return {
        "loaded": len(proxies),
        "unique": len(candidates),
        "duplicates": duplicates,
        "created": created,
        "updated": updated,
        "checked": checked,
        "alive": alive,
        "dead": dead,
        "skipped_unchecked": skipped_unchecked,
        "errors": errors[:20],
    }


async def fetch_proxy_text_url(url: str) -> list[str]:
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, timeout=30) as response:
                if response.status >= 400:
                    raise HTTPException(status_code=502, detail=f"Proxy source returned HTTP {response.status}")
                text = await response.text()
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Could not fetch proxy source URL: {error}") from error
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


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
        async with proxy_check_semaphore:
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


@router.delete("/api/proxies")
def clear_proxies(db: Session = Depends(get_db)):
    deleted = db.query(Proxy).delete()
    db.commit()
    return {
        "deleted": deleted,
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
async def fetch_sources(
    check_before_add: bool = True,
    check_limit_per_source: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    sources = db.query(ProxySource).filter(ProxySource.enabled == True).all()  # noqa: E712
    total = {
        "sources": len(sources),
        "loaded": 0,
        "unique": 0,
        "duplicates": 0,
        "created": 0,
        "updated": 0,
        "checked": 0,
        "alive": 0,
        "dead": 0,
        "skipped_unchecked": 0,
    }
    errors: list[str] = []
    for source in sources:
        try:
            proxies = await fetch_proxy_text_url(source.url)
            result = await import_proxy_values(
                db,
                proxies,
                source=source.name,
                protocol=source.protocol,
                check_before_add=check_before_add,
                check_mode="fast",
                check_limit=check_limit_per_source,
            )
            for key in total:
                if key != "sources":
                    total[key] += int(result.get(key, 0))
            errors.extend(result.get("errors", []))
        except Exception as error:
            errors.append(f"{source.name}: {error}")
    return {**total, "errors": errors[:20]}


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


@router.get("/api/client-proxies")
def client_proxy_list(
    format: str = Query("json", pattern="^(json|txt)$"),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    rows = best_proxies(db, limit)
    if format == "txt":
        return PlainTextResponse("\n".join(row.proxy_url for row in rows) + ("\n" if rows else ""))
    return {
        "count": len(rows),
        "proxies": [
            {
                "proxy": row.proxy_url,
                "protocol": row.protocol,
                "score": row.score,
                "latency_ms": row.latency_ms,
                "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
            }
            for row in rows
        ],
    }
