"""GitHub Copilot auth — exchange a long-lived GitHub OAuth token for a
short-lived Copilot session token, and provide an httpx auth/header pipeline
that the OpenAI-compatible Copilot API accepts.

Copilot's chat-completions endpoint (https://api.githubcopilot.com) refuses
requests that don't carry the editor identification headers Copilot clients
send, so we plumb those through alongside the bearer token.

Auth flow:

1. User saves a GitHub OAuth/PAT token (any token tied to an account with
   Copilot access — `gh auth token` after `gh auth login --scopes 'read:user'`
   is the easiest source).
2. `fetch_session_token(oauth)` calls
   ``GET https://api.github.com/copilot_internal/v2/token`` with that token
   and returns a session token + expiry.
3. The session token is cached per-OAuth-token until ~60 s before expiry.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)

# Mimic a recent VS Code Copilot Chat client. Copilot's API rejects requests
# without these — they don't gate access, just identify the integration.
COPILOT_API_BASE = "https://api.githubcopilot.com"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
COPILOT_CLIENT_HEADERS: dict[str, str] = {
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
    "Copilot-Integration-Id": "vscode-chat",
    "User-Agent": "GitHubCopilotChat/0.22.0",
}

# GitHub OAuth Device Flow — the well-known VS Code Copilot Chat client ID.
# Tokens minted via this client + read:user scope are accepted by
# `copilot_internal/v2/token`. A regular PAT or a `gh auth token` from the gh
# CLI's own OAuth app is NOT, which is the most common cause of the 404 users
# see when pasting a token directly.
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_DEVICE_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_OAUTH_CLIENT_ID = "Iv1.b507a08c87ecfe98"
COPILOT_OAUTH_SCOPE = "read:user"


class CopilotAuthError(RuntimeError):
    """Raised when the GitHub OAuth token can't be exchanged for a session token."""


async def start_device_flow(*, timeout: float = 10.0) -> dict:
    """Kick off GitHub OAuth Device Flow for the Copilot client.

    Returns the response body which contains: device_code, user_code,
    verification_uri, expires_in, interval. The caller shows user_code +
    verification_uri to the user, then polls with poll_device_flow().
    """
    headers = {"Accept": "application/json", "User-Agent": COPILOT_CLIENT_HEADERS["User-Agent"]}
    data = {"client_id": COPILOT_OAUTH_CLIENT_ID, "scope": COPILOT_OAUTH_SCOPE}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(GITHUB_DEVICE_CODE_URL, headers=headers, data=data)
    if resp.status_code != 200:
        raise CopilotAuthError(
            f"GitHub device flow start returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
    body = resp.json()
    if "device_code" not in body or "user_code" not in body:
        raise CopilotAuthError(f"GitHub device flow start missing fields: {body}")
    return body


async def poll_device_flow(device_code: str, *, timeout: float = 10.0) -> dict:
    """Poll GitHub for a token after the user enters the user_code.

    Returns one of:
      {"status": "pending"}   — keep polling
      {"status": "slow_down"} — server asked us to back off
      {"status": "expired"}   — device_code expired; restart
      {"status": "denied"}    — user denied access
      {"status": "ok", "access_token": "gho_..."} — success
    """
    headers = {"Accept": "application/json", "User-Agent": COPILOT_CLIENT_HEADERS["User-Agent"]}
    data = {
        "client_id": COPILOT_OAUTH_CLIENT_ID,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(GITHUB_DEVICE_TOKEN_URL, headers=headers, data=data)
    if resp.status_code != 200:
        raise CopilotAuthError(
            f"GitHub device flow poll returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
    body = resp.json()
    err = body.get("error")
    if err == "authorization_pending":
        return {"status": "pending"}
    if err == "slow_down":
        return {"status": "slow_down"}
    if err == "expired_token":
        return {"status": "expired"}
    if err == "access_denied":
        return {"status": "denied"}
    if err:
        raise CopilotAuthError(f"GitHub device flow poll error: {err} ({body.get('error_description')})")
    token = body.get("access_token")
    if not token:
        raise CopilotAuthError(f"GitHub device flow poll missing access_token: {body}")
    return {"status": "ok", "access_token": token}


# {oauth_token: (session_token, expires_at_unix)}
_SESSION_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_LOCK = threading.Lock()
_REFRESH_LEEWAY_S = 60.0


def _exchange_sync(oauth_token: str, *, timeout: float = 10.0) -> tuple[str, float]:
    """Blocking exchange — used inside the httpx auth flow which is sync-context."""
    headers = {
        "Authorization": f"token {oauth_token}",
        "Accept": "application/json",
        **COPILOT_CLIENT_HEADERS,
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(COPILOT_TOKEN_URL, headers=headers)
    if resp.status_code != 200:
        raise CopilotAuthError(
            f"Copilot token exchange returned HTTP {resp.status_code}: "
            f"{resp.text[:200]} — confirm the GitHub token is valid and the "
            f"account has Copilot access (Pro / Business / Student Pack)."
        )
    data = resp.json()
    token = data.get("token")
    expires_at = float(data.get("expires_at") or 0)
    if not token:
        raise CopilotAuthError(f"Copilot token exchange missing 'token' field: {data}")
    return token, expires_at


def get_session_token(oauth_token: str) -> str:
    """Return a fresh Copilot session token for *oauth_token*, using the cache."""
    if not oauth_token:
        raise CopilotAuthError("no GitHub OAuth token configured")
    now = time.time()
    with _CACHE_LOCK:
        cached = _SESSION_CACHE.get(oauth_token)
        if cached and cached[1] - _REFRESH_LEEWAY_S > now:
            return cached[0]
    token, expires_at = _exchange_sync(oauth_token)
    with _CACHE_LOCK:
        _SESSION_CACHE[oauth_token] = (token, expires_at)
    logger.info("Copilot session token refreshed (expires in %.0fs)", expires_at - now)
    return token


def list_models(oauth_token: str, *, timeout: float = 10.0) -> list[dict]:
    """List models available to the Copilot account behind *oauth_token*."""
    session = get_session_token(oauth_token)
    headers = {
        "Authorization": f"Bearer {session}",
        **COPILOT_CLIENT_HEADERS,
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(f"{COPILOT_API_BASE}/models", headers=headers)
    if resp.status_code != 200:
        raise CopilotAuthError(
            f"Copilot /models returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
    body = resp.json()
    # The endpoint returns either a {"data": [...]} envelope or a bare list.
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return body["data"]
    if isinstance(body, list):
        return body
    return []


class CopilotAuth(httpx.Auth):
    """httpx auth that injects the Copilot session token + client headers and
    transparently re-exchanges on 401."""

    requires_response_body = False

    def __init__(self, oauth_token: str) -> None:
        self._oauth = oauth_token

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {get_session_token(self._oauth)}"
        for k, v in COPILOT_CLIENT_HEADERS.items():
            request.headers.setdefault(k, v)
        response = yield request
        if response.status_code != 401:
            return
        # Force-refresh and retry once.
        with _CACHE_LOCK:
            _SESSION_CACHE.pop(self._oauth, None)
        request.headers["Authorization"] = f"Bearer {get_session_token(self._oauth)}"
        yield request


def make_copilot_http_client(oauth_token: str) -> httpx.AsyncClient:
    """Return an `httpx.AsyncClient` preconfigured for the Copilot endpoint."""
    return httpx.AsyncClient(
        base_url=COPILOT_API_BASE,
        auth=CopilotAuth(oauth_token),
        headers=COPILOT_CLIENT_HEADERS,
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
    )
