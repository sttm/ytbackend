"""Authentication helpers for the private resolver service."""

from hashlib import sha256
from hmac import compare_digest, new
from time import time

from fastapi import Cookie, Header, HTTPException, status

from app.config import get_settings


DASHBOARD_SESSION_COOKIE = "pc_backend_dashboard"


def require_api_key(
    authorization: str | None = Header(default=None),
    dashboard_session: str | None = Cookie(default=None, alias=DASHBOARD_SESSION_COOKIE),
) -> None:
    """Require the shared gateway-to-resolver bearer token.

    Health remains unauthenticated so the gateway can monitor a node. All
    resolver, proxy-management, catalogue and dashboard routes use this
    dependency. An absent key deliberately makes private routes unavailable
    rather than accidentally public.
    """

    if dashboard_session_is_valid(dashboard_session):
        return

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


def require_dashboard_session(
    dashboard_session: str | None = Cookie(default=None, alias=DASHBOARD_SESSION_COOKIE),
) -> None:
    """Require the browser-only dashboard session, never the node API key."""

    if not dashboard_password_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard authentication is not configured.",
        )
    if not dashboard_session_is_valid(dashboard_session):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Dashboard login is required.",
        )


def dashboard_password_configured() -> bool:
    return bool(get_settings().dashboard_password.strip())


def dashboard_password_matches(candidate: str) -> bool:
    expected = get_settings().dashboard_password.strip()
    return bool(expected) and compare_digest(candidate, expected)


def create_dashboard_session() -> str:
    expires_at = int(time()) + max(300, get_settings().dashboard_session_ttl_seconds)
    payload = f"dashboard:{expires_at}"
    return f"{expires_at}.{_dashboard_signature(payload)}"


def dashboard_session_is_valid(value: str | None) -> bool:
    if not dashboard_password_configured() or not value:
        return False
    raw_expiry, separator, supplied_signature = value.partition(".")
    if not separator or not raw_expiry.isdecimal() or not supplied_signature:
        return False
    expires_at = int(raw_expiry)
    if expires_at < int(time()):
        return False
    return compare_digest(supplied_signature, _dashboard_signature(f"dashboard:{expires_at}"))


def _dashboard_signature(payload: str) -> str:
    # A password-derived HMAC makes the cookie tamper-proof without storing a
    # session server-side. Changing the dashboard password invalidates all
    # existing dashboard sessions.
    key = sha256(get_settings().dashboard_password.strip().encode("utf-8")).digest()
    return new(key, payload.encode("utf-8"), sha256).hexdigest()
