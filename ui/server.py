"""FastAPI web UI for CTF Agent.

Provides:
  - Auth: email/password + GitHub OAuth
  - Dashboard: real-time challenge viewer via WebSocket
  - Settings: API keys, model preferences, exclusions
  - CTF management: create/list/delete CTF instances
  - Run controls: start/stop run, per-challenge stop/priority
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from backend.auth import hash_password, verify_password
from backend.config import Settings
from backend.crypto import open_opt, seal_opt
from backend.db import get_db
from backend.db_models import CTF as CTFModel
from backend.db_models import User, UserModelPref, UserSettings
from backend.models import ALL_MODELS, DEFAULT_MODELS
from backend.run_manager import get_run_manager
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
    version="2.0.0",
)

SECRET_KEY = os.environ.get("UI_SECRET_KEY") or secrets.token_hex(32)
app.add_middleware(
    SessionMiddleware, secret_key=SECRET_KEY, session_cookie="ctf_session", max_age=86400 * 7
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
UI_HOST = os.environ.get("UI_HOST", "0.0.0.0")
UI_PORT = int(os.environ.get("UI_PORT", "8080"))


def _callback_url(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/auth/github/callback"


def _get_user(request: Request) -> dict | None:
    return request.session.get("user")


async def _require_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    sess = _get_user(request)
    if not sess or not sess.get("user_id"):
        raise HTTPException(status_code=401, detail="unauthorized")
    user_id = int(sess["user_id"])
    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


async def _require_db_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    return await _require_user(request, db)


def _require_user_redirect(request: Request):
    """For page routes — return session user dict or redirect to /login."""
    sess = _get_user(request)
    if not sess or not sess.get("user_id"):
        return None  # caller should redirect
    return sess


# ─────────────────────────────────────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login")
    bus = get_bus()
    # Load user's CTFs for the CTF selector
    ctfs: list[dict] = []
    if user.get("user_id"):
        rows = (
            await db.execute(
                select(CTFModel).where(CTFModel.user_id == int(user["user_id"])).order_by(CTFModel.id.desc())
            )
        ).scalars().all()
        ctfs = [{"id": c.id, "name": c.name, "ctfd_url": c.ctfd_url} for c in rows]

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "github_login_enabled": bool(GITHUB_CLIENT_ID),
            "ctfd_status": bus.ctfd_status,
            "total_cost": bus.total_cost,
            "challenge_count": len(bus.challenges),
            "ctfs": ctfs,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login")
    user_id = int(user["user_id"])

    # Load current settings
    st = await db.get(UserSettings, user_id)
    cfg = {}
    if st:
        cfg = {
            "ctfd_url": st.ctfd_url or "",
            "claude_cli_path": st.claude_cli_path or "",
            "claude_config_dir": st.claude_config_dir or "",
            "exclude_challenges": st.exclude_challenges or "",
            "exclude_challenge_regex": st.exclude_challenge_regex or "",
            "has_anthropic": bool(st.anthropic_api_key_enc),
            "has_openai": bool(st.openai_api_key_enc),
            "has_gemini": bool(st.gemini_api_key_enc),
        }

    # Load model prefs
    prefs_rows = (
        await db.execute(select(UserModelPref).where(UserModelPref.user_id == user_id))
    ).scalars().all()
    enabled_specs = {p.model_spec for p in prefs_rows if p.enabled}
    # If no prefs set, default models are enabled
    if not prefs_rows:
        enabled_specs = set(DEFAULT_MODELS)

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "user": user,
            "cfg": cfg,
            "all_models": ALL_MODELS,
            "enabled_specs": enabled_specs,
            "saved": request.query_params.get("saved") == "1",
        },
    )


@app.get("/ctfs", response_class=HTMLResponse)
async def ctfs_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login")
    user_id = int(user["user_id"])

    rows = (
        await db.execute(
            select(CTFModel).where(CTFModel.user_id == user_id).order_by(CTFModel.id.desc())
        )
    ).scalars().all()
    ctfs = [{"id": c.id, "name": c.name, "ctfd_url": c.ctfd_url, "created_at": c.created_at.strftime("%Y-%m-%d")} for c in rows]

    return templates.TemplateResponse(
        request=request,
        name="ctfs.html",
        context={"user": user, "ctfs": ctfs},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _get_user(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "", "github_login_enabled": bool(GITHUB_CLIENT_ID)},
    )


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if _get_user(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"error": "", "github_login_enabled": bool(GITHUB_CLIENT_ID)},
    )


@app.post("/register")
async def register_post(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    pw = (form.get("password") or "").strip()
    if not email or not pw or len(pw) < 8:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Invalid email or password (min 8 chars).", "github_login_enabled": bool(GITHUB_CLIENT_ID)},
            status_code=400,
        )

    exists = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if exists:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Email already registered.", "github_login_enabled": bool(GITHUB_CLIENT_ID)},
            status_code=400,
        )

    user = User(email=email, password_hash=hash_password(pw))
    db.add(user)
    await db.commit()
    await db.refresh(user)

    request.session["user"] = {"user_id": user.id, "email": user.email}
    # New users go to settings to configure their keys
    return RedirectResponse("/settings", status_code=303)


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
            context={"error": "Invalid credentials.", "github_login_enabled": bool(GITHUB_CLIENT_ID)},
            status_code=401,
        )
    request.session["user"] = {"user_id": user.id, "email": user.email}
    return RedirectResponse("/", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# GitHub OAuth
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/auth/github")
async def github_login(request: Request):
    if not GITHUB_CLIENT_ID:
        return JSONResponse({"error": "GitHub OAuth not configured."}, status_code=503)
    state = generate_state()
    request.session["oauth_state"] = state
    url = build_authorize_url(GITHUB_CLIENT_ID, _callback_url(request), state)
    return RedirectResponse(url)


@app.get("/auth/github/callback")
async def github_callback(request: Request, code: str = "", state: str = "", error: str = ""):
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
            context={"message": "OAuth state mismatch."},
            status_code=400,
        )

    token = await exchange_code_for_token(GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, code, _callback_url(request))
    if not token:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": "Failed to exchange OAuth code for token."},
            status_code=400,
        )

    gh_user = await fetch_github_user(token)
    if not gh_user:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": "Failed to fetch GitHub user profile."},
            status_code=400,
        )

    # Create or find DB user for GitHub login
    from backend.db import SessionLocal
    async with SessionLocal() as db:
        gh_email = gh_user.get("email") or f"github_{gh_user.get('id')}@github.local"
        db_user = (await db.execute(select(User).where(User.email == gh_email))).scalar_one_or_none()
        if not db_user:
            db_user = User(email=gh_email, password_hash="")
            db.add(db_user)
            await db.commit()
            await db.refresh(db_user)
        request.session["user"] = {
            "user_id": db_user.id,
            "email": db_user.email,
            "login": gh_user.get("login"),
            "name": gh_user.get("name"),
            "avatar_url": gh_user.get("avatar_url"),
        }

    logger.info("GitHub login: %s", gh_user.get("login"))
    return RedirectResponse("/")


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/auth/me")
async def auth_me(request: Request):
    user = _get_user(request)
    if not user:
        return JSONResponse({"authenticated": False})
    return JSONResponse({"authenticated": True, "user": {k: v for k, v in user.items() if k != "access_token"}})


# ─────────────────────────────────────────────────────────────────────────────
# Settings API (replaces /api/config)
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/config")
async def api_get_config(user: User = Depends(_require_db_user), db: AsyncSession = Depends(get_db)):
    st = await db.get(UserSettings, user.id)
    if not st:
        return JSONResponse({"ok": True, "config": {}})
    return JSONResponse({
        "ok": True,
        "config": {
            "ctfd_url": st.ctfd_url,
            "claude_cli_path": st.claude_cli_path,
            "claude_config_dir": st.claude_config_dir,
            "exclude_challenges": st.exclude_challenges,
            "exclude_challenge_regex": st.exclude_challenge_regex,
            "has_anthropic_key": bool(st.anthropic_api_key_enc),
            "has_openai_key": bool(st.openai_api_key_enc),
            "has_gemini_key": bool(st.gemini_api_key_enc),
        },
    })


@app.post("/api/config")
async def api_config(
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Update per-user configuration."""
    body = await request.json()
    st = await db.get(UserSettings, user.id)
    if not st:
        st = UserSettings(user_id=user.id)
        db.add(st)

    try:
        if "ctfd_url" in body:
            st.ctfd_url = (body["ctfd_url"] or "").strip()
        if "claude_cli_path" in body:
            st.claude_cli_path = (body["claude_cli_path"] or "").strip()
        if "claude_config_dir" in body:
            st.claude_config_dir = (body["claude_config_dir"] or "").strip()
        if "exclude_challenges" in body:
            st.exclude_challenges = body["exclude_challenges"] or ""
        if "exclude_challenge_regex" in body:
            st.exclude_challenge_regex = (body["exclude_challenge_regex"] or "").strip()
        if "ctfd_token" in body:
            raw = (body["ctfd_token"] or "").strip()
            st.ctfd_token_enc = seal_opt(raw)
        if "anthropic_api_key" in body:
            raw = (body["anthropic_api_key"] or "").strip()
            st.anthropic_api_key_enc = seal_opt(raw)
            if raw:
                os.environ["ANTHROPIC_API_KEY"] = raw
        if "openai_api_key" in body:
            raw = (body["openai_api_key"] or "").strip()
            st.openai_api_key_enc = seal_opt(raw)
            if raw:
                os.environ["OPENAI_API_KEY"] = raw
        if "gemini_api_key" in body:
            raw = (body["gemini_api_key"] or "").strip()
            st.gemini_api_key_enc = seal_opt(raw)
            if raw:
                os.environ["GEMINI_API_KEY"] = raw
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "hint": "Set APP_SECRET_KEY."}, status_code=500)

    st.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# CTF Management API
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/ctfs")
async def api_list_ctfs(user: User = Depends(_require_db_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(CTFModel).where(CTFModel.user_id == user.id).order_by(CTFModel.id.desc())
        )
    ).scalars().all()
    return JSONResponse({
        "ok": True,
        "ctfs": [
            {
                "id": c.id,
                "name": c.name,
                "ctfd_url": c.ctfd_url,
                "created_at": c.created_at.isoformat(),
            }
            for c in rows
        ],
    })


@app.post("/api/ctfs")
async def api_create_ctf(
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    name = (body.get("name") or "").strip()
    ctfd_url = (body.get("ctfd_url") or "").strip()
    ctfd_token = (body.get("ctfd_token") or "").strip()

    if not name or not ctfd_url:
        return JSONResponse({"ok": False, "error": "name and ctfd_url are required"}, status_code=400)

    # Check for duplicate name
    existing = (
        await db.execute(
            select(CTFModel).where(CTFModel.user_id == user.id, CTFModel.name == name)
        )
    ).scalar_one_or_none()
    if existing:
        return JSONResponse({"ok": False, "error": "CTF with this name already exists"}, status_code=409)

    try:
        token_enc = seal_opt(ctfd_token)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "hint": "Set APP_SECRET_KEY."}, status_code=500)

    ctf = CTFModel(user_id=user.id, name=name, ctfd_url=ctfd_url, ctfd_token_enc=token_enc)
    db.add(ctf)
    await db.commit()
    await db.refresh(ctf)

    return JSONResponse({
        "ok": True,
        "ctf": {"id": ctf.id, "name": ctf.name, "ctfd_url": ctf.ctfd_url},
    }, status_code=201)


@app.delete("/api/ctfs/{ctf_id}")
async def api_delete_ctf(
    ctf_id: int,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    ctf = await db.get(CTFModel, ctf_id)
    if not ctf or ctf.user_id != user.id:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    await db.delete(ctf)
    await db.commit()
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# Model Preferences API
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/models/available")
async def api_available_models():
    return JSONResponse({"ok": True, "models": ALL_MODELS})


@app.get("/api/models")
async def api_get_models(user: User = Depends(_require_db_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(select(UserModelPref).where(UserModelPref.user_id == user.id))
    ).scalars().all()
    if not rows:
        return JSONResponse({"ok": True, "enabled": list(DEFAULT_MODELS), "default": True})
    enabled = [r.model_spec for r in rows if r.enabled]
    return JSONResponse({"ok": True, "enabled": enabled})


@app.post("/api/models")
async def api_set_models(
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    enabled_specs: list[str] = body.get("enabled", [])
    if not isinstance(enabled_specs, list):
        return JSONResponse({"ok": False, "error": "enabled must be a list"}, status_code=400)

    # Delete existing prefs and insert new ones
    existing = (
        await db.execute(select(UserModelPref).where(UserModelPref.user_id == user.id))
    ).scalars().all()
    for row in existing:
        await db.delete(row)

    all_specs = {m["spec"] for m in ALL_MODELS}
    for spec in all_specs:
        pref = UserModelPref(user_id=user.id, model_spec=spec, enabled=(spec in enabled_specs))
        db.add(pref)

    await db.commit()
    return JSONResponse({"ok": True, "enabled": enabled_specs})


# ─────────────────────────────────────────────────────────────────────────────
# Status & Challenge Data API
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/status")
async def api_status():
    bus = get_bus()
    return JSONResponse({
        "challenges": bus.challenges,
        "cost": {
            "total_usd": bus.total_cost,
            "total_tokens": bus.total_tokens,
            "by_model": bus.cost_summary,
        },
        "ctfd": bus.ctfd_status,
    })


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
    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    from ui.coordinator_bridge import get_operator_inbox
    inbox = get_operator_inbox()
    if inbox:
        inbox.put_nowait(message)
        return JSONResponse({"ok": True, "queued": message[:200]})

    import json as _json
    import urllib.request

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


# ─────────────────────────────────────────────────────────────────────────────
# Run Control API
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/run/status")
async def api_run_status(user: User = Depends(_require_db_user)):
    mgr = get_run_manager()
    return JSONResponse({"ok": True, "status": mgr.status()})


@app.post("/api/run/start")
async def api_run_start(
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()

    # If ctf_id provided, load CTF's credentials and override user settings
    ctf_id: int | None = body.get("ctf_id")
    ctf_row: CTFModel | None = None
    if ctf_id:
        ctf_row = await db.get(CTFModel, int(ctf_id))
        if not ctf_row or ctf_row.user_id != user.id:
            return JSONResponse({"ok": False, "error": "CTF not found"}, status_code=404)

    st = await db.get(UserSettings, user.id)

    try:
        settings = Settings()
        if ctf_row:
            settings.ctfd_url = ctf_row.ctfd_url
            token = open_opt(ctf_row.ctfd_token_enc)
            if token:
                settings.ctfd_token = token
        elif st:
            if st.ctfd_url:
                settings.ctfd_url = st.ctfd_url
            token = open_opt(st.ctfd_token_enc) if st else None
            if token:
                settings.ctfd_token = token

        if st:
            settings.anthropic_api_key = open_opt(st.anthropic_api_key_enc) or ""
            settings.openai_api_key = open_opt(st.openai_api_key_enc) or ""
            settings.gemini_api_key = open_opt(st.gemini_api_key_enc) or ""
            settings.claude_cli_path = st.claude_cli_path or ""
            settings.claude_config_dir = st.claude_config_dir or ""

        max_concurrent = int(body.get("max_concurrent_challenges") or 10)
        settings.max_concurrent_challenges = max_concurrent
        get_run_manager().set_max_concurrent(max_concurrent)

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "hint": "Set APP_SECRET_KEY."}, status_code=500)

    # Model selection: user prefs → body override → default
    prefs_rows = (
        await db.execute(select(UserModelPref).where(UserModelPref.user_id == user.id))
    ).scalars().all()
    if prefs_rows:
        model_specs = [r.model_spec for r in prefs_rows if r.enabled]
    else:
        model_specs = list(DEFAULT_MODELS)

    # Body can override model specs
    if isinstance(body.get("model_specs"), list):
        model_specs = [s for s in body["model_specs"] if isinstance(s, str)]

    if not model_specs:
        model_specs = list(DEFAULT_MODELS)

    # Exclusions from user settings
    exclude_list: list[str] = []
    if st and st.exclude_challenges.strip():
        for line in st.exclude_challenges.splitlines():
            exclude_list.extend(p.strip() for p in line.split(",") if p.strip())
    exclude_rx = (st.exclude_challenge_regex.strip() if st else None) or None

    coordinator_backend = (body.get("coordinator") or "claude").strip()
    coordinator_model = body.get("coordinator_model") or None
    no_submit = bool(body.get("no_submit"))

    resp = await get_run_manager().start(
        user_id=user.id,
        settings=settings,
        model_specs=model_specs,
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
async def api_run_stop(request: Request, user: User = Depends(_require_db_user)):
    body = await request.json()
    force = bool(body.get("force"))
    resp = await get_run_manager().stop(user_id=user.id, force=force)
    return JSONResponse(resp, status_code=200 if resp.get("ok") else 403)


@app.post("/api/run/concurrency")
async def api_run_concurrency(request: Request, user: User = Depends(_require_db_user)):
    body = await request.json()
    n = int(body.get("max_concurrent") or 10)
    resp = get_run_manager().set_max_concurrent(n)
    return JSONResponse(resp)


@app.post("/api/run/challenge/{name}/stop")
async def api_challenge_stop(name: str, user: User = Depends(_require_db_user)):
    """Toggle stop state for a specific challenge. Sends an operator message."""
    mgr = get_run_manager()
    result = mgr.stop_challenge(name)
    # Notify coordinator
    from ui.coordinator_bridge import get_operator_inbox
    inbox = get_operator_inbox()
    if inbox:
        verb = "STOP_CHALLENGE" if result["stopped"] else "RESUME_CHALLENGE"
        inbox.put_nowait(f"{verb}: {name}")
    return JSONResponse(result)


@app.post("/api/run/challenge/{name}/priority")
async def api_challenge_priority(name: str, user: User = Depends(_require_db_user)):
    """Toggle priority flag for a specific challenge."""
    mgr = get_run_manager()
    result = mgr.toggle_priority(name)
    from ui.coordinator_bridge import get_operator_inbox
    inbox = get_operator_inbox()
    if inbox:
        verb = "PRIORITIZE_CHALLENGE" if result["priority"] else "UNPRIORITIZE_CHALLENGE"
        inbox.put_nowait(f"{verb}: {name}")
    return JSONResponse(result)


# ─────────────────────────────────────────────────────────────────────────────
# CLI Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

_CLAUDE_CTF_CONFIG_ROOT = os.path.join(os.path.expanduser("~"), ".claude-ctf-agents")
_CODEX_CTF_CONFIG_ROOT  = os.path.join(os.path.expanduser("~"), ".codex-ctf-agents")


def _user_claude_config_dir(user_id: int) -> str:
    return os.path.join(_CLAUDE_CTF_CONFIG_ROOT, str(user_id))


def _claude_is_authenticated(config_dir: str) -> bool:
    """Return True if a credentials file with non-empty content exists in config_dir."""
    for name in (".credentials.json", "credentials.json", ".auth.json", "auth.json"):
        path = os.path.join(config_dir, name)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = _json.load(f)
                if data:
                    return True
            except Exception:
                pass
    return False


async def _capture_cli_auth_url(
    cmd: list[str],
    env: dict,
    timeout: float = 12.0,
) -> tuple[list[str], str]:
    """Spawn *cmd*, collect stdout+stderr for *timeout* seconds and return (auth_urls, full_output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError:
        raise

    lines: list[str] = []

    async def _read(stream: asyncio.StreamReader) -> None:
        while True:
            try:
                raw = await asyncio.wait_for(stream.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                break
            if not raw:
                break
            lines.append(raw.decode("utf-8", errors="replace"))

    try:
        await asyncio.wait_for(
            asyncio.gather(_read(proc.stdout), _read(proc.stderr)),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        pass
    finally:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            pass

    output = "".join(lines)
    raw_urls = re.findall(r"https://\S+", output)
    urls = [u.rstrip(".,;)\"'") for u in raw_urls]
    return urls, output


# ─────────────────────────────────────────────────────────────────────────────
# Claude CLI Auth API
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/api/auth/claude/start")
async def api_claude_auth_start(
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Begin Claude Code CLI sign-in.

    Spawns the claude binary in a per-user config directory, captures any OAuth
    URL from its output, and returns it so the browser can open it.
    """
    st = await db.get(UserSettings, user.id)
    config_dir = (st.claude_config_dir if st else "") or _user_claude_config_dir(user.id)
    claude_bin = (st.claude_cli_path if st else "") or shutil.which("claude") or "claude"

    os.makedirs(config_dir, exist_ok=True)

    if _claude_is_authenticated(config_dir):
        return JSONResponse({"ok": True, "status": "authenticated", "config_dir": config_dir})

    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": config_dir,
        "CLAUDECODE": "",
        "DISPLAY": "",
        "BROWSER": "echo",          # prevent real browser open, just echo the URL
        "NO_COLOR": "1",
    }

    try:
        urls, output = await _capture_cli_auth_url([claude_bin], env=env, timeout=12.0)
    except FileNotFoundError:
        return JSONResponse(
            {
                "ok": False,
                "error": "Claude CLI not found on this server.",
                "hint": "Install Claude Code: npm install -g @anthropic-ai/claude-code",
            },
            status_code=404,
        )

    # Prefer claude.ai or anthropic.com URLs
    auth_urls = [u for u in urls if any(d in u for d in ("claude.ai", "anthropic.com"))]
    if not auth_urls:
        auth_urls = urls  # fall back to any URL found

    if auth_urls:
        return JSONResponse(
            {
                "ok": True,
                "status": "pending",
                "auth_url": auth_urls[0],
                "config_dir": config_dir,
            }
        )

    # Re-check: the process might have completed auth before we could read a URL
    if _claude_is_authenticated(config_dir):
        return JSONResponse({"ok": True, "status": "authenticated", "config_dir": config_dir})

    return JSONResponse(
        {
            "ok": True,
            "status": "manual",
            "config_dir": config_dir,
            "message": (
                "Could not capture the auth URL automatically "
                "(claude may need a TTY). Follow the manual steps below."
            ),
        }
    )


@app.get("/api/auth/claude/check")
async def api_claude_auth_check(
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll whether Claude CLI auth has completed for this user."""
    st = await db.get(UserSettings, user.id)
    config_dir = (st.claude_config_dir if st else "") or _user_claude_config_dir(user.id)

    if _claude_is_authenticated(config_dir):
        # Persist the config_dir so solvers can find it
        if not (st and st.claude_config_dir):
            if not st:
                st = UserSettings(user_id=user.id)
                db.add(st)
            st.claude_config_dir = config_dir
            st.updated_at = datetime.now(timezone.utc)
            await db.commit()
        return JSONResponse({"ok": True, "status": "authenticated", "config_dir": config_dir})

    return JSONResponse({"ok": True, "status": "pending"})


# ─────────────────────────────────────────────────────────────────────────────
# Codex CLI Auth API
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/api/auth/codex/start")
async def api_codex_auth_start(
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Begin Codex / OpenAI CLI sign-in.

    Tries `codex auth login` (or `codex login`) to get an OAuth URL.
    Falls back to instructions for entering an OpenAI API key directly.
    """
    st = await db.get(UserSettings, user.id)

    # If user already has an API key stored, report authenticated
    try:
        api_key = open_opt(st.openai_api_key_enc) if st and st.openai_api_key_enc else ""
    except Exception:
        api_key = ""
    if api_key:
        return JSONResponse({"ok": True, "status": "authenticated"})

    codex_bin = shutil.which("codex")
    if not codex_bin:
        return JSONResponse(
            {
                "ok": False,
                "error": "Codex CLI not found on this server.",
                "hint": "Install Codex: npm install -g @openai/codex",
                "alt": "Or paste your OpenAI API key in the field above.",
            },
            status_code=404,
        )

    env = {**os.environ, "DISPLAY": "", "BROWSER": "echo", "NO_COLOR": "1"}

    # Try common codex auth subcommands
    for subcmd in (["auth", "login"], ["login"], ["auth"]):
        try:
            urls, _ = await _capture_cli_auth_url([codex_bin, *subcmd], env=env, timeout=10.0)
        except FileNotFoundError:
            break

        auth_urls = [
            u for u in urls
            if any(d in u for d in ("openai.com", "auth0.com", "platform.openai"))
        ]
        if not auth_urls:
            auth_urls = urls
        if auth_urls:
            return JSONResponse(
                {"ok": True, "status": "pending", "auth_url": auth_urls[0]}
            )

    return JSONResponse(
        {
            "ok": True,
            "status": "manual",
            "message": "Could not get auth URL from codex CLI. Enter your OpenAI API key instead.",
            "api_key_url": "https://platform.openai.com/api-keys",
        }
    )


@app.get("/api/auth/codex/check")
async def api_codex_auth_check(
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Check whether Codex / OpenAI auth is complete for this user."""
    st = await db.get(UserSettings, user.id)
    try:
        api_key = open_opt(st.openai_api_key_enc) if st and st.openai_api_key_enc else ""
    except Exception:
        api_key = ""
    if api_key:
        return JSONResponse({"ok": True, "status": "authenticated"})
    return JSONResponse({"ok": True, "status": "pending"})


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket
# ─────────────────────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
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
# Startup / Entry point
# ─────────────────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def on_startup():
    logger.info("CTF Agent UI starting at http://%s:%d", UI_HOST, UI_PORT)


def run():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%X",
    )
    uvicorn.run("ui.server:app", host=UI_HOST, port=UI_PORT, reload=False, log_level="info")


if __name__ == "__main__":
    run()
