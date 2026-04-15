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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.sessions import SessionMiddleware

from backend.auth import hash_password, verify_password
from backend.db import get_db
from backend.crypto import open_opt, seal_opt
from backend.db_models import User, UserSettings
from backend.models import DEFAULT_MODELS
from backend.run_manager import get_run_manager
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ui.event_bus import get_bus
from ui.github_auth import (
    build_authorize_url,
    exchange_code_for_token,
    fetch_github_user,
    generate_state,
)

logger = logging.getLogger(__name__)


def _truthy_env(name: str, default: str = "") -> bool:
    v = os.environ.get(name, default).strip().lower()
    return v not in ("", "0", "false", "no", "off")


def _write_env_file(path: Path, updates: dict[str, str]) -> None:
    """Persist config updates to a simple KEY=VALUE env file.

    We keep this intentionally minimal (no dotenv parsing) so we don't rewrite
    unknown keys. We update existing keys in-place and append new ones.
    """

    def _escape(v: str) -> str:
        # Use bash $'..' quoting so `source` preserves newlines via \n escapes.
        s = v.replace("\\", "\\\\").replace("'", "\\'").replace("\r", "\\r").replace("\n", "\\n")
        return "$'" + s + "'"

    existing: list[str] = []
    if path.exists():
        existing = path.read_text(encoding="utf-8").splitlines(True)

    wanted = dict(updates)
    out: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in wanted:
            out.append(f"{key}={_escape(wanted.pop(key))}\n")
        else:
            out.append(line)

    if wanted:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"\n# Updated by UI at {datetime.now(timezone.utc).isoformat()}\n")
        for k in sorted(wanted.keys()):
            out.append(f"{k}={_escape(wanted[k])}\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(out), encoding="utf-8")
    tmp.replace(path)


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
# For production, set UI_SECRET_KEY to a stable value (so sessions survive restarts).
SECRET_KEY = os.environ.get("UI_SECRET_KEY") or secrets.token_hex(32)
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


async def _require_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    sess = _get_user(request)
    if not sess or not sess.get("user_id"):
        # FastAPI will treat this as a 500; convert to 401 here.
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="unauthorized")
    user_id = int(sess["user_id"])
    user = await db.get(User, user_id)
    if not user or not user.is_active:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="unauthorized")
    return user


async def _require_db_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """Require a real DB-backed user (email/password), not GitHub-only session."""
    user = await _require_user(request, db)
    # GitHub session dicts don't have email/password user_id from our DB.
    # If a session was created via GitHub OAuth it won't map to a DB user.
    # (We can implement linking later.)
    return user


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


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _get_user(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="login.html", context={"error": ""})


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if _get_user(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="register.html", context={"error": ""})


@app.post("/register")
async def register_post(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    pw = (form.get("password") or "").strip()
    if not email or not pw or len(pw) < 8:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Invalid email or password (min 8 chars)."},
            status_code=400,
        )

    exists = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if exists:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Email already registered."},
            status_code=400,
        )

    user = User(email=email, password_hash=hash_password(pw))
    db.add(user)
    await db.commit()
    await db.refresh(user)

    request.session["user"] = {"user_id": user.id, "email": user.email}
    return RedirectResponse("/", status_code=303)


@app.post("/login")
async def login_post(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    pw = (form.get("password") or "").strip()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not user or not user.is_active or not verify_password(pw, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid credentials."},
            status_code=401,
        )
    request.session["user"] = {"user_id": user.id, "email": user.email}
    return RedirectResponse("/", status_code=303)


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


@app.get("/api/config")
async def api_get_config(
    user: User = Depends(_require_db_user), db: AsyncSession = Depends(get_db)
):
    st = await db.get(UserSettings, user.id)
    if not st:
        return JSONResponse({"ok": True, "config": {}})
    return JSONResponse(
        {
            "ok": True,
            "config": {
                "ctfd_url": st.ctfd_url,
                # never return secrets
                "claude_cli_path": st.claude_cli_path,
                "claude_config_dir": st.claude_config_dir,
                "exclude_challenges": st.exclude_challenges,
                "exclude_challenge_regex": st.exclude_challenge_regex,
            },
        }
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
async def api_config(
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Update per-user configuration (persisted in Postgres, secrets encrypted).

    For backwards compatibility, also applies values to process env (affects any running run).
    """
    body = await request.json()

    st = await db.get(UserSettings, user.id)
    if not st:
        st = UserSettings(user_id=user.id)
        db.add(st)

    try:
        # Non-secret
        if "ctfd_url" in body:
            st.ctfd_url = (body.get("ctfd_url") or "").strip()
            if st.ctfd_url:
                os.environ["CTFD_URL"] = st.ctfd_url
        if "claude_cli_path" in body:
            st.claude_cli_path = (body.get("claude_cli_path") or "").strip()
            if st.claude_cli_path:
                os.environ["CLAUDE_CLI_PATH"] = st.claude_cli_path
        if "claude_config_dir" in body:
            st.claude_config_dir = (body.get("claude_config_dir") or "").strip()
            if st.claude_config_dir:
                os.environ["CLAUDE_CONFIG_DIR"] = st.claude_config_dir
        if "exclude_challenges" in body:
            st.exclude_challenges = body.get("exclude_challenges") or ""
            if st.exclude_challenges:
                os.environ["EXCLUDE_CHALLENGES"] = st.exclude_challenges
        if "exclude_challenge_regex" in body:
            st.exclude_challenge_regex = (body.get("exclude_challenge_regex") or "").strip()
            if st.exclude_challenge_regex:
                os.environ["EXCLUDE_CHALLENGE_REGEX"] = st.exclude_challenge_regex

        # Secrets
        if "ctfd_token" in body:
            raw = (body.get("ctfd_token") or "").strip()
            st.ctfd_token_enc = seal_opt(raw)
            if raw:
                os.environ["CTFD_TOKEN"] = raw
        if "anthropic_api_key" in body:
            raw = (body.get("anthropic_api_key") or "").strip()
            st.anthropic_api_key_enc = seal_opt(raw)
            if raw:
                os.environ["ANTHROPIC_API_KEY"] = raw
        if "openai_api_key" in body:
            raw = (body.get("openai_api_key") or "").strip()
            st.openai_api_key_enc = seal_opt(raw)
            if raw:
                os.environ["OPENAI_API_KEY"] = raw
        if "gemini_api_key" in body:
            raw = (body.get("gemini_api_key") or "").strip()
            st.gemini_api_key_enc = seal_opt(raw)
            if raw:
                os.environ["GEMINI_API_KEY"] = raw
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "hint": "Set APP_SECRET_KEY."},
            status_code=500,
        )

    st.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return JSONResponse({"ok": True})


@app.get("/api/run/status")
async def api_run_status(user: User = Depends(_require_db_user)):
    mgr = get_run_manager()
    st = mgr.status()
    # Only show owner id to authenticated users; caller is authenticated here.
    return JSONResponse({"ok": True, "status": st})


@app.post("/api/run/start")
async def api_run_start(
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    st = await db.get(UserSettings, user.id)
    if not st:
        return JSONResponse({"ok": False, "error": "configure CTFd first"}, status_code=400)

    try:
        # Build Settings for this run.
        settings = Settings()
        if st.ctfd_url:
            settings.ctfd_url = st.ctfd_url
        token = open_opt(st.ctfd_token_enc)
        if token:
            settings.ctfd_token = token
        settings.anthropic_api_key = open_opt(st.anthropic_api_key_enc)
        settings.openai_api_key = open_opt(st.openai_api_key_enc)
        settings.gemini_api_key = open_opt(st.gemini_api_key_enc)
        settings.claude_cli_path = st.claude_cli_path
        settings.claude_config_dir = st.claude_config_dir
        settings.max_concurrent_challenges = int(body.get("max_concurrent_challenges") or 10)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "hint": "Set APP_SECRET_KEY."},
            status_code=500,
        )

    coordinator_backend = (body.get("coordinator") or "claude").strip()
    coordinator_model = (body.get("coordinator_model") or None) or None
    no_submit = bool(body.get("no_submit") or False)

    # Model selection: if absent, default lineup.
    model_specs = body.get("model_specs")
    if not isinstance(model_specs, list) or not all(isinstance(x, str) for x in model_specs):
        model_specs = list(DEFAULT_MODELS)

    # Exclusions from user settings.
    exclude_list: list[str] = []
    if st.exclude_challenges.strip():
        parts: list[str] = []
        for line in st.exclude_challenges.splitlines():
            parts.extend(line.split(","))
        exclude_list = [p.strip() for p in parts if p.strip()]
    exclude_rx = st.exclude_challenge_regex.strip() or None

    resp = await get_run_manager().start(
        user_id=user.id,
        settings=settings,
        model_specs=list(model_specs),
        challenges_dir=str(body.get("challenges_dir") or "challenges"),
        exclude_challenges=exclude_list,
        exclude_challenge_regex=exclude_rx,
        no_submit=no_submit,
        coordinator_backend=coordinator_backend,
        coordinator_model=coordinator_model,
        msg_port=int(body.get("msg_port") or 0),
    )
    return JSONResponse(resp, status_code=200 if resp.get("ok") else 409)


@app.post("/api/run/stop")
async def api_run_stop(
    request: Request,
    user: User = Depends(_require_db_user),
):
    body = await request.json()
    force = bool(body.get("force") or False)
    resp = await get_run_manager().stop(user_id=user.id, force=force)
    return JSONResponse(resp, status_code=200 if resp.get("ok") else 403)


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
    except asyncio.TimeoutError:
        pass
    except WebSocketDisconnect:
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
