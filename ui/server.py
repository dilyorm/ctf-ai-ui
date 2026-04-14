"""FastAPI web UI for CTF Agent.

Provides:
  - Real-time dashboard via WebSocket
  - GitHub OAuth login
  - REST API for status, config, and operator messaging
  - Static files & Jinja2 templates

Start with:
    uv run ctf-ui
  or
    uv run uvicorn ui.server:app --host 0.0.0.0 --port 8080 --reload
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.sessions import SessionMiddleware

from ui.event_bus import get_bus
from ui.github_auth import (
    build_authorize_url,
    exchange_code_for_token,
    fetch_github_user,
    generate_state,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent

app = FastAPI(
    title="CTF Agent Dashboard",
    description="Real-time dashboard for the CTF multi-model solver swarm",
    version="1.0.0",
)

# Session middleware (cookie-based signed sessions)
SECRET_KEY = os.environ.get("UI_SECRET_KEY", secrets.token_hex(32))
app.add_middleware(
    SessionMiddleware, secret_key=SECRET_KEY, session_cookie="ctf_session", max_age=86400 * 7
)

# Static files + templates
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# GitHub OAuth config (from env)
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
UI_HOST = os.environ.get("UI_HOST", "0.0.0.0")
UI_PORT = int(os.environ.get("UI_PORT", "8080"))


def _callback_url(request: Request) -> str:
    """Build the GitHub OAuth callback URL from the current request."""
    return str(request.base_url).rstrip("/") + "/auth/github/callback"


def _get_user(request: Request) -> dict | None:
    """Return the current user dict from session, or None."""
    return request.session.get("user")


# ─────────────────────────────────────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = _get_user(request)
    bus = get_bus()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "github_login_enabled": bool(GITHUB_CLIENT_ID),
            "ctfd_status": bus.ctfd_status,
            "total_cost": bus.total_cost,
            "challenge_count": len(bus.challenges),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# GitHub OAuth
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/auth/github")
async def github_login(request: Request):
    """Redirect user to GitHub OAuth authorization page."""
    if not GITHUB_CLIENT_ID:
        return JSONResponse(
            {
                "error": "GitHub OAuth not configured. Set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET in .env"
            },
            status_code=503,
        )
    state = generate_state()
    request.session["oauth_state"] = state
    url = build_authorize_url(GITHUB_CLIENT_ID, _callback_url(request), state)
    return RedirectResponse(url)


@app.get("/auth/github/callback")
async def github_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle GitHub OAuth callback — exchange code for token and store user in session."""
    if error:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": f"GitHub OAuth error: {error}"},
            status_code=400,
        )

    saved_state = request.session.pop("oauth_state", None)
    if not saved_state or saved_state != state:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": "OAuth state mismatch — possible CSRF attack."},
            status_code=400,
        )

    token = await exchange_code_for_token(
        GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, code, _callback_url(request)
    )
    if not token:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": "Failed to exchange OAuth code for token."},
            status_code=400,
        )

    user = await fetch_github_user(token)
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": "Failed to fetch GitHub user profile."},
            status_code=400,
        )

    request.session["user"] = user
    logger.info("GitHub login: %s (%s)", user["login"], user["name"])
    return RedirectResponse("/")


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/auth/me")
async def auth_me(request: Request):
    user = _get_user(request)
    if not user:
        return JSONResponse({"authenticated": False})
    return JSONResponse(
        {"authenticated": True, "user": {k: v for k, v in user.items() if k != "access_token"}}
    )


# ─────────────────────────────────────────────────────────────────────────────
# REST API
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/status")
async def api_status():
    """Return full solver status snapshot."""
    bus = get_bus()
    return JSONResponse(
        {
            "challenges": bus.challenges,
            "cost": {
                "total_usd": bus.total_cost,
                "total_tokens": bus.total_tokens,
                "by_model": bus.cost_summary,
            },
            "ctfd": bus.ctfd_status,
        }
    )


@app.get("/api/challenges")
async def api_challenges():
    bus = get_bus()
    return JSONResponse({"challenges": list(bus.challenges.values())})


@app.get("/api/challenges/{name}/logs")
async def api_challenge_logs(name: str):
    bus = get_bus()
    logs = list(bus.logs.get(name, []))
    return JSONResponse({"challenge": name, "logs": logs})


@app.post("/api/message")
async def api_message(request: Request):
    """Send an operator message to the running coordinator."""
    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    # Forward to coordinator via the operator inbox if available
    from ui.coordinator_bridge import get_operator_inbox

    inbox = get_operator_inbox()
    if inbox:
        inbox.put_nowait(message)
        return JSONResponse({"ok": True, "queued": message[:200]})

    # Fallback: try the HTTP endpoint
    import urllib.request, json as _json

    port = int(os.environ.get("MSG_PORT", "9400"))
    body_bytes = _json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/msg",
        data=body_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return JSONResponse(_json.loads(resp.read()))
    except Exception:
        return JSONResponse({"error": "Coordinator not running or unreachable"}, status_code=503)


@app.post("/api/config")
async def api_config(request: Request):
    """Update runtime configuration (CTFd URL/token, API keys)."""
    body = await request.json()
    updated: list[str] = []

    env_map = {
        "ctfd_url": "CTFD_URL",
        "ctfd_token": "CTFD_TOKEN",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "gemini_api_key": "GEMINI_API_KEY",
    }
    for key, env_var in env_map.items():
        if key in body and body[key]:
            os.environ[env_var] = body[key]
            updated.append(key)

    return JSONResponse({"ok": True, "updated": updated})


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket
# ─────────────────────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Real-time event stream for the dashboard."""
    await ws.accept()
    bus = get_bus()
    queue = await bus.subscribe()
    try:
        while True:
            msg = await asyncio.wait_for(queue.get(), timeout=30.0)
            await ws.send_text(msg)
    except asyncio.TimeoutError, WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WebSocket closed: %s", e)
    finally:
        await bus.unsubscribe(queue)
        try:
            await ws.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Startup / shutdown
# ─────────────────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def on_startup():
    logger.info("CTF Agent UI starting at http://%s:%d", UI_HOST, UI_PORT)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def run():
    """Entry point for `ctf-ui` CLI command."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%X",
    )
    uvicorn.run(
        "ui.server:app",
        host=UI_HOST,
        port=UI_PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
