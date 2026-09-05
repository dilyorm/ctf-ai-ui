"""What models each connected subscription can actually run, right now.

Hardcoded catalogs drift: the Antigravity entry listed `gemini-3-pro-preview`,
`gemini-3-flash-preview` and `gemini-2.5-pro`, none of which exist, while the
account actually offers Gemini 3.8/3.7/3.6 Flash, 3.1 Pro, Claude Opus/Sonnet
4.6 and GPT-OSS 120B. Providers whose CLI can list its own models are asked
instead, and the registry is used only as a fallback.

This also backs *auto* model selection: with no explicit choice, a run uses the
strongest model each connected subscription offers, so connecting an account is
all an operator has to do.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from backend.account_pool import get_account_pool
from backend.providers import PROVIDERS

logger = logging.getLogger(__name__)

_TTL = 900  # 15 min; a CLI's model list changes rarely
_cache: dict[str, tuple[float, list[str]]] = {}

# Preference order per provider, strongest first. Matching is by prefix, so a
# provider that renames a variant still ranks sensibly, and anything unmatched
# sorts after the listed entries rather than disappearing.
_PREFERENCE: dict[str, tuple[str, ...]] = {
    "claude": ("opus/max", "opus/high", "opus", "sonnet/high", "sonnet", "haiku"),
    "codex": ("gpt-6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6", "gpt-5.3"),
    "grok": ("grok-4.6", "grok-4.5", "grok-build", "grok-4.3"),
    "antigravity": (
        "gemini-3.1-pro-high", "claude-opus-4-6", "gemini-3.8-flash-high",
        "gemini-3.7-flash-high", "claude-sonnet-4-6", "gemini-3.6-flash-high",
        "gemini-3.1-pro", "gpt-oss",
    ),
    "kimi": ("kimi-k2-thinking-turbo", "kimi-k2-thinking"),
    "copilot": ("claude-opus-4", "gpt-5.3-codex", "gpt-5-codex", "gpt-5", "claude-sonnet-4"),
}

# The model-spec prefix each pool provider is addressed by.
_SPEC_PREFIX = {"claude": "claude-sdk", "codex": "codex", "grok": "grok",
                "antigravity": "antigravity", "kimi": "kimi", "copilot": "copilot"}


def _rank(provider: str, model_id: str) -> int:
    prefs = _PREFERENCE.get(provider, ())
    for i, p in enumerate(prefs):
        if p in model_id:
            return i
    return len(prefs)


def _static_models(provider: str) -> list[str]:
    p = PROVIDERS.get(provider)
    if p and p.models:
        return list(p.models)
    # claude/codex have no registry models — their specs live in backend.models.
    from backend.models import ALL_MODELS

    prefix = _SPEC_PREFIX.get(provider, provider) + "/"
    return [m["spec"][len(prefix):] for m in ALL_MODELS if m["spec"].startswith(prefix)]


async def _discover(provider: str) -> list[str]:
    """Ask the provider's own CLI, falling back to the static registry."""
    pool = get_account_pool()
    config_dir = pool.config_dir_for(provider)
    if not config_dir:
        return _static_models(provider)

    if provider == "antigravity":
        models, err = await _antigravity_models(config_dir)
        if models:
            return models
        logger.info("antigravity model discovery fell back to the registry: %s", err)
    elif provider == "grok":
        models = await _grok_models(config_dir)
        if models:
            return models
    return _static_models(provider)


async def _antigravity_models(config_dir: str) -> tuple[list[str], str]:
    """`agy models` for a signed-in account. Imported lazily: connect_manager is
    POSIX-only (it drives PTYs), and discovery must stay importable elsewhere."""
    try:
        from backend.connect_manager import agy_models
    except Exception as e:  # noqa: BLE001
        return [], f"agy unavailable: {e}"
    ok, models, err = await agy_models(config_dir, timeout=90)
    return (models if ok else []), err


async def _grok_models(config_dir: str) -> list[str]:
    """`grok models` prints 'Available models:' then '  * id (default)' rows."""
    import os

    env = {**os.environ, "GROK_HOME": config_dir, "NO_COLOR": "1"}
    env.pop("XAI_API_KEY", None)
    try:
        proc = await asyncio.create_subprocess_exec(
            "grok", "models",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env, start_new_session=True,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except Exception as e:  # noqa: BLE001 — a CLI hiccup must not break a run
        logger.debug("grok model discovery failed: %s", e)
        return []
    if proc.returncode != 0:
        return []
    models = []
    for line in out.decode("utf-8", "replace").splitlines():
        m = re.match(r"\s*[-*]\s+(\S+)", line)
        if m:
            models.append(m.group(1))
    return models


async def models_for(provider: str, *, refresh: bool = False) -> list[str]:
    """Live model ids for *provider*, ranked strongest first."""
    now = time.time()
    hit = _cache.get(provider)
    if hit and not refresh and now - hit[0] < _TTL:
        return hit[1]
    models = await _discover(provider)
    models = sorted(dict.fromkeys(models), key=lambda m: (_rank(provider, m), m))
    _cache[provider] = (now, models)
    return models


async def auto_model_specs(
    *, max_challenges: int = 1, coordinator_provider: str = ""
) -> list[str]:
    """Models for a run, chosen from the connected subscriptions.

    Every challenge runs *every* selected model, so the selection size is a
    divisor of parallelism: five subscriptions each contributing their best
    model means five seats per challenge, and one challenge at a time. Size the
    set to the seat budget instead — total free seats divided by the challenges
    the operator wants running — and spread it across providers, so two
    challenges land on different subscriptions rather than queueing on one.

    Connecting an account is then the only configuration a run needs; the two
    limits that matter (per-account Max, challenges in parallel) do the rest.
    """
    pool = get_account_pool()
    providers = [p for p in pool.connected_providers() if _SPEC_PREFIX.get(p)]
    if not providers:
        return []

    ranked: dict[str, list[str]] = {}
    for provider in providers:
        prefix = _SPEC_PREFIX[provider]
        ranked[provider] = [f"{prefix}/{m}" for m in await models_for(provider)]

    # The coordinator holds a seat on its own provider for the whole run, so it
    # is not available to solvers. Counting it produced a selection larger than
    # the pool could ever run, which the run plan then reported as "0 challenges
    # can run at once".
    def seats(provider: str) -> int:
        free = pool.free(provider)
        if provider == coordinator_provider:
            free -= 1
        return max(free, 0)

    total_seats = sum(seats(p) for p in providers)
    budget = max(1, total_seats // max(1, max_challenges))

    # Round-robin by strength: one model from each provider before a second
    # from any, so the set stays spread even when it is smaller than the
    # number of connected subscriptions.
    specs: list[str] = []
    for depth in range(max(len(v) for v in ranked.values())):
        for provider in providers:
            if len(specs) >= budget:
                return specs
            options = ranked[provider]
            if depth < len(options):
                specs.append(options[depth])
    return specs


async def catalog_by_provider(*, refresh: bool = False) -> dict[str, list[str]]:
    """Live models for every connected provider — for the UI and the coordinator."""
    pool = get_account_pool()
    out: dict[str, list[str]] = {}
    for provider in pool.connected_providers():
        prefix = _SPEC_PREFIX.get(provider)
        if not prefix:
            continue
        out[provider] = [f"{prefix}/{m}" for m in await models_for(provider, refresh=refresh)]
    return out
