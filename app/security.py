"""Authentication helpers for the private resolver service."""

from hmac import compare_digest

from fastapi import Header, HTTPException, status

from app.config import get_settings


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    """Require the shared gateway-to-resolver bearer token.

    Health remains unauthenticated so the gateway can monitor a node. All
    resolver, proxy-management, catalogue and dashboard routes use this
    dependency. An absent key deliberately makes private routes unavailable
    rather than accidentally public.
    """

    expected = get_settings().api_key.strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Resolver authentication is not configured.",
        )

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or not compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Resolver authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
