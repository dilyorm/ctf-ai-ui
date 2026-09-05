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
import time
import uuid
from datetime import UTC, datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from backend.account_pool import get_account_pool
from backend.auth import hash_password, verify_password
from backend.cli_auth import (
    ANTIGRAVITY_CONFIG_ROOT,
    CLAUDE_CONFIG_ROOT,
    CODEX_CONFIG_ROOT,
    GROK_CONFIG_ROOT,
    claude_is_authenticated,
    codex_is_authenticated,
    is_authenticated as cli_is_authenticated,
)
from backend.config import Settings
from backend.crypto import open_opt, seal_opt
from backend.db import get_db
from backend.db_models import CTF as CTFModel
from backend.db_models import PooledAccount, User, UserModelPref, UserSettings
from backend.models import ALL_MODELS, DEFAULT_MODELS
from backend.run_manager import get_run_manager
from ui.event_bus import get_bus
from ui.team_routes import register_team_routes
from ui.github_auth import (
    build_authorize_url,
    exchange_code_for_token,
    fetch_github_user,
    generate_state,
)

def _configure_logging() -> None:
    """Set up logging however the app was started.

    `run()` is only used by `python -m ui.server`; production runs
    `uvicorn ui.server:app` directly, so configuring inside run() left the root
    logger at WARNING and hid every backend INFO message — which is why a run's
    pool decisions were invisible in the journal.
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
        datefmt="%X",
    )
    for noisy in ("httpx", "httpcore", "botocore", "urllib3", "aiodocker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_logging()

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


async def _require_admin(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await _require_user(request, db)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


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
            (
                await db.execute(
                    select(CTFModel)
                    .where(CTFModel.user_id == int(user["user_id"]))
                    .order_by(CTFModel.id.desc())
                )
            )
            .scalars()
            .all()
        )
        ctfs = [{"id": c.id, "name": c.name, "ctfd_url": c.ctfd_url} for c in rows]

    return templates.TemplateResponse(
        request=request,
        name="app.html",
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
            "codex_cli_path": st.codex_cli_path or "",
            "codex_config_dir": st.codex_config_dir or "",
            "exclude_challenges": st.exclude_challenges or "",
            "exclude_challenge_regex": st.exclude_challenge_regex or "",
            "has_anthropic": bool(st.anthropic_api_key_enc),
            "has_openai": bool(st.openai_api_key_enc),
            "has_gemini": bool(st.gemini_api_key_enc),
            "has_copilot": bool(getattr(st, "github_copilot_oauth_token_enc", b"")),
        }

    # Load model prefs
    prefs_rows = (
        (await db.execute(select(UserModelPref).where(UserModelPref.user_id == user_id)))
        .scalars()
        .all()
    )
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


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request=request, name="accounts.html", context={"user": user}
    )


@app.get("/ctfs", response_class=HTMLResponse)
async def ctfs_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login")
    user_id = int(user["user_id"])

    rows = (
        (
            await db.execute(
                select(CTFModel).where(CTFModel.user_id == user_id).order_by(CTFModel.id.desc())
            )
        )
        .scalars()
        .all()
    )
    ctfs = [
        {
            "id": c.id,
            "name": c.name,
            "platform": (c.platform or "ctfd"),
            "ctfd_url": c.ctfd_url,
            "created_at": c.created_at.strftime("%Y-%m-%d"),
        }
        for c in rows
    ]

    return templates.TemplateResponse(
        request=request,
        name="ctfs.html",
        context={"user": user, "ctfs": ctfs},
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, db: AsyncSession = Depends(get_db)):
    sess = _get_user(request)
    if not sess:
        return RedirectResponse("/login")
    me = await db.get(User, int(sess["user_id"]))
    if not me or me.role != "admin":
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": "Admin access required."},
            status_code=403,
        )
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"user": sess},
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
            context={
                "error": "Invalid credentials.",
                "github_login_enabled": bool(GITHUB_CLIENT_ID),
            },
            status_code=401,
        )
    request.session["user"] = {
        "user_id": user.id,
        "email": user.email,
        "role": user.role,
        "name": user.display_name or user.email.split("@")[0],
    }
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

    gh_user = await fetch_github_user(token)
    if not gh_user:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": "Failed to fetch GitHub user profile."},
            status_code=400,
        )

    # Login-only: admin creates accounts. GitHub OAuth must match an existing
    # account by email (no auto-provisioning).
    from backend.db import SessionLocal

    async with SessionLocal() as db:
        gh_email = (gh_user.get("email") or "").strip().lower()
        db_user = None
        if gh_email:
            db_user = (
                await db.execute(select(User).where(User.email == gh_email))
            ).scalar_one_or_none()
        if not db_user or not db_user.is_active:
            return templates.TemplateResponse(
                request=request,
                name="error.html",
                context={
                    "message": "No account for this GitHub email. Ask an admin to create one."
                },
                status_code=403,
            )
        request.session["user"] = {
            "user_id": db_user.id,
            "email": db_user.email,
            "role": db_user.role,
            "login": gh_user.get("login"),
            "name": db_user.display_name or gh_user.get("name") or gh_user.get("login"),
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
    return JSONResponse(
        {"authenticated": True, "user": {k: v for k, v in user.items() if k != "access_token"}}
    )


# ─────────────────────────────────────────────────────────────────────────────
# Admin API (user CRUD — admin role only)
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/admin/users")
async def api_admin_list_users(
    admin: User = Depends(_require_admin), db: AsyncSession = Depends(get_db)
):
    rows = (await db.execute(select(User).order_by(User.id))).scalars().all()
    return JSONResponse(
        {
            "ok": True,
            "users": [
                {
                    "id": u.id,
                    "email": u.email,
                    "display_name": u.display_name,
                    "role": u.role,
                    "is_active": u.is_active,
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                }
                for u in rows
            ],
        }
    )


@app.post("/api/admin/users")
async def api_admin_create_user(
    request: Request,
    admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    password = (body.get("password") or "").strip()
    role = (body.get("role") or "member").strip()
    display_name = (body.get("display_name") or "").strip()

    if not email or not password or len(password) < 8:
        return JSONResponse(
            {"ok": False, "error": "email and password (min 8 chars) required"}, status_code=400
        )
    if role not in ("admin", "member"):
        return JSONResponse({"ok": False, "error": "invalid role"}, status_code=400)

    exists = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if exists:
        return JSONResponse({"ok": False, "error": "email already exists"}, status_code=409)

    user = User(
        email=email,
        password_hash=hash_password(password),
        role=role,
        display_name=display_name,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return JSONResponse(
        {
            "ok": True,
            "user": {
                "id": user.id,
                "email": user.email,
                "display_name": user.display_name,
                "role": user.role,
                "is_active": user.is_active,
            },
        },
        status_code=201,
    )


@app.patch("/api/admin/users/{user_id}")
async def api_admin_update_user(
    user_id: int,
    request: Request,
    admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    target = await db.get(User, user_id)
    if not target:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    if "role" in body:
        new_role = (body["role"] or "").strip()
        if new_role not in ("admin", "member"):
            return JSONResponse({"ok": False, "error": "invalid role"}, status_code=400)
        # Don't let the last admin demote themselves and lock the system.
        if target.id == admin.id and new_role != "admin":
            admin_count = (
                await db.execute(
                    select(User).where(User.role == "admin", User.is_active.is_(True))
                )
            ).scalars().all()
            if len(admin_count) <= 1:
                return JSONResponse(
                    {"ok": False, "error": "cannot demote the only active admin"},
                    status_code=400,
                )
        target.role = new_role
    if "is_active" in body:
        new_active = bool(body["is_active"])
        if target.id == admin.id and not new_active:
            return JSONResponse(
                {"ok": False, "error": "cannot deactivate yourself"}, status_code=400
            )
        target.is_active = new_active
    if "display_name" in body:
        target.display_name = (body["display_name"] or "").strip()
    if "password" in body and body["password"]:
        pw = str(body["password"]).strip()
        if len(pw) < 8:
            return JSONResponse(
                {"ok": False, "error": "password must be at least 8 chars"}, status_code=400
            )
        target.password_hash = hash_password(pw)

    await db.commit()
    await db.refresh(target)
    return JSONResponse(
        {
            "ok": True,
            "user": {
                "id": target.id,
                "email": target.email,
                "display_name": target.display_name,
                "role": target.role,
                "is_active": target.is_active,
            },
        }
    )


@app.delete("/api/admin/users/{user_id}")
async def api_admin_delete_user(
    user_id: int,
    admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(User, user_id)
    if not target:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if target.id == admin.id:
        return JSONResponse({"ok": False, "error": "cannot delete yourself"}, status_code=400)
    await db.delete(target)
    await db.commit()
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# Settings API (replaces /api/config)
# ─────────────────────────────────────────────────────────────────────────────


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
                "claude_cli_path": st.claude_cli_path,
                "claude_config_dir": st.claude_config_dir,
                "codex_cli_path": st.codex_cli_path,
                "codex_config_dir": st.codex_config_dir,
                "exclude_challenges": st.exclude_challenges,
                "exclude_challenge_regex": st.exclude_challenge_regex,
                "has_anthropic_key": bool(st.anthropic_api_key_enc),
                "has_openai_key": bool(st.openai_api_key_enc),
                "has_gemini_key": bool(st.gemini_api_key_enc),
                "has_copilot_token": bool(getattr(st, "github_copilot_oauth_token_enc", b"")),
            },
        }
    )


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
        if "codex_cli_path" in body:
            st.codex_cli_path = (body["codex_cli_path"] or "").strip()
        if "codex_config_dir" in body:
            st.codex_config_dir = (body["codex_config_dir"] or "").strip()
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
        if "github_copilot_oauth_token" in body:
            raw = (body["github_copilot_oauth_token"] or "").strip()
            st.github_copilot_oauth_token_enc = seal_opt(raw)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "hint": "Set APP_SECRET_KEY."}, status_code=500
        )

    st.updated_at = datetime.now(UTC)
    await db.commit()
    return JSONResponse({"ok": True})


@app.get("/api/settings/copilot/models")
async def api_copilot_test(
    user: User = Depends(_require_db_user), db: AsyncSession = Depends(get_db)
):
    """Verify the saved GitHub Copilot OAuth token by exchanging it for a
    Copilot session token and listing the models the account has access to.
    Use this from the Settings page to check the credential before adding
    ``copilot/<model>`` specs to the model picker."""
    st = await db.get(UserSettings, user.id)
    oauth = open_opt(getattr(st, "github_copilot_oauth_token_enc", b"")) if st else ""
    if not oauth:
        return JSONResponse(
            {"ok": False, "error": "no GitHub Copilot OAuth token saved"},
            status_code=400,
        )
    try:
        from backend.copilot_auth import list_models

        models = list_models(oauth)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    # Surface a compact view: id, vendor, capabilities.chat (when present).
    out = []
    for m in models:
        out.append(
            {
                "id": m.get("id") or m.get("name") or "",
                "vendor": m.get("vendor") or m.get("vendor_name") or "",
                "name": m.get("name") or m.get("id") or "",
                "supports_chat": bool(
                    (m.get("capabilities") or {}).get("type") == "chat"
                    or (m.get("capabilities") or {}).get("supports", {}).get("streaming")
                ),
                "model_picker_enabled": bool(m.get("model_picker_enabled", True)),
            }
        )
    return JSONResponse({"ok": True, "models": out})


@app.post("/api/auth/copilot/device/start")
async def api_copilot_device_start(
    request: Request, user: User = Depends(_require_db_user)
):
    """Begin GitHub OAuth Device Flow for Copilot. Returns user_code +
    verification_uri the user enters at github.com/login/device, plus a
    poll interval. The device_code is stashed in the session — never sent
    to the browser."""
    from backend.copilot_auth import start_device_flow, CopilotAuthError

    try:
        data = await start_device_flow()
    except CopilotAuthError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)

    request.session["copilot_device_code"] = data["device_code"]
    return JSONResponse(
        {
            "ok": True,
            "user_code": data["user_code"],
            "verification_uri": data.get("verification_uri") or "https://github.com/login/device",
            "interval": int(data.get("interval", 5)),
            "expires_in": int(data.get("expires_in", 900)),
        }
    )


@app.post("/api/auth/copilot/device/poll")
async def api_copilot_device_poll(
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll GitHub for the access token. On success, encrypts and stores
    the token in UserSettings.github_copilot_oauth_token_enc and verifies
    it works against the Copilot session-token endpoint."""
    from backend.copilot_auth import (
        poll_device_flow,
        get_session_token,
        CopilotAuthError,
    )

    device_code = request.session.get("copilot_device_code")
    if not device_code:
        return JSONResponse(
            {"ok": False, "error": "No device flow in progress. Click Sign in again."},
            status_code=400,
        )

    try:
        result = await poll_device_flow(device_code)
    except CopilotAuthError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)

    status = result.get("status")
    if status in ("pending", "slow_down"):
        return JSONResponse({"ok": True, "status": status})
    if status in ("expired", "denied"):
        request.session.pop("copilot_device_code", None)
        return JSONResponse({"ok": True, "status": status})
    if status != "ok":
        return JSONResponse({"ok": False, "error": f"unexpected status: {status}"}, status_code=500)

    token = result["access_token"]

    # Verify the freshly minted token actually has Copilot access before saving.
    try:
        await asyncio.to_thread(get_session_token, token)
    except CopilotAuthError as e:
        request.session.pop("copilot_device_code", None)
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "GitHub authorized us, but the account doesn't have Copilot "
                    f"access: {e}"
                ),
            },
            status_code=502,
        )

    st = await db.get(UserSettings, user.id)
    if not st:
        st = UserSettings(user_id=user.id)
        db.add(st)
    st.github_copilot_oauth_token_enc = seal_opt(token)
    st.updated_at = datetime.now(timezone.utc)
    await db.commit()
    request.session.pop("copilot_device_code", None)
    return JSONResponse({"ok": True, "status": "connected"})


# ─────────────────────────────────────────────────────────────────────────────
# CTF Management API
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/ctfs")
async def api_list_ctfs(user: User = Depends(_require_db_user), db: AsyncSession = Depends(get_db)):
    rows = (
        (
            await db.execute(
                select(CTFModel).where(CTFModel.user_id == user.id).order_by(CTFModel.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return JSONResponse(
        {
            "ok": True,
            "ctfs": [
                {
                    "id": c.id,
                    "name": c.name,
                    "platform": c.platform or "ctfd",
                    "ctfd_url": c.ctfd_url,
                    "created_at": c.created_at.isoformat(),
                }
                for c in rows
            ],
        }
    )


@app.post("/api/ctfs")
async def api_create_ctf(
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    from backend.platforms import SUPPORTED_PLATFORMS

    body = await request.json()
    name = (body.get("name") or "").strip()
    ctfd_url = (body.get("ctfd_url") or body.get("url") or "").strip()
    ctfd_token = (body.get("ctfd_token") or body.get("token") or "").strip()
    platform = (body.get("platform") or "ctfd").strip().lower()
    api_base = (body.get("api_base") or "/api/v1").strip() or "/api/v1"
    if not api_base.startswith("/"):
        api_base = "/" + api_base

    # Generic-platform adapter spec (from the connector wizard). Accept a dict or
    # a JSON string; store as a compact JSON string.
    adapter_json = ""
    adapter_in = body.get("adapter")
    if isinstance(adapter_in, dict):
        adapter_json = _json.dumps(adapter_in)
    elif isinstance(adapter_in, str) and adapter_in.strip():
        try:
            adapter_json = _json.dumps(_json.loads(adapter_in))
        except _json.JSONDecodeError:
            return JSONResponse({"ok": False, "error": "adapter is not valid JSON"}, status_code=400)
    if platform == "generic" and not adapter_json:
        return JSONResponse(
            {"ok": False, "error": "a generic platform needs an adapter (run the connector probe first)"},
            status_code=400,
        )

    if platform not in SUPPORTED_PLATFORMS:
        return JSONResponse(
            {"ok": False, "error": f"platform must be one of {SUPPORTED_PLATFORMS}"},
            status_code=400,
        )
    if not name or not ctfd_url:
        return JSONResponse(
            {"ok": False, "error": "name and URL are required"}, status_code=400
        )

    existing = (
        await db.execute(select(CTFModel).where(CTFModel.user_id == user.id, CTFModel.name == name))
    ).scalar_one_or_none()
    if existing:
        return JSONResponse(
            {"ok": False, "error": "CTF with this name already exists"}, status_code=409
        )

    try:
        token_enc = seal_opt(ctfd_token)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "hint": "Set APP_SECRET_KEY."}, status_code=500
        )

    ctf = CTFModel(
        user_id=user.id,
        name=name,
        platform=platform,
        ctfd_url=ctfd_url,
        ctfd_token_enc=token_enc,
        api_base=api_base,
        adapter_json=adapter_json,
    )
    db.add(ctf)
    await db.commit()
    await db.refresh(ctf)

    return JSONResponse(
        {
            "ok": True,
            "ctf": {
                "id": ctf.id,
                "name": ctf.name,
                "platform": ctf.platform,
                "ctfd_url": ctf.ctfd_url,
            },
        },
        status_code=201,
    )


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
# Connect-any-platform: the connector agent probes a site and asks for what it
# cannot infer, then validates the drafted adapter before it is saved.
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/api/platform/probe")
async def api_platform_probe(request: Request, user: User = Depends(_require_db_user)):
    """Probe a platform URL and return a draft adapter + any operator questions."""
    from backend.platforms.probe import probe_platform

    body = await request.json() if await request.body() else {}
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url is required"}, status_code=400)
    from backend.net_guard import assert_public_url

    try:
        await assert_public_url(url)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    token = (body.get("token") or "").strip()
    context = (body.get("context") or "").strip()
    hint = (body.get("platform_hint") or "auto").strip().lower()
    try:
        result = await probe_platform(url, token=token, context=context, platform_hint=hint)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"probe failed: {e}"}, status_code=502)
    return JSONResponse({"ok": True, **result.to_dict()})


@app.post("/api/platform/validate")
async def api_platform_validate(request: Request, user: User = Depends(_require_db_user)):
    """Validate a drafted adapter by listing challenges and rejecting a wrong flag."""
    from backend.platforms.probe import validate_adapter

    body = await request.json() if await request.body() else {}
    url = (body.get("url") or "").strip()
    token = (body.get("token") or "").strip()
    adapter = body.get("adapter")
    if not url or not isinstance(adapter, dict):
        return JSONResponse({"ok": False, "error": "url and adapter are required"}, status_code=400)
    from backend.net_guard import assert_public_url

    try:
        await assert_public_url(url)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    try:
        ok, message = await validate_adapter(url, token, adapter)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"validation failed: {e}"}, status_code=502)
    return JSONResponse({"ok": ok, "message": message})


# ─────────────────────────────────────────────────────────────────────────────
# Model Preferences API
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/models/available")
async def api_available_models():
    return JSONResponse({"ok": True, "models": ALL_MODELS})


@app.get("/api/models")
async def api_get_models(
    user: User = Depends(_require_db_user), db: AsyncSession = Depends(get_db)
):
    rows = (
        (await db.execute(select(UserModelPref).where(UserModelPref.user_id == user.id)))
        .scalars()
        .all()
    )
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

    # Update existing prefs in place; insert any specs missing for this user.
    # (delete+insert in one transaction trips the uq_user_model_spec unique
    # constraint because ORM flush order isn't guaranteed.)
    existing = (
        (await db.execute(select(UserModelPref).where(UserModelPref.user_id == user.id)))
        .scalars()
        .all()
    )
    by_spec = {row.model_spec: row for row in existing}

    enabled_set = set(enabled_specs)
    all_specs = {m["spec"] for m in ALL_MODELS}

    for spec in all_specs:
        want = spec in enabled_set
        row = by_spec.get(spec)
        if row is None:
            db.add(UserModelPref(user_id=user.id, model_spec=spec, enabled=want))
        elif row.enabled != want:
            row.enabled = want

    # Drop any stale rows whose spec is no longer in the catalog.
    for spec, row in by_spec.items():
        if spec not in all_specs:
            await db.delete(row)

    await db.commit()
    return JSONResponse({"ok": True, "enabled": sorted(enabled_set & all_specs)})


# ─────────────────────────────────────────────────────────────────────────────
# Status & Challenge Data API
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/status")
async def api_status():
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


@app.get("/api/interventions")
async def api_interventions(challenge: str = "", model: str = "", limit: int = 50):
    """Who steered which agent — the human-intervention log (newest first)."""
    bus = get_bus()
    items = list(bus.interventions)
    if challenge:
        items = [i for i in items if i.get("challenge") == challenge]
    if model:
        items = [i for i in items if i.get("model") == model]
    return JSONResponse({"ok": True, "interventions": items[: max(1, min(limit, 200))]})


@app.post("/api/message")
async def api_message(request: Request):
    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    sess_user = request.session.get("user") or {}
    actor = sess_user.get("name") or sess_user.get("email") or "operator"
    challenge = (body.get("challenge") or "").strip()
    get_bus().emit_sync(
        "agent_intervention",
        {"actor": actor, "challenge": challenge, "model": "coordinator",
         "action": "message", "text": message[:400]},
    )

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


def _send_operator_message(message: str) -> bool:
    """Best-effort: send a message to the coordinator operator inbox.

    Returns True if queued, False otherwise.
    """
    msg = (message or "").strip()
    if not msg:
        return False

    try:
        from ui.coordinator_bridge import get_operator_inbox

        inbox = get_operator_inbox()
        if inbox:
            inbox.put_nowait(msg)
            return True
    except Exception:
        pass

    # Fallback to coordinator_loop's lightweight HTTP endpoint.
    try:
        import json as _json
        import urllib.request

        port = int(os.environ.get("MSG_PORT", "9400"))
        body_bytes = _json.dumps({"message": msg}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/msg",
            data=body_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            _ = resp.read()
        return True
    except Exception:
        return False


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

    # Require explicit CTF selection from the CTFs page.
    # This prevents the service from running against a baked-in / default CTF.
    ctf_id: int | None = body.get("ctf_id")
    if not ctf_id:
        return JSONResponse(
            {
                "ok": False,
                "error": "Select a CTF instance first (create one in /ctfs), then click Start.",
            },
            status_code=400,
        )

    # If ctf_id provided, load CTF's credentials and override user settings
    ctf_row: CTFModel | None = None
    if ctf_id:
        ctf_row = await db.get(CTFModel, int(ctf_id))
        if not ctf_row or ctf_row.user_id != user.id:
            return JSONResponse({"ok": False, "error": "CTF not found"}, status_code=404)

    st = await db.get(UserSettings, user.id)

    try:
        settings = Settings()
        if ctf_row:
            settings.platform = (ctf_row.platform or "ctfd").lower()
            settings.ctfd_url = ctf_row.ctfd_url
            settings.ctfd_api_base = getattr(ctf_row, "api_base", "/api/v1") or "/api/v1"
            settings.platform_adapter_json = getattr(ctf_row, "adapter_json", "") or ""
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
            settings.github_copilot_oauth_token = (
                open_opt(getattr(st, "github_copilot_oauth_token_enc", b"")) or ""
            )
            settings.claude_cli_path = st.claude_cli_path or ""
            settings.claude_config_dir = st.claude_config_dir or ""
            settings.codex_cli_path = st.codex_cli_path or ""
            settings.codex_config_dir = st.codex_config_dir or ""

        max_concurrent = int(body.get("max_concurrent_challenges") or 10)
        settings.max_concurrent_challenges = max_concurrent
        get_run_manager().set_max_concurrent(max_concurrent)

    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "hint": "Set APP_SECRET_KEY."}, status_code=500
        )

    # Model selection: user prefs → body override → default
    prefs_rows = (
        (await db.execute(select(UserModelPref).where(UserModelPref.user_id == user.id)))
        .scalars()
        .all()
    )
    model_specs = [r.model_spec for r in prefs_rows if r.enabled] if prefs_rows else []

    # Body can override model specs
    if isinstance(body.get("model_specs"), list):
        model_specs = [s for s in body["model_specs"] if isinstance(s, str)]

    # Auto: with nothing selected, use the strongest model each connected
    # subscription actually offers. Connecting an account is then the only
    # configuration a run needs — no model page to visit, and no stale spec to
    # leave a run stuck on one provider.
    # Needed by auto model selection below (the coordinator holds a seat on its
    # own provider), so resolve it before that runs.
    coordinator_backend = (body.get("coordinator") or "claude").strip()
    auto_models = not model_specs
    if auto_models:
        from backend.account_pool import get_account_pool
        from backend.model_discovery import auto_model_specs

        try:
            await get_account_pool().reload()
            # Size the selection to the seat budget for the requested
            # parallelism — every challenge runs every selected model.
            model_specs = await auto_model_specs(
                max_challenges=max_concurrent,
                coordinator_provider=coordinator_backend,
            )
        except Exception as e:
            logger.warning("Auto model selection failed: %s", e)
        if not model_specs:
            model_specs = list(DEFAULT_MODELS)

    # Exclusions from user settings
    exclude_list: list[str] = []
    if st and st.exclude_challenges.strip():
        for line in st.exclude_challenges.splitlines():
            exclude_list.extend(p.strip() for p in line.split(",") if p.strip())
    exclude_rx = (st.exclude_challenge_regex.strip() if st else None) or None

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
        auto_models=auto_models,
        # Use a stable port so the UI can always reach the operator endpoint
        # even when running out-of-process from the coordinator.
        msg_port=int(body.get("msg_port") or os.environ.get("MSG_PORT", "9400")),
    )
    return JSONResponse(resp, status_code=200 if resp.get("ok") else 409)


@app.post("/api/run/stop")
async def api_run_stop(request: Request, user: User = Depends(_require_db_user)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    # Default to force-stop so "Stop All" works reliably even if a run was started
    # from another session or the owner id is stale.
    force = bool(body.get("force", True))

    # Tell the coordinator to stop swarms immediately (even if the LLM call is blocked).
    _send_operator_message("STOP_ALL")

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

    # Optimistic UI update.
    try:
        from ui.event_bus import get_bus

        get_bus().emit_sync(
            "challenge_update",
            {
                "name": name,
                # On resume we don't know if a swarm is already running; treat
                # it as pending until the coordinator emits challenge_started.
                "status": "stopped" if result.get("stopped") else "pending",
            },
        )
    except Exception:
        pass

    # Notify coordinator
    verb = "STOP_CHALLENGE" if result["stopped"] else "RESUME_CHALLENGE"
    _send_operator_message(f"{verb}: {name}")
    return JSONResponse(result)


@app.post("/api/run/challenge/{name}/priority")
async def api_challenge_priority(name: str, user: User = Depends(_require_db_user)):
    """Toggle priority flag for a specific challenge."""
    mgr = get_run_manager()
    result = mgr.toggle_priority(name)
    verb = "PRIORITIZE_CHALLENGE" if result["priority"] else "UNPRIORITIZE_CHALLENGE"
    _send_operator_message(f"{verb}: {name}")
    return JSONResponse(result)


@app.post("/api/run/challenge/{name}/exclude")
async def api_challenge_exclude(name: str, user: User = Depends(_require_db_user)):
    """Toggle excluded state for a specific challenge.

    This is a run-scoped control: excluded challenges won't be auto-spawned again.
    We also treat exclude as a stop for the current run.
    """
    mgr = get_run_manager()
    result = mgr.toggle_exclude(name)

    # Optimistic UI update.
    try:
        from ui.event_bus import get_bus

        get_bus().emit_sync(
            "challenge_update",
            {
                "name": name,
                "status": "stopped" if result.get("excluded") else "pending",
            },
        )
    except Exception:
        pass

    verb = "EXCLUDE_CHALLENGE" if result.get("excluded") else "UNEXCLUDE_CHALLENGE"
    _send_operator_message(f"{verb}: {name}")
    # Excluding implies stopped
    if result.get("excluded"):
        _send_operator_message(f"STOP_CHALLENGE: {name}")
    return JSONResponse(result)


# ─────────────────────────────────────────────────────────────────────────────
# CLI Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

_CLAUDE_CTF_CONFIG_ROOT = os.path.join(os.path.expanduser("~"), ".claude-ctf-agents")
_CODEX_CTF_CONFIG_ROOT = os.path.join(os.path.expanduser("~"), ".codex-ctf-agents")


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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _extract_device_code(output: str) -> str | None:
    # Codex device auth prints codes like "J47X-LWCU1".
    cleaned = _strip_ansi(output)
    m = re.search(r"\b[A-Z0-9]{4,6}-[A-Z0-9]{4,6}\b", cleaned)
    return m.group(0) if m else None


async def _claude_cli_is_authenticated(claude_bin: str, config_dir: str) -> bool:
    env = {**os.environ, "CLAUDE_CONFIG_DIR": config_dir, "NO_COLOR": "1"}
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin,
            "auth",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError:
        return False

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=4.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False

    try:
        payload = _json.loads(out.decode("utf-8", errors="replace"))
        return bool(payload.get("loggedIn"))
    except Exception:
        return False


async def _codex_cli_is_authenticated(codex_bin: str, config_dir: str) -> bool:
    env = {
        **os.environ,
        "HOME": config_dir,
        "NO_COLOR": "1",
        "CODEX_DISABLE_TELEMETRY": "1",
    }
    env.pop("OPENAI_API_KEY", None)  # subscription auth should not depend on an API key
    try:
        proc = await asyncio.create_subprocess_exec(
            codex_bin,
            "login",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError:
        return False

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=4.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False

    text = (out or b"").decode("utf-8", errors="replace") + (err or b"").decode(
        "utf-8", errors="replace"
    )
    cleaned = _strip_ansi(text).lower()
    if "not logged in" in cleaned:
        return False
    # Best-effort: any status output that doesn't explicitly say not-logged-in is treated as logged in.
    return "logged" in cleaned or "signed" in cleaned or "token" in cleaned


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
            except TimeoutError:
                break
            if not raw:
                break
            lines.append(raw.decode("utf-8", errors="replace"))

    try:
        await asyncio.wait_for(
            asyncio.gather(_read(proc.stdout), _read(proc.stderr)),
            timeout=timeout,
        )
    except TimeoutError:
        pass
    finally:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            pass

    output = "".join(lines)
    output = _strip_ansi(output)
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

    if await _claude_cli_is_authenticated(claude_bin, config_dir) or _claude_is_authenticated(
        config_dir
    ):
        return JSONResponse({"ok": True, "status": "authenticated", "config_dir": config_dir})

    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": config_dir,
        "CLAUDECODE": "",
        "DISPLAY": "",
        "BROWSER": "echo",  # prevent real browser open, just echo the URL
        "NO_COLOR": "1",
    }

    # Use the dedicated auth flow; plain `claude` often doesn't print an OAuth URL.
    try:
        urls, output = await _capture_cli_auth_url(
            [claude_bin, "auth", "login", "--claudeai"],
            env=env,
            timeout=12.0,
        )
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
    if await _claude_cli_is_authenticated(claude_bin, config_dir) or _claude_is_authenticated(
        config_dir
    ):
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

    claude_bin = (st.claude_cli_path if st else "") or shutil.which("claude") or "claude"

    if await _claude_cli_is_authenticated(claude_bin, config_dir) or _claude_is_authenticated(
        config_dir
    ):
        # Persist the config_dir so solvers can find it
        if not (st and st.claude_config_dir):
            if not st:
                st = UserSettings(user_id=user.id)
                db.add(st)
            st.claude_config_dir = config_dir
            st.updated_at = datetime.now(UTC)
            await db.commit()
        return JSONResponse({"ok": True, "status": "authenticated", "config_dir": config_dir})

    return JSONResponse({"ok": True, "status": "pending"})


# ─────────────────────────────────────────────────────────────────────────────
# Codex CLI Auth API
# ─────────────────────────────────────────────────────────────────────────────


def _user_codex_config_dir(user_id: int) -> str:
    return os.path.join(_CODEX_CTF_CONFIG_ROOT, str(user_id))


def _codex_is_authenticated(config_dir: str) -> bool:
    """Return True if codex credentials exist in *config_dir* (used as HOME)."""
    candidates = [
        os.path.join(config_dir, ".config", "openai", "credentials.json"),
        os.path.join(config_dir, ".config", "openai", "auth.json"),
        os.path.join(config_dir, ".openai", "credentials.json"),
        os.path.join(config_dir, ".openai", "auth.json"),
        os.path.join(config_dir, ".config", "openai"),  # non-empty dir counts
    ]
    for path in candidates:
        if os.path.exists(path):
            if os.path.isdir(path):
                try:
                    if any(True for _ in os.scandir(path)):
                        return True
                except Exception:
                    pass
            else:
                try:
                    with open(path) as f:
                        data = _json.load(f)
                    if data:
                        return True
                except Exception:
                    pass
    return False


@app.post("/api/auth/codex/start")
async def api_codex_auth_start(
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Begin Codex CLI subscription sign-in.

    Uses a per-user HOME directory so each user's OAuth tokens are isolated.
    Tries `codex auth login` / `codex login` to capture an OAuth URL.
    Falls back to manual instructions.
    """
    st = await db.get(UserSettings, user.id)

    config_dir = (st.codex_config_dir if st else "") or _user_codex_config_dir(user.id)
    codex_bin = (st.codex_cli_path if st else "") or shutil.which("codex") or "codex"

    os.makedirs(config_dir, exist_ok=True)

    if await _codex_cli_is_authenticated(codex_bin, config_dir) or _codex_is_authenticated(
        config_dir
    ):
        return JSONResponse({"ok": True, "status": "authenticated", "config_dir": config_dir})

    if not shutil.which(codex_bin) and not os.path.isfile(codex_bin):
        return JSONResponse(
            {
                "ok": False,
                "error": "Codex CLI not found on this server.",
                "hint": "Install Codex: npm install -g @openai/codex",
                "alt": "Or paste your OpenAI API key in the API key field above.",
            },
            status_code=404,
        )

    env = {
        **os.environ,
        "HOME": config_dir,
        "DISPLAY": "",
        "BROWSER": "echo",
        "NO_COLOR": "1",
        "CODEX_DISABLE_TELEMETRY": "1",
    }
    env.pop("OPENAI_API_KEY", None)  # force subscription, not API key

    # Preferred flow: device auth (works in headless servers, prints URL + code).
    try:
        urls, output = await _capture_cli_auth_url(
            [codex_bin, "login", "--device-auth"],
            env=env,
            timeout=10.0,
        )
    except FileNotFoundError:
        urls, output = ([], "")

    device_code = _extract_device_code(output)
    auth_urls = [u for u in urls if any(d in u for d in ("openai.com", "chatgpt.com", "auth0.com"))]
    if not auth_urls:
        auth_urls = [u for u in urls if u]
    if auth_urls:
        payload = {
            "ok": True,
            "status": "pending",
            "auth_url": auth_urls[0],
            "config_dir": config_dir,
        }
        if device_code:
            payload["device_code"] = device_code
        return JSONResponse(payload)

    if await _codex_cli_is_authenticated(codex_bin, config_dir) or _codex_is_authenticated(
        config_dir
    ):
        return JSONResponse({"ok": True, "status": "authenticated", "config_dir": config_dir})

    return JSONResponse(
        {
            "ok": True,
            "status": "manual",
            "config_dir": config_dir,
            "message": "Could not get auth URL automatically. Follow the manual steps below.",
        }
    )


@app.get("/api/auth/codex/check")
async def api_codex_auth_check(
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll whether Codex CLI subscription auth has completed for this user."""
    st = await db.get(UserSettings, user.id)
    config_dir = (st.codex_config_dir if st else "") or _user_codex_config_dir(user.id)

    codex_bin = (st.codex_cli_path if st else "") or shutil.which("codex") or "codex"

    if await _codex_cli_is_authenticated(codex_bin, config_dir) or _codex_is_authenticated(
        config_dir
    ):
        if not (st and st.codex_config_dir):
            if not st:
                st = UserSettings(user_id=user.id)
                db.add(st)
            st.codex_config_dir = config_dir
            st.updated_at = datetime.now(UTC)
            await db.commit()
        return JSONResponse({"ok": True, "status": "authenticated", "config_dir": config_dir})

    return JSONResponse({"ok": True, "status": "pending"})


# ─────────────────────────────────────────────────────────────────────────────
# Per-agent control (one model on one challenge)
# ─────────────────────────────────────────────────────────────────────────────


def _live_swarm(challenge: str):
    """Return the running ChallengeSwarm for *challenge*, or None."""
    from ui.coordinator_bridge import get_current_deps

    deps = get_current_deps()
    if not deps:
        return None
    return deps.swarms.get(challenge)


@app.post("/api/run/agent/{action}")
async def api_agent_action(
    action: str, request: Request, user: User = Depends(_require_db_user)
):
    """Control a single agent: message | stop | pause | resume | restart.

    Body: {challenge, model_spec, text?}. model_spec carries slashes so it's
    passed in the body, not the path.
    """
    valid = ("message", "stop", "pause", "resume", "restart", "swap_account", "swap_model")
    if action not in valid:
        return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400)
    body = await request.json()
    challenge = (body.get("challenge") or "").strip()
    model_spec = (body.get("model_spec") or "").strip()
    swarm = _live_swarm(challenge)
    if not swarm:
        return JSONResponse(
            {"ok": False, "error": "no active swarm for that challenge"}, status_code=404
        )
    if action == "message":
        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "empty message"}, status_code=400)
        ok = swarm.message_agent(model_spec, text)
    elif action == "stop":
        ok = swarm.stop_agent(model_spec)
    elif action == "pause":
        ok = swarm.pause_agent(model_spec)
    elif action == "resume":
        ok = swarm.resume_agent(model_spec)
    elif action == "restart":
        ok = swarm.restart_agent(model_spec)
    elif action == "swap_account":
        acct_id = body.get("account_id")
        ok = swarm.swap_account(model_spec, int(acct_id) if acct_id else None)
    else:  # swap_model
        new_spec = (body.get("new_spec") or "").strip()
        if not new_spec:
            return JSONResponse({"ok": False, "error": "new_spec required"}, status_code=400)
        ok = swarm.swap_model(model_spec, new_spec)

    if ok:
        actor = getattr(user, "display_name", "") or getattr(user, "email", "") or "operator"
        get_bus().emit_sync(
            "agent_intervention",
            {
                "actor": actor,
                "challenge": challenge,
                "model": model_spec,
                "action": action,
                "text": (body.get("text") or body.get("new_spec") or "").strip()[:400],
            },
        )
    return JSONResponse({"ok": bool(ok), "action": action, "model_spec": model_spec})


@app.post("/api/run/agent/context")
async def api_agent_context(
    challenge: str = Form(...),
    model_spec: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(_require_db_user),
):
    """Upload a context file into a single agent's sandbox (/challenge/workspace)."""
    swarm = _live_swarm(challenge.strip())
    if not swarm:
        return JSONResponse({"ok": False, "error": "no active swarm"}, status_code=404)
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        return JSONResponse({"ok": False, "error": "file too large (max 25MB)"}, status_code=413)
    ok = await swarm.add_context_file(model_spec.strip(), file.filename or "context.bin", data)
    return JSONResponse({"ok": bool(ok), "filename": file.filename})


# ─────────────────────────────────────────────────────────────────────────────
# Shared account pool API (team-wide, multi-account failover)
# ─────────────────────────────────────────────────────────────────────────────


_CONFIG_ROOTS = {
    "claude": CLAUDE_CONFIG_ROOT,
    "codex": CODEX_CONFIG_ROOT,
    "grok": GROK_CONFIG_ROOT,
    "antigravity": ANTIGRAVITY_CONFIG_ROOT,
}


def _new_account_config_dir(provider: str, *, cli: bool = True) -> str:
    """Where a new account keeps its credentials.

    ``cli`` accounts get a real isolated directory; token accounts store their
    credential in ``secret_enc`` and only need a unique placeholder to satisfy
    the NOT NULL + UNIQUE constraint on the column. Antigravity can be connected
    either way, so the caller says which.
    """
    from backend.providers import TOKEN_POOL_PROVIDERS

    if not cli or provider in TOKEN_POOL_PROVIDERS:
        return f"{provider}:{uuid.uuid4().hex}"
    root = _CONFIG_ROOTS.get(provider, CODEX_CONFIG_ROOT)
    return os.path.join(root, f"acct-{uuid.uuid4().hex[:12]}")


def _rmtree_managed(config_dir: str) -> None:
    """Delete an account's config dir, but only under a root we own."""
    if config_dir and any(config_dir.startswith(r) for r in _CONFIG_ROOTS.values()):
        shutil.rmtree(config_dir, ignore_errors=True)


def _default_account_label(provider: str) -> str:
    """A readable auto-label, e.g. ``copilot-9f3ac1``.

    The old form sliced the config dir, which for token providers is
    ``"<provider>:<uuid>"`` — producing labels like ``copilot-copilot:``.
    """
    return f"{provider}-{uuid.uuid4().hex[:6]}"


# CLI subscription providers need their binary present on this host. Probing is
# cheap but not free (each `--version` spawns a process), so cache the result.
_CLI_BINARIES: dict[str, dict] = {
    "claude": {"bin": "claude", "install": "npm install -g @anthropic-ai/claude-code"},
    "codex": {"bin": "codex", "install": "npm install -g @openai/codex"},
    "grok": {"bin": "grok", "install": "curl -fsSL https://x.ai/cli/install.sh | bash"},
    "antigravity": {
        "bin": "agy",
        "install": "curl -fsSL https://antigravity.google/cli/install.sh | bash -s -- -d /usr/local/bin",
    },
}
_cli_probe_cache: tuple[float, dict] = (0.0, {})


async def _probe_cli(name: str, binary: str) -> dict:
    path = shutil.which(binary)
    if not path:
        return {"installed": False, "path": "", "version": ""}
    version = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            path, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        version = out.decode("utf-8", "replace").strip().splitlines()[0][:80] if out else ""
    except Exception as e:  # noqa: BLE001 — a missing/broken CLI is data, not a crash
        logger.debug("version probe for %s failed: %s", name, e)
    return {"installed": True, "path": path, "version": version}


@app.get("/api/accounts/capabilities")
async def api_account_capabilities(user: User = Depends(_require_db_user)):
    """Which subscription CLIs this server can actually drive.

    The accounts page calls this before showing the connect buttons, so a
    missing `claude` / `codex` binary is visible up front instead of surfacing
    as a 404 halfway through a sign-in.
    """
    global _cli_probe_cache
    now = time.time()
    cached_at, cached = _cli_probe_cache
    if cached and now - cached_at < 60:
        return JSONResponse({"ok": True, "providers": cached})
    results = await asyncio.gather(
        *(_probe_cli(name, meta["bin"]) for name, meta in _CLI_BINARIES.items())
    )
    providers = {
        name: {**res, "install": meta["install"]}
        for (name, meta), res in zip(_CLI_BINARIES.items(), results, strict=True)
    }
    _cli_probe_cache = (now, providers)
    return JSONResponse({"ok": True, "providers": providers})


def _account_authed(acct: PooledAccount) -> bool:
    """True if a pool account has usable credentials (token present, or CLI creds on disk).

    Delegates to ``backend.cli_auth.is_authenticated`` — the same resolver the
    account pool uses — so the accounts page and the pool can never disagree
    about whether a connected account is authenticated. (They did: grok is a
    ``cli`` provider, so it used to fall through to the codex check and every
    connected Grok account was reported "pending" forever.)
    """
    from backend.providers import TOKEN_POOL_PROVIDERS

    # Decide per account, not per provider: antigravity can be connected either
    # by signing a Google account into the `agy` CLI (real config dir) or by
    # pasting a Gemini key (credential in secret_enc).
    if acct.secret_enc or acct.provider in TOKEN_POOL_PROVIDERS:
        return bool(acct.secret_enc)
    return cli_is_authenticated(acct.provider, acct.config_dir)


async def _spawn_claude_signin(config_dir: str) -> dict:
    """Spawn the Claude CLI sign-in in *config_dir*; return status + auth_url."""
    claude_bin = shutil.which("claude") or "claude"
    os.makedirs(config_dir, exist_ok=True)
    if claude_is_authenticated(config_dir):
        return {"status": "authenticated"}
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": config_dir,
        "CLAUDECODE": "",
        "DISPLAY": "",
        "BROWSER": "echo",
        "NO_COLOR": "1",
    }
    try:
        urls, _ = await _capture_cli_auth_url([claude_bin], env=env, timeout=12.0)
    except FileNotFoundError:
        return {
            "error": "Claude CLI not found on this server.",
            "hint": "Install: npm install -g @anthropic-ai/claude-code",
            "status_code": 404,
        }
    auth_urls = [u for u in urls if any(d in u for d in ("claude.ai", "anthropic.com"))] or urls
    if auth_urls:
        return {"status": "pending", "auth_url": auth_urls[0]}
    if claude_is_authenticated(config_dir):
        return {"status": "authenticated"}
    return {"status": "manual", "message": "Could not capture auth URL; claude may need a TTY."}


async def _spawn_codex_signin(config_dir: str) -> dict:
    """Spawn the Codex CLI sign-in in *config_dir* (as HOME); return status + auth_url."""
    codex_bin = shutil.which("codex") or "codex"
    os.makedirs(config_dir, exist_ok=True)
    if codex_is_authenticated(config_dir):
        return {"status": "authenticated"}
    if not shutil.which(codex_bin) and not os.path.isfile(codex_bin):
        return {
            "error": "Codex CLI not found on this server.",
            "hint": "Install: npm install -g @openai/codex",
            "status_code": 404,
        }
    env = {
        **os.environ,
        "HOME": config_dir,
        "DISPLAY": "",
        "BROWSER": "echo",
        "NO_COLOR": "1",
        "CODEX_DISABLE_TELEMETRY": "1",
    }
    env.pop("OPENAI_API_KEY", None)
    for subcmd in (["auth", "login"], ["login"], ["auth"]):
        try:
            urls, _ = await _capture_cli_auth_url([codex_bin, *subcmd], env=env, timeout=10.0)
        except FileNotFoundError:
            break
        auth_urls = [
            u for u in urls if any(d in u for d in ("openai.com", "auth0.com", "chatgpt.com"))
        ] or [u for u in urls if u]
        if auth_urls:
            return {"status": "pending", "auth_url": auth_urls[0]}
    if codex_is_authenticated(config_dir):
        return {"status": "authenticated"}
    return {"status": "manual", "message": "Could not capture auth URL. Follow manual steps."}


@app.post("/api/accounts/token/verify")
async def api_account_token_verify(
    request: Request, user: User = Depends(_require_db_user)
):
    """Check a pasted subscription/API token and list the models it can reach.

    Used by the connect UI before saving a Grok / Kimi / Antigravity account so
    the operator sees the real model IDs (which change often) rather than guesses.
    """
    body = await request.json() if await request.body() else {}
    provider = (body.get("provider") or "").strip().lower()
    token = (body.get("token") or "").strip()
    from backend.providers import OPENAI_COMPAT_PROVIDERS
    from backend.token_providers import verify_token_provider

    if provider not in OPENAI_COMPAT_PROVIDERS:
        return JSONResponse({"ok": False, "error": "unknown token provider"}, status_code=400)
    result = await verify_token_provider(provider, token)
    return JSONResponse(result)


@app.post("/api/accounts/{provider}/token")
async def api_account_connect_token(
    provider: str,
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Connect a NEW token-based subscription account (Grok / Kimi / Antigravity).

    The operator pastes a subscription/API token; it is verified against the
    provider's /models endpoint, stored encrypted, and added to the shared pool.
    Any user may add any number of accounts.
    """
    from backend.providers import OPENAI_COMPAT_PROVIDERS
    from backend.token_providers import verify_token_provider

    provider = provider.lower().strip()
    if provider not in OPENAI_COMPAT_PROVIDERS:
        return JSONResponse({"ok": False, "error": "unknown token provider"}, status_code=400)

    body = await request.json() if await request.body() else {}
    token = (body.get("token") or "").strip()
    if not token:
        return JSONResponse({"ok": False, "error": "token is required"}, status_code=400)
    label = (body.get("label") or "").strip()
    try:
        max_conc = max(1, min(int(body.get("max_concurrent") or 1), 20))
    except (TypeError, ValueError):
        max_conc = 1

    # Verify unless the operator explicitly opts out (offline / rate-limited).
    verified_models: list[str] = []
    if body.get("skip_verify") is not True:
        result = await verify_token_provider(provider, token)
        if not result.get("ok"):
            return JSONResponse(
                {"ok": False, "error": result.get("error") or "token verification failed"},
                status_code=400,
            )
        verified_models = result.get("models", [])

    acct = PooledAccount(
        provider=provider,
        label=label or _default_account_label(provider),
        owner_user_id=user.id,
        config_dir=_new_account_config_dir(provider, cli=False),
        secret_enc=seal_opt(token),
        max_concurrent=max_conc,
    )
    db.add(acct)
    await db.commit()
    await db.refresh(acct)
    try:
        await get_account_pool().reload()
    except Exception as e:
        logger.warning("pool reload after token connect failed: %s", e)
    return JSONResponse(
        {"ok": True, "account_id": acct.id, "status": "authenticated", "models": verified_models}
    )


@app.get("/api/accounts")
async def api_accounts_list(
    user: User = Depends(_require_db_user), db: AsyncSession = Depends(get_db)
):
    """List every account in the shared pool with live runtime status."""
    rows = (await db.execute(select(PooledAccount))).scalars().all()
    # Map owner ids to emails for display.
    owner_ids = {r.owner_user_id for r in rows if r.owner_user_id}
    owners: dict[int, str] = {}
    if owner_ids:
        for u in (
            await db.execute(select(User).where(User.id.in_(owner_ids)))
        ).scalars().all():
            owners[u.id] = u.email
    live = {s["id"]: s for s in get_account_pool().snapshot()}
    out = []
    for r in rows:
        authed = _account_authed(r)
        snap = live.get(r.id)
        if not authed:
            status = "pending"
        elif snap:
            status = snap["status"]
        elif r.disabled:
            status = "disabled"
        else:
            status = "healthy"
        out.append(
            {
                "id": r.id,
                "provider": r.provider,
                "label": r.label or r.config_dir,
                "owner": owners.get(r.owner_user_id, ""),
                "owner_user_id": r.owner_user_id,
                "max_concurrent": r.max_concurrent,
                "disabled": r.disabled,
                "authenticated": authed,
                "status": status,
                "active_leases": (snap or {}).get("active_leases", 0),
                "cooldown_until": (snap or {}).get("cooldown_until")
                or (r.cooldown_until.isoformat() if r.cooldown_until else None),
            }
        )
    out.sort(key=lambda a: (a["provider"], a["id"]))
    return JSONResponse({"ok": True, "accounts": out})


@app.post("/api/accounts/{provider}/start")
async def api_account_connect_start(
    provider: str,
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Begin connecting a NEW account to the shared pool via CLI web sign-in.

    Each connect creates its own isolated config dir + pool row, so any user can
    add as many accounts as they want.
    """
    if provider not in ("claude", "codex", "copilot", "grok", "antigravity"):
        return JSONResponse({"ok": False, "error": "unknown provider"}, status_code=400)
    body = await request.json() if await request.body() else {}
    label = (body.get("label") or "").strip()

    config_dir = _new_account_config_dir(provider)
    acct = PooledAccount(
        provider=provider,
        label=label or _default_account_label(provider),
        owner_user_id=user.id,
        config_dir=config_dir,
        max_concurrent=int(body.get("max_concurrent") or 1),
    )
    db.add(acct)
    await db.commit()
    await db.refresh(acct)

    # Copilot: GitHub OAuth device flow (token), not a CLI config-dir sign-in.
    if provider == "copilot":
        from backend.copilot_auth import CopilotAuthError, start_device_flow

        try:
            data = await start_device_flow()
        except CopilotAuthError as e:
            await db.delete(acct)
            await db.commit()
            return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
        request.session[f"copilot_pool_device_{acct.id}"] = data["device_code"]
        return JSONResponse(
            {
                "ok": True,
                "account_id": acct.id,
                "status": "device",
                "user_code": data["user_code"],
                "verification_uri": data.get("verification_uri") or "https://github.com/login/device",
                "interval": int(data.get("interval", 5)),
                "expires_in": int(data.get("expires_in", 900)),
            }
        )

    # Claude (PTY setup-token, code paste) / Codex (device-auth) via the
    # interactive connect manager, which holds the live CLI session.
    from backend.connect_manager import get_connect_manager

    mgr = get_connect_manager()
    if provider == "claude":
        result = await mgr.start_claude(acct.id, config_dir)
    elif provider == "grok":
        result = await mgr.start_grok(acct.id, config_dir)
    elif provider == "antigravity":
        result = await mgr.start_antigravity(acct.id, config_dir)
    else:
        result = await mgr.start_codex(acct.id, config_dir)

    if "error" in result:
        await db.delete(acct)
        await db.commit()
        return JSONResponse({"ok": False, **result}, status_code=result.get("status_code", 400))
    return JSONResponse({"ok": True, "account_id": acct.id, **result})


@app.post("/api/accounts/{account_id}/code")
async def api_account_submit_code(
    account_id: int,
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit a pasted Claude authorization code into the live setup-token session."""
    acct = await db.get(PooledAccount, account_id)
    if not acct:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    body = await request.json()
    code = (body.get("code") or "").strip()
    if not code:
        return JSONResponse({"ok": False, "error": "no code"}, status_code=400)
    from backend.connect_manager import get_connect_manager

    ok = await get_connect_manager().submit_code(account_id, code)
    return JSONResponse({"ok": ok})


@app.post("/api/accounts/{account_id}/copilot/poll")
async def api_account_copilot_poll(
    account_id: int,
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll the GitHub device flow for a connecting Copilot pool account.

    On success: validate Copilot access, seal the token into the account, and
    reload the pool so it becomes leasable.
    """
    from backend.copilot_auth import CopilotAuthError, get_session_token, poll_device_flow

    acct = await db.get(PooledAccount, account_id)
    if not acct or acct.provider != "copilot":
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    sess_key = f"copilot_pool_device_{account_id}"
    device_code = request.session.get(sess_key)
    if not device_code:
        return JSONResponse(
            {"ok": False, "error": "No device flow in progress. Connect again."}, status_code=400
        )

    try:
        result = await poll_device_flow(device_code)
    except CopilotAuthError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)

    status = result.get("status")
    if status in ("pending", "slow_down"):
        return JSONResponse({"ok": True, "status": status})
    if status in ("expired", "denied"):
        request.session.pop(sess_key, None)
        return JSONResponse({"ok": True, "status": status})
    if status != "ok":
        return JSONResponse({"ok": False, "error": f"unexpected status: {status}"}, status_code=500)

    token = result["access_token"]
    try:
        await asyncio.to_thread(get_session_token, token)
    except CopilotAuthError as e:
        request.session.pop(sess_key, None)
        return JSONResponse(
            {"ok": False, "error": f"GitHub authorized, but no Copilot access: {e}"},
            status_code=502,
        )

    acct.secret_enc = seal_opt(token)
    await db.commit()
    request.session.pop(sess_key, None)
    await get_account_pool().reload()
    return JSONResponse({"ok": True, "status": "connected"})


@app.get("/api/accounts/{account_id}/check")
async def api_account_check(
    account_id: int,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll whether a connecting account finished sign-in.

    Three outcomes, so the browser never sits on "waiting…" forever:
    ``authenticated`` (creds on disk), ``failed`` (the CLI exited without
    writing creds — expired/denied device code, or the CLI errored), and
    ``pending`` (still waiting on the operator).
    """
    from backend.connect_manager import get_connect_manager

    acct = await db.get(PooledAccount, account_id)
    if not acct:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    mgr = get_connect_manager()
    if _account_authed(acct):
        await mgr.finish(account_id)
        await get_account_pool().reload()
        return JSONResponse({"ok": True, "status": "authenticated"})
    sess = mgr.status(account_id)
    if sess and sess.get("error"):
        # The CLI printed a failure. Say so instead of leaving the browser on
        # "finishing sign-in…" forever.
        error = sess["error"]
        if "400" in error:
            # The only thing a 400 on the code exchange ever means in practice.
            error += " — the code was already used, expired, or came from a different sign-in link."
        return JSONResponse(
            {
                "ok": True,
                "status": "failed",
                "error": error,
                "detail": sess.get("tail", ""),
                "can_retry": sess.get("can_retry", False),
            }
        )
    if sess and not sess.get("alive"):
        return JSONResponse(
            {
                "ok": True,
                "status": "failed",
                "error": "Sign-in ended without writing credentials. "
                "The code may have expired — start over.",
                "detail": sess.get("tail", ""),
                "can_retry": False,
            }
        )
    return JSONResponse(
        {"ok": True, "status": "pending", "expires_in": (sess or {}).get("expires_in")}
    )


@app.post("/api/accounts/{account_id}/cancel")
async def api_account_cancel(
    account_id: int,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Abandon an in-progress sign-in: kill the CLI and drop the half-made row.

    Closing the browser tab used to leave both behind — a `pending` account
    nobody can finish, and a `claude setup-token` / `grok login` process running
    on the server until the app restarted.
    """
    from backend.connect_manager import get_connect_manager

    await get_connect_manager().finish(account_id)
    acct = await db.get(PooledAccount, account_id)
    if acct and not _account_authed(acct):
        config_dir = acct.config_dir
        await db.delete(acct)
        await db.commit()
        _rmtree_managed(config_dir)
        await get_account_pool().reload()
    return JSONResponse({"ok": True})


@app.patch("/api/accounts/{account_id}")
async def api_account_update(
    account_id: int,
    request: Request,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Update label / max_concurrent / disabled for a pool account."""
    acct = await db.get(PooledAccount, account_id)
    if not acct:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    body = await request.json()
    if "label" in body:
        acct.label = (body["label"] or "").strip() or acct.label
    if "max_concurrent" in body:
        acct.max_concurrent = max(1, min(int(body["max_concurrent"]), 20))
    if "disabled" in body:
        acct.disabled = bool(body["disabled"])
    await db.commit()
    await get_account_pool().reload()
    return JSONResponse({"ok": True})


@app.delete("/api/accounts/{account_id}")
async def api_account_delete(
    account_id: int,
    user: User = Depends(_require_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove an account from the pool and delete its credentials from disk."""
    acct = await db.get(PooledAccount, account_id)
    if not acct:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    # Kill any live sign-in for this account first, or its CLI keeps running
    # (and holding the config dir) after the row and directory are gone.
    from backend.connect_manager import get_connect_manager

    await get_connect_manager().finish(account_id)
    config_dir = acct.config_dir
    await db.delete(acct)
    await db.commit()
    _rmtree_managed(config_dir)
    await get_account_pool().reload()
    return JSONResponse({"ok": True})


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
    except TimeoutError:
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


async def _bootstrap_admin_from_env() -> None:
    """Create an admin user from ADMIN_BOOTSTRAP_EMAIL/PASSWORD if no admin exists.

    Safe to run every startup. Does nothing if an admin already exists.
    """
    email = (os.environ.get("ADMIN_BOOTSTRAP_EMAIL") or "").strip().lower()
    password = (os.environ.get("ADMIN_BOOTSTRAP_PASSWORD") or "").strip()
    if not email or not password or len(password) < 8:
        return
    from backend.db import SessionLocal

    try:
        async with SessionLocal() as db:
            existing_admin = (
                await db.execute(select(User).where(User.role == "admin", User.is_active.is_(True)))
            ).scalars().first()
            if existing_admin:
                return
            match = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if match:
                match.role = "admin"
                match.is_active = True
                match.password_hash = hash_password(password)
                await db.commit()
                logger.info("Promoted existing user %s to admin via bootstrap env", email)
                return
            user = User(
                email=email,
                password_hash=hash_password(password),
                role="admin",
                is_active=True,
                display_name=email.split("@")[0],
            )
            db.add(user)
            await db.commit()
            logger.info("Bootstrapped admin account %s from env", email)
    except Exception as e:
        logger.warning("Admin bootstrap failed: %s", e)


@app.on_event("startup")
async def on_startup():
    logger.info("CTF Agent UI starting at http://%s:%d", UI_HOST, UI_PORT)
    await _bootstrap_admin_from_env()
    # Subscribe to the challenge-event bus so that solver updates reflect into
    # the /team kanban (Task.status, flag, solver status).
    asyncio.create_task(_task_bus_subscriber(), name="team-bus-subscriber")
    # Reap abandoned CLI sign-ins so `claude setup-token` / `codex login` /
    # `grok login` never linger after the operator closes the tab.
    from backend.connect_manager import get_connect_manager

    asyncio.create_task(get_connect_manager().reap_loop(), name="connect-session-reaper")


_RELEVANT_EVENT_TYPES = {
    "challenge_new",
    "challenge_update",
    "challenge_started",
    "challenge_solved",
    "challenge_failed",
}


async def _task_bus_subscriber() -> None:
    """Listen to the event bus and reconcile Task rows on solver progress."""
    bus = get_bus()
    queue = await bus.subscribe()
    try:
        while True:
            raw = await queue.get()
            try:
                msg = _json.loads(raw)
            except Exception:
                continue
            etype = msg.get("type")
            if etype not in _RELEVANT_EVENT_TYPES:
                continue
            data = msg.get("data") or {}
            name = data.get("name")
            if not name:
                continue
            await _reconcile_task_from_event(etype, name, data)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug("team bus subscriber stopped: %s", e)
    finally:
        await bus.unsubscribe(queue)


_WRITEUP_INFLIGHT: set[int] = set()


async def _auto_generate_writeup(task_id: int, name: str) -> None:
    """Background: generate a writeup for a freshly-solved task.

    Skips if the task already has one — solvers can re-emit `challenge_solved`
    on retries, and we don't want to clobber a manual edit.
    """
    if task_id in _WRITEUP_INFLIGHT:
        return
    _WRITEUP_INFLIGHT.add(task_id)
    try:
        from backend.db import SessionLocal
        from backend.db_models import Task
        from ui.event_bus import get_bus
        from ui.team_routes import _generate_writeup_md

        bus = get_bus()
        logs = list(bus.logs.get(name, []))
        ch_state = bus.challenges.get(name, {})

        async with SessionLocal() as db:
            t = await db.get(Task, task_id)
            if not t or (t.writeup_md or "").strip():
                return
            description = t.description_override_md or t.platform_description_md or ""
            flag = t.flag or ch_state.get("flag", "")
            category = t.category or ""
            points = t.points or 0

        # Run generation outside the DB session — it can take ~60s.
        # Convert log entries (may be dicts) to text the generator expects.
        log_lines: list[str] = []
        for entry in logs[-600:]:
            if isinstance(entry, str):
                log_lines.append(entry)
            else:
                try:
                    log_lines.append(_json.dumps(entry, default=str))
                except Exception:
                    log_lines.append(str(entry))

        writeup = await _generate_writeup_md(
            name=name,
            category=category,
            points=points,
            description=description,
            flag=flag,
            logs=log_lines,
        )

        async with SessionLocal() as db:
            t = await db.get(Task, task_id)
            if not t or (t.writeup_md or "").strip():
                return
            t.writeup_md = writeup
            t.updated_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info("Auto-generated writeup for task %d (%s)", task_id, name)
    except Exception as e:
        logger.warning("Auto writeup for %s failed: %s", name, e)
    finally:
        _WRITEUP_INFLIGHT.discard(task_id)


async def _reconcile_task_from_event(etype: str, name: str, data: dict) -> None:
    """Update any Task rows matching *name* with fresh status/flag from the solver."""
    from backend.db import SessionLocal
    from backend.db_models import Task

    status_raw = (data.get("status") or "").lower()
    flag = data.get("flag") or ""

    if etype == "challenge_solved":
        new_status = "solved"
    elif etype == "challenge_failed":
        new_status = "blocked"
    elif etype == "challenge_started":
        new_status = "in_progress"
    else:
        mapping = {
            "running": "in_progress",
            "started": "in_progress",
            "pending": "todo",
            "solved": "solved",
            "failed": "blocked",
            "error": "blocked",
            "stopped": "blocked",
        }
        new_status = mapping.get(status_raw, "")

    try:
        just_solved_ids: list[int] = []
        async with SessionLocal() as db:
            rows = (
                (await db.execute(select(Task).where(Task.name == name))).scalars().all()
            )
            if not rows:
                return
            now = datetime.now(timezone.utc)
            for t in rows:
                # Only advance forward; don't stomp solved tasks back to in_progress.
                became_solved = False
                if new_status and t.status != new_status:
                    if t.status == "solved" and new_status != "solved":
                        pass
                    else:
                        was_solved = t.status == "solved"
                        t.status = new_status
                        if new_status == "solved" and t.solved_at is None:
                            t.solved_at = now
                            became_solved = not was_solved
                if flag and not t.flag:
                    t.flag = flag
                if status_raw:
                    t.last_solver_status = status_raw
                elif etype:
                    t.last_solver_status = etype
                t.updated_at = now
                if became_solved and not (t.writeup_md or "").strip():
                    just_solved_ids.append(t.id)
            await db.commit()

        for tid in just_solved_ids:
            asyncio.create_task(
                _auto_generate_writeup(tid, name),
                name=f"auto-writeup-{tid}",
            )
    except Exception as e:
        logger.debug("reconcile Task for %s failed: %s", name, e)


register_team_routes(app, templates)


def run():
    uvicorn.run("ui.server:app", host=UI_HOST, port=UI_PORT, reload=False, log_level="info")


if __name__ == "__main__":
    run()
