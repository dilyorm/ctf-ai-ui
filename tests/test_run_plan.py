"""A run must say up front what it can actually do.

The case this was written for: five subscriptions connected, one stale model
selected. The run solved one challenge at a time on one provider and nothing
explained why — the model list, the pool and the concurrency number are three
knobs that multiply, and none of them was reported together.
"""

from __future__ import annotations

import pytest

from backend import run_plan as run_plan_mod
from backend.run_plan import build_run_plan


class FakePool:
    def __init__(self, accounts: dict[str, tuple[int, int]]) -> None:
        # provider -> (account count, free seats)
        self._accounts = accounts

    def has_accounts(self, provider: str) -> bool:
        return provider in self._accounts

    def free(self, provider: str) -> int:
        return self._accounts.get(provider, (0, 0))[1]

    def snapshot(self) -> list[dict]:
        return [
            {"provider": p} for p, (n, _) in self._accounts.items() for _ in range(n)
        ]


@pytest.fixture
def pool(monkeypatch):
    def _install(accounts):
        fake = FakePool(accounts)
        monkeypatch.setattr(run_plan_mod, "get_account_pool", lambda: fake)
        return fake

    return _install


def test_stale_model_is_dropped_and_reported(pool):
    """`codex/gpt-5.3-codex` left the catalog; it only exists under copilot now."""
    pool({"codex": (1, 1)})
    plan = build_run_plan(["codex/gpt-5.3-codex"], coordinator_backend="claude")

    assert plan.model_specs == []
    assert plan.dropped_specs == ["codex/gpt-5.3-codex"]
    assert any("no longer exist" in w for w in plan.warnings)
    assert any("No usable model" in w for w in plan.warnings)


def test_connected_but_unused_subscriptions_are_named(pool):
    pool({"claude": (1, 2), "codex": (1, 1), "grok": (1, 1), "antigravity": (1, 1)})
    plan = build_run_plan(["codex/gpt-6-astra"], coordinator_backend="claude")

    assert set(plan.idle_subscriptions) == {"antigravity", "grok"}
    assert any("Connected but unused" in w for w in plan.warnings)


def test_reports_when_capacity_caps_challenges_below_the_request(pool):
    """One codex account at Max=1 means one challenge at a time, whatever conc says."""
    pool({"codex": (1, 1)})
    plan = build_run_plan(
        ["codex/gpt-6-astra"], coordinator_backend="claude", max_concurrent_challenges=2
    )

    assert plan.solver_capacity == {"codex": 1}
    assert plan.max_parallel_solvers == 1
    assert any("Only 1 challenge(s) can run at once, not 2" in w for w in plan.warnings)


def test_coordinator_seat_is_subtracted_from_its_own_provider(pool):
    """The coordinator holds an account for the whole run; solvers get the rest."""
    pool({"claude": (1, 2)})
    plan = build_run_plan(
        ["claude-sdk/opus/high"], coordinator_backend="claude", max_concurrent_challenges=2
    )

    assert plan.solver_capacity == {"claude": 1}
    assert any("Only 1 challenge(s)" in w for w in plan.warnings)


def test_enabling_models_across_providers_unlocks_parallelism(pool):
    """The actual remedy: spread the models over the connected subscriptions."""
    pool({"claude": (1, 2), "codex": (1, 1), "grok": (1, 1)})
    plan = build_run_plan(
        ["codex/gpt-6-astra", "grok/grok-4.6"],
        coordinator_backend="claude",
        max_concurrent_challenges=1,
    )

    assert plan.solver_capacity == {"codex": 1, "grok": 1}
    assert plan.idle_subscriptions == []
    assert not any("can run at once" in w for w in plan.warnings)


def test_missing_account_falls_back_to_the_servers_own_login(pool):
    pool({})
    plan = build_run_plan(
        ["claude-sdk/opus/high"], coordinator_backend="claude", ambient_concurrency=1
    )

    assert plan.solver_capacity == {"claude": 1}
    assert any("server's own login" in w for w in plan.warnings)


def test_api_key_models_are_not_seat_limited(pool):
    pool({})
    plan = build_run_plan(
        ["bedrock/us.anthropic.claude-opus-4-7-v1"],
        coordinator_backend="claude",
        max_concurrent_challenges=5,
    )

    assert plan.solver_capacity == {}
    assert plan.max_parallel_solvers == 5
    assert not any("can run at once" in w for w in plan.warnings)
