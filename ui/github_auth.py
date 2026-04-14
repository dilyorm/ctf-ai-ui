"""GitHub OAuth 2.0 helper for the CTF Agent web UI.

Flow:
  1. User clicks "Sign in with GitHub"
  2. Browser is redirected to /auth/github  → we redirect to GitHub's OAuth page
  3. GitHub calls back to /auth/github/callback with ?code=...&state=...
  4. We exchange the code for an access token and fetch the user profile
  5. Store the user info in the signed cookie session

Register a GitHub OAuth App at https://github.com/settings/developers:
  - Homepage URL:      http://localhost:8080
  - Callback URL:      http://localhost:8080/auth/github/callback
  - Scopes required:   read:user (user profile only)
"""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

# Requested scopes — read:user is enough to get name/avatar/login
GITHUB_SCOPES = "read:user"


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the GitHub OAuth authorization URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": GITHUB_SCOPES,
        "state": state,
        "allow_signup": "true",
    }
    return f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"


def generate_state() -> str:
    """Generate a CSRF-safe random state token."""
    return secrets.token_urlsafe(32)


async def exchange_code_for_token(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> str | None:
    """Exchange the OAuth code for an access token. Returns the token or None on failure."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=15.0,
        )
    if resp.status_code != 200:
        return None
    data = resp.json()
    return data.get("access_token")


async def fetch_github_user(access_token: str) -> dict[str, Any] | None:
    """Fetch the authenticated user profile from GitHub."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15.0,
        )
    if resp.status_code != 200:
        return None
    data = resp.json()
    return {
        "login": data.get("login", ""),
        "name": data.get("name") or data.get("login", ""),
        "avatar_url": data.get("avatar_url", ""),
        "html_url": data.get("html_url", ""),
        "id": data.get("id", 0),
        "email": data.get("email", ""),
        "access_token": access_token,
    }


def gravatar_url(email: str, size: int = 40) -> str:
    """Fallback avatar using Gravatar."""
    h = hashlib.md5(email.lower().encode()).hexdigest()
    return f"https://www.gravatar.com/avatar/{h}?s={size}&d=identicon"
