from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.api.media import router as media_router
from app.api.metadata import router as metadata_router
from app.api.playtest import router as playtest_router
from app.api.proxies import router as proxies_router
from app.api.stats import router as stats_router
from app.api.streams import router as streams_router
from app.api.tracks import router as tracks_router
from app.config import get_settings
from app.database import init_db
from app.security import (
    DASHBOARD_SESSION_COOKIE,
    create_dashboard_session,
    dashboard_password_configured,
    dashboard_password_matches,
    dashboard_session_is_valid,
    require_api_key,
    require_dashboard_session,
)
from app.services.capacity import ResolverCapacityMiddleware, capacity

settings = get_settings()
static_dir = Path(__file__).resolve().parent / "static"

init_db()

app = FastAPI(title=settings.name, version=settings.version)
app.add_middleware(ResolverCapacityMiddleware, capacity=capacity)

origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
private_router_options = {"dependencies": [Depends(require_api_key)]}
app.include_router(stats_router, **private_router_options)
app.include_router(proxies_router, **private_router_options)
app.include_router(streams_router, **private_router_options)
app.include_router(tracks_router, **private_router_options)
app.include_router(metadata_router, **private_router_options)
app.include_router(playtest_router, **private_router_options)
app.include_router(media_router, **private_router_options)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def root():
    return {
        "service": settings.name,
        "version": settings.version,
        "dashboard": "/dashboard",
    }


@app.get("/dashboard/login", response_class=HTMLResponse)
def dashboard_login_page(request: Request):
    if dashboard_session_is_valid(request.cookies.get(DASHBOARD_SESSION_COOKIE)):
        return RedirectResponse("/dashboard", status_code=303)
    if not dashboard_password_configured():
        return HTMLResponse(
            "<h1>Dashboard authentication is not configured.</h1><p>Set PRODUCERSCENTER_BACKEND_DASHBOARD_PASSWORD in Render and redeploy this service.</p>",
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    invalid_password = request.query_params.get("error") == "invalid"
    error_message = '<p class="error">Incorrect password. Try again.</p>' if invalid_password else ""
    return HTMLResponse(
        dashboard_login_html(error_message),
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )


@app.post("/dashboard/login")
def dashboard_login(request: Request, password: str = Form(default="")):
    if not dashboard_password_configured():
        raise HTTPException(status_code=503, detail="Dashboard authentication is not configured.")
    if not dashboard_password_matches(password):
        return RedirectResponse("/dashboard/login?error=invalid", status_code=303)
    response = RedirectResponse("/dashboard", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        key=DASHBOARD_SESSION_COOKIE,
        value=create_dashboard_session(),
        max_age=max(300, settings.dashboard_session_ttl_seconds),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return response


@app.post("/dashboard/logout")
def dashboard_logout():
    response = RedirectResponse("/dashboard/login", status_code=303)
    response.delete_cookie(DASHBOARD_SESSION_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/dashboard", dependencies=[Depends(require_dashboard_session)])
def dashboard():
    return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


def dashboard_login_html(error_message: str) -> str:
    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\" /><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>Backend dashboard login</title><link rel=\"stylesheet\" href=\"/static/style.css\" /></head>
<body class=\"login-page\"><main class=\"login-card\"><p class=\"eyebrow\">ProducersCenter</p><h1>Backend dashboard</h1>
<p>Enter the dashboard password. This is separate from the resolver node key.</p>{error_message}
<form method=\"post\" action=\"/dashboard/login\"><label for=\"password\">Dashboard password</label><input id=\"password\" name=\"password\" type=\"password\" autocomplete=\"current-password\" required autofocus /><button type=\"submit\">Sign in</button></form>
</main></body></html>"""
