from __future__ import annotations

import asyncio

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings


class ResolverCapacity:
    """Per-instance semaphore consumed for the full ASGI response."""

    def __init__(self, maximum: int):
        self.maximum = max(1, maximum)
        self.active = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self.active >= self.maximum:
                return False
            self.active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self.active = max(0, self.active - 1)

    def snapshot(self) -> dict[str, int | bool]:
        return {"active": self.active, "maximum": self.maximum, "available": self.active < self.maximum}


class ResolverCapacityMiddleware:
    def __init__(self, app: ASGIApp, capacity: ResolverCapacity):
        self.app = app
        self.capacity = capacity

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in {"/", "/health", "/api/health", "/api/health/db"}:
            await self.app(scope, receive, send)
            return
        if not await self.capacity.try_acquire():
            response = JSONResponse(
                {"error": {"code": "CAPACITY_FULL", "message": "Resolver node is busy."}},
                status_code=503,
                headers={"Retry-After": "3", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            await self.capacity.release()


capacity = ResolverCapacity(get_settings().max_concurrent_requests)
