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
    # into, and the OpenAI-compatible endpoint used to drive it. A provider may
    # declare both a CLI sign-in and these, in which case each *account* picks a
    # mode: a stored secret means token, an on-disk config dir means CLI.
    token_field: str = ""
    openai_base_url: str = ""
    # For CLI providers: the binary that has to exist on the server.
    cli_binary: str = ""
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
        id="claude", label="Claude (Anthropic)", connect_kind="cli", cli_binary="claude",
        token_help="Sign in to your Claude subscription via the Claude Code CLI.",
    ),
    "codex": Provider(
        id="codex", label="Codex (ChatGPT)", connect_kind="cli", cli_binary="codex",
        token_help="Sign in to your ChatGPT/Codex subscription via the Codex CLI.",
    ),
    "copilot": Provider(
        id="copilot", label="GitHub Copilot", connect_kind="device",
        token_help="Authorize with GitHub; needs an active Copilot subscription.",
        token_url="https://github.com/login/device",
    ),
    "grok": Provider(
        id="grok", label="Grok (xAI)", connect_kind="cli", cli_binary="grok",
        token_help="Sign in to your Grok/SuperGrok subscription via the xAI CLI (device login).",
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
    # Dual-mode: sign in with a Google account through the `agy` CLI (the real
    # subscription), or paste a Gemini API key. `agy` reads HOME/XDG for all of
    # its state, so pointing those at a per-account directory keeps several
    # Google accounts isolated and poolable.
    "antigravity": Provider(
        id="antigravity", label="Google Antigravity (Gemini)", connect_kind="cli",
        cli_binary="agy",
        token_field="antigravity_api_key",
        openai_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        token_help="Sign in with the Google account that has Antigravity access, or paste a Gemini API key from aistudio.google.com.",
        token_url="https://aistudio.google.com/app/apikey",
        models=("gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-2.5-pro"),
        vision_models=("gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-2.5-pro"),
    ),
}

# Providers whose credential can *only* live in secret_enc (no config dir).
TOKEN_POOL_PROVIDERS: set[str] = {
    p.id for p in PROVIDERS.values() if p.connect_kind in ("device", "token")
}
# Providers reachable over an OpenAI-compatible endpoint with a pasted token.
# Keyed off the endpoint, not connect_kind, so a dual-mode provider
# (antigravity: `agy` sign-in *or* a Gemini key) keeps its token path.
OPENAI_COMPAT_PROVIDERS: dict[str, Provider] = {
    p.id: p for p in PROVIDERS.values() if p.openai_base_url
}
# Providers that can be connected by driving a CLI on the server.
CLI_PROVIDERS: dict[str, Provider] = {
    p.id: p for p in PROVIDERS.values() if p.connect_kind == "cli"
}


def cli_binary_for(pid: str) -> str:
    p = PROVIDERS.get(pid)
    return p.cli_binary if p else ""


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
    """Model-catalog rows contributed by every provider that declares models.

    Covers token providers (kimi/antigravity) *and* CLI ones (grok), so a
    provider's model list lives in exactly one place. It used to iterate only
    ``OPENAI_COMPAT_PROVIDERS``, which silently dropped Grok's models the moment
    Grok became a CLI provider and forced a duplicate hardcoded list in
    ``backend.models``.
    """
    rows: list[dict] = []
    for p in PROVIDERS.values():
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
    for p in PROVIDERS.values():
        for m in p.vision_models:
            out.add(m)
    return out
