"""Verify and enumerate models for OpenAI-compatible token providers.

Grok (xAI), Kimi (Moonshot) and Antigravity (Gemini) all expose an
OpenAI-compatible ``GET /models`` endpoint. Because provider model IDs change
often (and several current ones could not be verified from docs), the connect
flow calls :func:`verify_token_provider` to list the *real* models a pasted
subscription/API token can reach, instead of trusting a hardcoded catalog.
"""

from __future__ import annotations

import logging

import httpx

from backend.providers import OPENAI_COMPAT_PROVIDERS

logger = logging.getLogger(__name__)


async def verify_token_provider(provider: str, token: str) -> dict:
    """Check a token against a provider's ``/models`` endpoint.

    Returns ``{"ok": bool, "models": [ids], "error": str}``. Never raises.
    """
    p = OPENAI_COMPAT_PROVIDERS.get(provider)
    if p is None:
        return {"ok": False, "models": [], "error": f"'{provider}' is not a token provider"}
    if not token:
        return {"ok": False, "models": [], "error": "no token provided"}

    url = f"{p.openai_base_url.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        return {"ok": False, "models": [], "error": f"request failed: {e}"}

    if resp.status_code == 401 or resp.status_code == 403:
        return {"ok": False, "models": [], "error": f"token rejected (HTTP {resp.status_code})"}
    if resp.status_code >= 400:
        return {"ok": False, "models": [], "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "models": [], "error": "non-JSON response from /models"}

    # OpenAI shape: {"data": [{"id": ...}, ...]}. Some list bare arrays.
    items = data.get("data") if isinstance(data, dict) else data
    models: list[str] = []
    for it in items or []:
        if isinstance(it, dict) and it.get("id"):
            models.append(str(it["id"]))
        elif isinstance(it, str):
            models.append(it)
    models.sort()
    return {"ok": True, "models": models, "error": ""}
