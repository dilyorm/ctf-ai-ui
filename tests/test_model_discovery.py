"""Models come from the connected subscriptions, not a hardcoded guess.

The Antigravity entry listed `gemini-3-pro-preview`, `gemini-3-flash-preview`
and `gemini-2.5-pro`; asking the signed-in account returns Gemini 3.8/3.7/3.6
Flash, 3.1 Pro, Claude Opus/Sonnet 4.6 and GPT-OSS 120B. None of the three
hardcoded ids exist.
"""

from __future__ import annotations

import pytest

from backend import model_discovery
from backend.model_discovery import auto_model_specs, models_for

# Verbatim `agy models` output from a signed-in account.
AGY_OUTPUT = """Fetching available models...
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)
gemini-3.1-pro-high\tGemini 3.1 Pro (High)
claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""

GROK_OUTPUT = """You are logged in with grok.com.

Default model: grok-4.6

Available models:
  * grok-4.6 (default)
  - grok-4.5
"""


class FakePool:
    def __init__(self, providers):
        self._providers = providers

    def connected_providers(self):
        return sorted(self._providers)

    def config_dir_for(self, provider):
        return f"/cfg/{provider}" if provider in self._providers else ""

    def free(self, provider):
        return 1


@pytest.fixture(autouse=True)
def _clear_cache():
    model_discovery._cache.clear()
    yield
    model_discovery._cache.clear()


@pytest.fixture
def env(monkeypatch):
    def _install(providers, *, agy=AGY_OUTPUT, grok=GROK_OUTPUT):
        monkeypatch.setattr(model_discovery, "get_account_pool", lambda: FakePool(providers))

        async def fake_agy(config_dir):
            return [ln.split("\t")[0] for ln in agy.splitlines() if "\t" in ln], ""

        async def fake_grok(config_dir):
            import re

            return [m.group(1) for m in re.finditer(r"^\s*[-*]\s+(\S+)", grok, re.M)]

        monkeypatch.setattr(model_discovery, "_antigravity_models", fake_agy)
        monkeypatch.setattr(model_discovery, "_grok_models", fake_grok)

    return _install


async def test_antigravity_models_come_from_the_account(env):
    env({"antigravity"})
    models = await models_for("antigravity")

    assert "gemini-3.8-flash-high" in models
    assert "claude-opus-4-6-thinking" in models
    # The stale guesses must not reappear.
    assert "gemini-3-pro-preview" not in models
    assert "gemini-2.5-pro" not in models


async def test_strongest_model_is_ranked_first(env):
    env({"antigravity"})
    assert (await models_for("antigravity"))[0] == "gemini-3.1-pro-high"

    env({"grok"})
    assert (await models_for("grok"))[0] == "grok-4.6"


async def test_auto_selects_one_model_per_connected_subscription(env):
    env({"antigravity", "grok", "claude", "codex"})
    specs = await auto_model_specs()

    providers = sorted(s.split("/")[0] for s in specs)
    assert providers == ["antigravity", "claude-sdk", "codex", "grok"]
    assert "antigravity/gemini-3.1-pro-high" in specs
    assert "grok/grok-4.6" in specs


async def test_auto_is_empty_with_nothing_connected(env):
    env(set())
    assert await auto_model_specs() == []


async def test_results_are_cached(env, monkeypatch):
    env({"grok"})
    calls = []

    async def counting(config_dir):
        calls.append(config_dir)
        return ["grok-4.6"]

    monkeypatch.setattr(model_discovery, "_grok_models", counting)
    await models_for("grok")
    await models_for("grok")
    assert len(calls) == 1, "the CLI should be asked once per TTL"
