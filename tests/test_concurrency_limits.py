"""Concurrency limits an operator sets must actually bind.

Two ways they didn't:

* A subscription with no pooled account bypassed the pool entirely, so nothing
  capped how many solvers ran on it at once — one Claude subscription could be
  driven by every solver of every swarm simultaneously.
* Changing the challenge concurrency during a run only updated a field that
  `status()` read; the running coordinator never saw it.
"""

from __future__ import annotations

import asyncio

import pytest

from backend import account_pool
from backend.account_pool import ambient_slot
from backend.run_manager import GlobalRunManager


@pytest.fixture(autouse=True)
def _fresh_slots():
    account_pool._ambient_slots.clear()
    account_pool._ambient_limits.clear()
    yield
    account_pool._ambient_slots.clear()
    account_pool._ambient_limits.clear()


@pytest.mark.parametrize("provider", ["claude", "codex", "grok", "antigravity"])
def test_subscription_providers_get_a_slot(provider):
    assert ambient_slot(provider) is not None


@pytest.mark.parametrize("provider", ["copilot", "kimi", "bedrock", "google", ""])
def test_metered_providers_are_not_capped(provider):
    """API-key billing is a cost decision, not a seat limit — leave it alone."""
    assert ambient_slot(provider) is None


def test_same_semaphore_is_shared_across_swarms():
    """Different challenges must contend for the one subscription, not get one each."""
    assert ambient_slot("claude") is ambient_slot("claude")
    assert ambient_slot("claude") is not ambient_slot("codex")


async def test_only_one_solver_runs_on_an_unpooled_subscription():
    slot = ambient_slot("claude")
    running = 0
    peak = 0

    async def solver():
        nonlocal running, peak
        async with slot:
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1

    await asyncio.gather(*(solver() for _ in range(6)))
    assert peak == 1, "six solvers ran on one subscription at once"


async def test_limit_is_configurable_and_rebuilt_when_changed():
    assert ambient_slot("claude", 3)._value == 3
    assert ambient_slot("claude", 1)._value == 1


def test_concurrency_change_reaches_a_running_coordinator():
    class Deps:
        max_concurrent_challenges = 10

    class _DoneTask:
        @staticmethod
        def done() -> bool:
            return False

    mgr = GlobalRunManager()
    deps = Deps()

    # No run yet: the CLI also builds deps, and must keep its own value.
    mgr.bind_deps(deps)
    assert deps.max_concurrent_challenges == 10
    assert mgr.set_max_concurrent(2)["applied_to_running_coordinator"] is False

    mgr._task = _DoneTask()
    mgr.bind_deps(deps)
    assert deps.max_concurrent_challenges == 2

    result = mgr.set_max_concurrent(5)
    assert result["applied_to_running_coordinator"] is True
    assert deps.max_concurrent_challenges == 5
    assert mgr.status()["max_concurrent"] == 5
