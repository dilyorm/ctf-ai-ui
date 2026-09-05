"""What a run will actually do, worked out before it starts.

Model selection, the account pool and the challenge-concurrency number are three
separate knobs that multiply, and nothing ever reported the product. An operator
could connect five subscriptions, leave one stale model enabled, and watch the
run solve one challenge at a time on one provider with no indication why.

`build_run_plan` answers the questions that were unanswerable from the UI:
which models will run, which subscription each needs, how many solvers can
actually hold an account at once, and which connected subscriptions nothing will
touch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.account_pool import get_account_pool, pool_provider_for_spec
from backend.models import ALL_MODELS, provider_from_spec

# Coordinator backend → the pool provider it signs in with.
_COORDINATOR_PROVIDER = {"claude": "claude", "codex": "codex"}


@dataclass
class RunPlan:
    model_specs: list[str] = field(default_factory=list)
    dropped_specs: list[str] = field(default_factory=list)
    coordinator_provider: str = ""
    # pool provider → number of solvers that can run on it at once
    solver_capacity: dict[str, int] = field(default_factory=dict)
    idle_subscriptions: list[str] = field(default_factory=list)
    max_parallel_solvers: int = 0
    requested_challenges: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "model_specs": self.model_specs,
            "dropped_specs": self.dropped_specs,
            "coordinator_provider": self.coordinator_provider,
            "solver_capacity": self.solver_capacity,
            "idle_subscriptions": self.idle_subscriptions,
            "max_parallel_solvers": self.max_parallel_solvers,
            "requested_challenges": self.requested_challenges,
            "warnings": self.warnings,
        }


def build_run_plan(
    model_specs: list[str],
    *,
    coordinator_backend: str = "claude",
    max_concurrent_challenges: int = 10,
    ambient_concurrency: int = 1,
) -> RunPlan:
    """Work out what a run can actually do, and what the operator should know."""
    pool = get_account_pool()
    known = {m["spec"] for m in ALL_MODELS}

    valid = [s for s in model_specs if s in known]
    dropped = [s for s in model_specs if s not in known]

    plan = RunPlan(
        model_specs=valid,
        dropped_specs=dropped,
        coordinator_provider=_COORDINATOR_PROVIDER.get(coordinator_backend, ""),
        requested_challenges=max_concurrent_challenges,
    )

    if dropped:
        plan.warnings.append(
            f"{len(dropped)} selected model(s) no longer exist and were skipped: "
            f"{', '.join(dropped)}. Pick current ones on Settings → Models."
        )
    if not valid:
        plan.warnings.append(
            "No usable model is selected, so the run has nothing to solve with. "
            "Enable at least one on Settings → Models."
        )
        return plan

    # How many solvers can hold an account at once, per provider.
    used_providers: set[str] = set()
    for spec in valid:
        provider = pool_provider_for_spec(provider_from_spec(spec))
        if not provider:
            # API-key backed (bedrock/azure/zen/google): not seat-limited.
            plan.max_parallel_solvers += max_concurrent_challenges
            continue
        used_providers.add(provider)

    for provider in sorted(used_providers):
        if pool.has_accounts(provider):
            capacity = pool.free(provider)
        else:
            # No pooled account: the server's own login, one seat.
            capacity = ambient_concurrency
            plan.warnings.append(
                f"No {provider} account is connected, so its solvers share the "
                f"server's own login ({capacity} at a time). Connect one on Accounts."
            )
        # The coordinator holds a seat on its provider for the whole run.
        if provider == plan.coordinator_provider and pool.has_accounts(provider):
            capacity = max(0, capacity - 1)
        plan.solver_capacity[provider] = capacity
        plan.max_parallel_solvers += capacity

    # Subscriptions that are connected but that no selected model can reach.
    for provider in sorted({a["provider"] for a in pool.snapshot()}):
        if provider not in used_providers and provider != plan.coordinator_provider:
            plan.idle_subscriptions.append(provider)
    if plan.idle_subscriptions:
        plan.warnings.append(
            f"Connected but unused: {', '.join(plan.idle_subscriptions)}. "
            f"No selected model runs on {'them' if len(plan.idle_subscriptions) > 1 else 'it'} "
            f"— enable one of their models on Settings → Models to use them in parallel."
        )

    # Each challenge runs one solver per model, so this is the real ceiling.
    reachable = plan.max_parallel_solvers // max(1, len(valid))
    if reachable < max_concurrent_challenges:
        plan.warnings.append(
            f"Only {reachable} challenge(s) can run at once, not {max_concurrent_challenges}: "
            f"each challenge needs {len(valid)} solver seat(s) and "
            f"{plan.max_parallel_solvers} are available. Raise 'Max' on an account, "
            f"connect another subscription, or enable models on other providers."
        )
    return plan
