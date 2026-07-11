from __future__ import annotations

from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from app.models import Proxy
from app.services.proxy_utils import normalize_proxy, proxy_host_port


def upsert_proxy(db: Session, raw_proxy: str, source: str = "manual", protocol: str = "auto") -> tuple[Proxy, bool]:
    proxy_url = normalize_proxy(raw_proxy, protocol)
    row = db.query(Proxy).filter(Proxy.proxy_url == proxy_url).first()
    created = False
    if row is None:
        host, port = proxy_host_port(proxy_url)
        row = Proxy(
            proxy_url=proxy_url,
            protocol=proxy_url.split("://", 1)[0],
            host=host,
            port=port,
            source=source,
        )
        db.add(row)
        created = True
    else:
        row.source = source or row.source
        row.is_active = True
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row, created


def apply_check_result(db: Session, proxy: Proxy, result: dict) -> Proxy:
    now = datetime.utcnow()
    status = result["status"]
    proxy.status = status
    proxy.latency_ms = result.get("latency_ms") or proxy.latency_ms
    proxy.last_error = (result.get("error") or "")[:4000]
    proxy.last_checked_at = now
    proxy.updated_at = now

    if status == "verified":
        proxy.is_verified = True
        proxy.is_active = True
        proxy.success_count += 1
        proxy.youtube_success += 1
        proxy.fail_count = 0
        proxy.last_success_at = now
        latency_penalty = max(proxy.latency_ms, 1)
        proxy.score = min(1000, 500 + proxy.youtube_success * 25 + int(100000 / latency_penalty))
        proxy.cooldown_until = None
    elif status in {"youtube_blocked", "captcha"}:
        proxy.is_verified = False
        proxy.is_active = True
        proxy.youtube_fail += 1
        proxy.bot_block_count += 1
        proxy.fail_count += 1
        proxy.score = max(0, proxy.score - 100)
        proxy.cooldown_until = now + timedelta(hours=2)
    elif status == "timeout":
        proxy.is_verified = False
        proxy.fail_count += 1
        proxy.timeout_count += 1
        proxy.score = max(0, proxy.score - 50)
        proxy.cooldown_until = now + timedelta(minutes=30)
    else:
        proxy.is_verified = False
        proxy.fail_count += 1
        proxy.score = max(0, proxy.score - 75)
        if proxy.fail_count >= 5:
            proxy.is_active = False

    db.commit()
    db.refresh(proxy)
    return proxy


def best_proxies(db: Session, limit: int = 20) -> list[Proxy]:
    now = datetime.utcnow()
    return (
        db.query(Proxy)
        .filter(Proxy.is_active == True)  # noqa: E712
        .filter(Proxy.is_verified == True)  # noqa: E712
        .filter((Proxy.cooldown_until == None) | (Proxy.cooldown_until < now))  # noqa: E711
        .order_by(Proxy.score.desc(), Proxy.latency_ms.asc())
        .limit(limit)
        .all()
    )

