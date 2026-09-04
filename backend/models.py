"""Model resolution — Bedrock, Azure OpenAI, Zen, Google AI Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from pydantic_ai.models import Model
from pydantic_ai.models.bedrock import BedrockConverseModel, BedrockModelSettings
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIModel, OpenAIModelSettings
from pydantic_ai.providers.bedrock import BedrockProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from backend.providers import (
    OPENAI_COMPAT_PROVIDERS,
    catalog_entries,
    openai_compat_config,
)
from backend.providers import vision_specs as _provider_vision_specs

if TYPE_CHECKING:
    from backend.config import Settings

# Default model specs — single Opus 4.7 solver per challenge by default.
# Users can opt into multi-model swarms via the Settings → Models page
# (which writes UserModelPref rows that override DEFAULT_MODELS).
# Family aliases (opus/sonnet/haiku) always resolve to the current model via the
# Claude Code CLI, so we never hardcode a dated Claude id here.
DEFAULT_MODELS: list[str] = [
    "claude-sdk/opus/high",
]

# Full catalog of supported models for UI model-selection page.
ALL_MODELS: list[dict] = [
    # ── Claude subscription (Claude Code CLI auth; family aliases stay current) ──
    {"spec": "claude-sdk/opus/high",     "label": "Claude Opus · High effort",   "provider": "claude", "provider_label": "Claude Subscription"},
    {"spec": "claude-sdk/opus/max",      "label": "Claude Opus · Max effort",    "provider": "claude", "provider_label": "Claude Subscription"},
    {"spec": "claude-sdk/opus/medium",   "label": "Claude Opus · Medium effort", "provider": "claude", "provider_label": "Claude Subscription"},
    {"spec": "claude-sdk/sonnet/high",   "label": "Claude Sonnet · High effort", "provider": "claude", "provider_label": "Claude Subscription"},
    {"spec": "claude-sdk/sonnet/medium", "label": "Claude Sonnet · Medium",      "provider": "claude", "provider_label": "Claude Subscription"},
    {"spec": "claude-sdk/haiku/medium",  "label": "Claude Haiku · fast",         "provider": "claude", "provider_label": "Claude Subscription"},
    # ── Codex subscription (ChatGPT account via `codex login`) ──────────────────
    # Codex has no always-current alias; these are the current recommended ids.
    # Verify with "codex --model <id>" — the list drifts.
    {"spec": "codex/gpt-6-astra",        "label": "GPT-6 Astra (most capable)",  "provider": "codex", "provider_label": "OpenAI / Codex (Subscription)"},
    {"spec": "codex/gpt-5.6-sol",        "label": "GPT-5.6 Sol (deep coding)",   "provider": "codex", "provider_label": "OpenAI / Codex (Subscription)"},
    {"spec": "codex/gpt-5.6-terra",      "label": "GPT-5.6 Terra (balanced)",    "provider": "codex", "provider_label": "OpenAI / Codex (Subscription)"},
    {"spec": "codex/gpt-5.6-luna",       "label": "GPT-5.6 Luna (fast)",         "provider": "codex", "provider_label": "OpenAI / Codex (Subscription)"},
    {"spec": "codex/gpt-5.3-codex-spark","label": "GPT-5.3 Codex Spark",         "provider": "codex", "provider_label": "OpenAI / Codex (Subscription)"},
    # ── Google ─────────────────────────────────────────────────────────────────
    {"spec": "google/gemini-3-pro-preview", "label": "Gemini 3 Pro", "provider": "google", "provider_label": "Google AI (key)"},
    # ── Bedrock ────────────────────────────────────────────────────────────────
    {"spec": "bedrock/us.anthropic.claude-opus-4-7-v1", "label": "Claude Opus 4.7 (Bedrock)", "provider": "bedrock", "provider_label": "AWS Bedrock"},
    {"spec": "bedrock/us.anthropic.claude-opus-4-6-v1", "label": "Claude Opus 4.6 (Bedrock)", "provider": "bedrock", "provider_label": "AWS Bedrock"},
    # ── Azure OpenAI ───────────────────────────────────────────────────────────
    {"spec": "azure/claude-opus-4-7", "label": "Claude Opus 4.7 (Azure)", "provider": "azure", "provider_label": "Azure OpenAI"},
    {"spec": "azure/claude-opus-4-6", "label": "Claude Opus 4.6 (Azure)", "provider": "azure", "provider_label": "Azure OpenAI"},
    # ── OpenCode Zen ───────────────────────────────────────────────────────────
    {"spec": "zen/claude-opus-4-7", "label": "Claude Opus 4.7 (Zen/OpenCode)", "provider": "zen", "provider_label": "OpenCode Zen"},
    {"spec": "zen/claude-opus-4-6", "label": "Claude Opus 4.6 (Zen/OpenCode)", "provider": "zen", "provider_label": "OpenCode Zen"},
    # ── GitHub Copilot ─────────────────────────────────────────────────────────
    # The exact set offered to your account is shown by Settings → "Test
    # Copilot connection". The catalog below covers the common ones; adjust
    # the spec to match anything `/api/settings/copilot/models` returns.
    {"spec": "copilot/gpt-5.3-codex",   "label": "GPT-5.3 Codex (Copilot)",  "provider": "copilot", "provider_label": "GitHub Copilot"},
    {"spec": "copilot/gpt-5-codex",     "label": "GPT-5 Codex (Copilot)",    "provider": "copilot", "provider_label": "GitHub Copilot"},
    {"spec": "copilot/gpt-5",           "label": "GPT-5 (Copilot)",          "provider": "copilot", "provider_label": "GitHub Copilot"},
    {"spec": "copilot/gpt-5-mini",      "label": "GPT-5 Mini (Copilot)",     "provider": "copilot", "provider_label": "GitHub Copilot"},
    {"spec": "copilot/claude-opus-4",   "label": "Claude Opus 4 (Copilot)",  "provider": "copilot", "provider_label": "GitHub Copilot"},
    {"spec": "copilot/claude-sonnet-4", "label": "Claude Sonnet 4 (Copilot)","provider": "copilot", "provider_label": "GitHub Copilot"},
    {"spec": "copilot/o4-mini",         "label": "o4-mini (Copilot)",        "provider": "copilot", "provider_label": "GitHub Copilot"},
]

# ── Subscription/token providers (Grok, Kimi, Antigravity) ──────────────────
# Contributed by the provider registry. These are OpenAI-compatible; the exact
# model IDs a subscription exposes are best confirmed via the accounts page's
# "Verify & list models" (GET /v1/models), since provider model IDs change often.
ALL_MODELS.extend(catalog_entries())

# Context window sizes (tokens). Keyed by model id (aliases included).
CONTEXT_WINDOWS: dict[str, int] = {
    "opus": 1_000_000, "sonnet": 1_000_000, "haiku": 400_000, "fable": 1_000_000,
    "gpt-6-astra": 1_000_000, "gpt-5.6-sol": 1_000_000, "gpt-5.6-terra": 1_000_000,
    "gpt-5.6-luna": 400_000, "gpt-5.3-codex-spark": 128_000,
    "gemini-3-pro-preview": 1_000_000, "gemini-3-flash-preview": 1_000_000,
    "grok-4.6": 256_000, "grok-build-0.1": 256_000,
    "kimi-k2-thinking-turbo": 256_000,
}

# Models that support vision (keyed by model id / alias).
VISION_MODELS: set[str] = {
    "opus", "sonnet", "haiku", "fable",
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra",
    "gemini-3-pro-preview", "gemini-3-flash-preview",
}
# Vision-capable models contributed by token providers (grok/kimi/antigravity).
VISION_MODELS |= _provider_vision_specs()


def resolve_model(spec: str, settings: Settings) -> Model:
    """Resolve a 'provider/model_id' spec to a Pydantic AI Model."""
    provider = provider_from_spec(spec)
    model_id = model_id_from_spec(spec)
    match provider:
        case "bedrock":
            if settings.aws_bearer_token:
                return BedrockConverseModel(
                    model_id,
                    provider=BedrockProvider(
                        api_key=settings.aws_bearer_token,
                        region_name=settings.aws_region,
                    ),
                )
            else:
                session = boto3.Session()
                client = session.client("bedrock-runtime", region_name=settings.aws_region)
                return BedrockConverseModel(
                    model_id,
                    provider=BedrockProvider(bedrock_client=client),
                )
        case "azure":
            return OpenAIModel(
                model_id,
                provider=OpenAIProvider(
                    base_url=settings.azure_openai_endpoint,
                    api_key=settings.azure_openai_api_key,
                ),
            )
        case "zen":
            return OpenAIModel(
                model_id,
                provider=OpenAIProvider(
                    base_url="https://opencode.ai/zen/v1",
                    api_key=settings.opencode_zen_api_key,
                ),
            )
        case "copilot":
            from pydantic_ai.models.openai import OpenAIResponsesModel

            from backend.copilot_auth import (
                COPILOT_API_BASE,
                make_copilot_http_client,
            )

            oauth = getattr(settings, "github_copilot_oauth_token", "") or ""
            if not oauth:
                raise ValueError(
                    "copilot/* requires a GitHub OAuth token. "
                    "Save it in Settings → GitHub Copilot."
                )
            # Codex variants (gpt-5.3-codex, gpt-5-codex, ...) are NOT served
            # via Copilot's /chat/completions — only via the Responses API
            # at /responses. Chat models (gpt-5, claude-sonnet-4, ...) work
            # the other way around. Pick the right transport per model.
            is_codex = "codex" in model_id.lower()
            provider = OpenAIProvider(
                base_url=COPILOT_API_BASE,
                api_key="copilot-session",
                http_client=make_copilot_http_client(oauth),
            )
            if is_codex:
                return OpenAIResponsesModel(model_id, provider=provider)
            return OpenAIModel(model_id, provider=provider)
        case "google":
            return GoogleModel(
                model_id,
                provider=GoogleProvider(api_key=settings.gemini_api_key),
            )
        case p if p in OPENAI_COMPAT_PROVIDERS:
            # Grok (xAI), Kimi (Moonshot), Antigravity (Gemini) — OpenAI-compatible
            # REST endpoints driven by a pooled subscription/API token.
            cfg = openai_compat_config(p)
            assert cfg is not None
            base_url, token_field = cfg
            api_key = getattr(settings, token_field, "") or ""
            if not api_key:
                raise ValueError(
                    f"{p}/* requires a subscription/API token. "
                    f"Connect a {p} account on the Accounts page."
                )
            return OpenAIModel(
                model_id,
                provider=OpenAIProvider(base_url=base_url, api_key=api_key),
            )
        case "claude-sdk" | "codex":
            raise ValueError(
                f"Provider '{provider}' uses its own solver backend, not Pydantic AI. "
                f"resolve_model() should not be called for {spec}."
            )
        case _:
            raise ValueError(f"Unknown provider: {provider}")


def resolve_model_settings(spec: str) -> ModelSettings:
    """Get provider-specific model settings with caching enabled."""
    provider = spec.split("/", 1)[0]
    match provider:
        case "bedrock":
            return BedrockModelSettings(
                max_tokens=128_000,
                bedrock_cache_instructions=True,
                bedrock_cache_tool_definitions=True,
                bedrock_cache_messages=True,
            )
        case "azure" | "zen" | "copilot" | "grok" | "kimi" | "antigravity":
            # OpenAI-compatible providers — server-side prompt caching is
            # automatic, no explicit config needed. Set max_tokens to avoid
            # reserving the full context window. Copilot Codex models go
            # through the Responses API, which has its own settings class.
            from pydantic_ai.models.openai import OpenAIResponsesModelSettings

            if provider == "copilot" and "codex" in spec.lower():
                return OpenAIResponsesModelSettings(max_tokens=128_000)
            return OpenAIModelSettings(
                max_tokens=128_000,
            )
        case "google":
            return GoogleModelSettings(
                max_tokens=64_000,
                google_thinking_config={
                    "thinking_level": "high",
                    "include_thoughts": True,
                },
            )
        case _:
            return ModelSettings(max_tokens=128_000)


def model_id_from_spec(spec: str) -> str:
    """Extract just the model ID from a spec (strips effort suffix)."""
    parts = spec.split("/")
    return parts[1] if len(parts) >= 2 else spec


def provider_from_spec(spec: str) -> str:
    """Extract the provider from a spec."""
    return spec.split("/", 1)[0]


def effort_from_spec(spec: str) -> str | None:
    """Extract effort level from a spec like 'claude-sdk/claude-opus-4-6/max'."""
    parts = spec.split("/")
    if len(parts) >= 3 and parts[2] in ("low", "medium", "high", "max"):
        return parts[2]
    return None


def supports_vision(spec: str) -> bool:
    """Check if a model spec supports vision."""
    return model_id_from_spec(spec) in VISION_MODELS


def context_window(spec: str) -> int:
    """Get context window size for a model spec."""
    return CONTEXT_WINDOWS.get(model_id_from_spec(spec), 200_000)
