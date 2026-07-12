from __future__ import annotations

import asyncio

from app.config import get_settings

settings = get_settings()

proxy_check_semaphore = asyncio.Semaphore(settings.proxy_check_concurrency)
stream_resolve_semaphore = asyncio.Semaphore(settings.stream_resolve_concurrency)

