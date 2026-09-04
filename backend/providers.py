"""Provider registry — the single source of truth for connectable AI backends.

A *provider* is one way to run a solver model: a subscription CLI (Claude Code,
Codex), a device-flow token (GitHub Copilot), or an OpenAI-compatible API token
(Grok, Kimi, Antigravity/Gemini). Each entry declares how it's connected, how
it's pooled, and (for token providers) which OpenAI-compatible endpoint drives
it, so the rest of the codebase (account pool, solver factory, models catalog,
accounts UI) can treat providers uniformly instead of special-casing each.

Connect kinds
-------------
- ``cli``    : an isolated CLI config dir holds OAuth creds (`claude setup-token`,
               `codex login`). Authenticated == credentials on disk.
- ``device`` : a device/OAuth flow yields a long-lived token stored (encrypted)
               in the pool row's ``secret_enc`` (GitHub Copilot).
- ``token``  : the operator pastes a subscription/API token; stored encrypted in
               ``secret_enc`` and injected into the solver at run time. Driven by
               the Pydantic-AI OpenAI-compatible ``Solver``.

Any user may connect any number of accounts for any provider; the shared pool
rotates across them and cools an account on a rate-limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    id: str  # pool provider id and model-spec prefix
    label: str
    connect_kind: str  # "cli" | "device" | "token"
    # For token providers: the Settings field the leased credential is injected
    # into, and the OpenAI-compatible endpoint used to drive it.
    token_field: str = ""
    openai_base_url: str = ""
    # Where an operator gets the token (shown in the connect UI).
    token_help: str = ""
    token_url: str = ""
    # Default model ids offered in the catalog (spec = "{id}/{model}").
    models: tuple[str, ...] = field(default_factory=tuple)
    vision_models: tuple[str, ...] = field(default_factory=tuple)


# NOTE: endpoints/model-ids for grok/kimi/antigravity are the documented
# OpenAI-compatible surfaces; adjust here (one place) if a provider moves.
PROVIDERS: dict[str, Provider] = {
    "claude": Provider(
        id="claude", label="Claude (Anthropic)", connect_kind="cli",
        token_help="Sign in to your Claude subscription via the Claude Code CLI.",
    ),
    "codex": Provider(
        id="codex", label="Codex (ChatGPT)", connect_kind="cli",
        token_help="Sign in to your ChatGPT/Codex subscription via the Codex CLI.",
    ),
    "copilot": Provider(
        id="copilot", label="GitHub Copilot", connect_kind="device",
        token_help="Authorize with GitHub; needs an active Copilot subscription.",
        token_url="https://github.com/login/device",
    ),
    "grok": Provider(
        id="grok", label="Grok (xAI)", connect_kind="token",
        token_field="grok_api_key",
        openai_base_url="https://api.x.ai/v1",
        token_help="Paste an xAI API key (SuperGrok / xAI console → API keys).",
        token_url="https://console.x.ai",
        models=("grok-4.6", "grok-build-0.1", "grok-4.5", "grok-4.3"),
        vision_models=("grok-4.6", "grok-4.5"),
    ),
    "kimi": Provider(
        id="kimi", label="Kimi (Moonshot)", connect_kind="token",
        token_field="kimi_api_key",
        openai_base_url="https://api.moonshot.ai/v1",
        token_help="Paste a Kimi Code / Moonshot API key (platform.kimi.ai → API keys). The Kimi Code subscription covers this key.",
        token_url="https://platform.kimi.ai/console/api-keys",
        models=("kimi-k2-thinking-turbo", "kimi-k2-thinking"),
        vision_models=(),
    ),
    "antigravity": Provider(
        id="antigravity", label="Google Antigravity (Gemini)", connect_kind="token",
        token_field="antigravity_api_key",
        openai_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        token_help="Paste a Google AI (Gemini) API key from aistudio.google.com. Antigravity's own CLI stores creds in the OS keyring and can't be pooled headlessly, so the Gemini key is the portable path.",
        token_url="https://aistudio.google.com/app/apikey",
        models=("gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-2.5-pro"),
        vision_models=("gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-2.5-pro"),
    ),
}

# Providers whose credential lives in secret_enc (not a config dir).
TOKEN_POOL_PROVIDERS: set[str] = {
    p.id for p in PROVIDERS.values() if p.connect_kind in ("device", "token")
}
# Providers driven by the Pydantic-AI OpenAI-compatible Solver via a pasted token.
OPENAI_COMPAT_PROVIDERS: dict[str, Provider] = {
    p.id: p for p in PROVIDERS.values() if p.connect_kind == "token"
}


def provider(pid: str) -> Provider | None:
    return PROVIDERS.get(pid)


def token_field_for(pid: str) -> str:
    p = PROVIDERS.get(pid)
    return p.token_field if p else ""


def openai_compat_config(pid: str) -> tuple[str, str] | None:
    """(base_url, token_field) for an OpenAI-compatible token provider, else None."""
    p = OPENAI_COMPAT_PROVIDERS.get(pid)
    return (p.openai_base_url, p.token_field) if p else None


def catalog_entries() -> list[dict]:
    """Model-catalog rows contributed by the token providers (grok/kimi/antigravity)."""
    rows: list[dict] = []
    for p in OPENAI_COMPAT_PROVIDERS.values():
        for m in p.models:
            rows.append({
                "spec": f"{p.id}/{m}",
                "label": f"{m} ({p.label.split(' ')[0]})",
                "provider": p.id,
                "provider_label": p.label,
            })
    return rows


def vision_specs() -> set[str]:
    out: set[str] = set()
    for p in OPENAI_COMPAT_PROVIDERS.values():
        for m in p.vision_models:
            out.add(m)
    return out
