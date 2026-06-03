"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backend.account_pool import (
    Lease,
    get_account_pool,
    parse_cooldown_seconds,
    pool_provider_for_spec,
)
from backend.agents.solver import Solver
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.message_bus import ChallengeMessageBus
from backend.models import DEFAULT_MODELS, provider_from_spec
from backend.prompts import ChallengeMeta
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    PARKED,
    QUOTA_ERROR,
    SolverProtocol,
    SolverResult,
)

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)


# Quota fallback: map subscription-backed providers to API-backed equivalents
QUOTA_FALLBACK: dict[str, str] = {
    "claude-sdk/claude-opus-4-7": "bedrock/us.anthropic.claude-opus-4-7-v1",
    "claude-sdk/claude-opus-4-6": "bedrock/us.anthropic.claude-opus-4-6-v1",
    "codex/gpt-5.4": "azure/gpt-5.4",
    "codex/gpt-5.4-mini": "azure/gpt-5.4-mini",
    "codex/gpt-5.3-codex-spark": "zen/gpt-5.3-codex-spark",
}


def _quota_fallback_spec(model_spec: str) -> str | None:
    return QUOTA_FALLBACK.get(model_spec)


@dataclass
class AgentControl:
    """Operator control surface for a single agent (one model on one challenge)."""

    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    restart: asyncio.Event = field(default_factory=asyncio.Event)
    paused: bool = False


@dataclass
class ChallengeSwarm:
    """Parallel solvers racing on one challenge."""

    challenge_dir: str
    meta: ChallengeMeta
    ctfd: CTFdClient
    cost_tracker: CostTracker
    settings: Settings
    model_specs: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    no_submit: bool = False
    coordinator_inbox: asyncio.Queue | None = None

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    solvers: dict[str, SolverProtocol] = field(default_factory=dict)
    findings: dict[str, str] = field(default_factory=dict)
    winner: SolverResult | None = None
    confirmed_flag: str | None = None
    _flag_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _submit_count: dict[str, int] = field(default_factory=dict)  # per-model wrong submission count
    _submitted_flags: set[str] = field(default_factory=set)  # dedup exact flags
    _last_submit_time: dict[str, float] = field(default_factory=dict)  # per-model last submit timestamp
    message_bus: ChallengeMessageBus = field(default_factory=ChallengeMessageBus)

    # Parking state: set when pooled solvers couldn't get an account. Holds the
    # earliest datetime an account frees up, so the coordinator can back off
    # before re-spawning this challenge instead of busy-looping.
    parked_until: object | None = None
    _parked_specs: set = field(default_factory=set)
    _ran_specs: set = field(default_factory=set)

    # Per-agent operator controls, keyed by model_spec.
    agent_controls: dict[str, AgentControl] = field(default_factory=dict)

    def _control(self, model_spec: str) -> AgentControl:
        ctrl = self.agent_controls.get(model_spec)
        if ctrl is None:
            ctrl = AgentControl()
            self.agent_controls[model_spec] = ctrl
        return ctrl

    def _cancel_all(self) -> None:
        """Stop the whole swarm (flag found / kill): swarm event + every agent."""
        self.cancel_event.set()
        for c in self.agent_controls.values():
            c.cancel.set()

    # ── per-agent operator control ───────────────────────────────────────────

    def message_agent(self, model_spec: str, text: str) -> bool:
        """Inject a free-text operator instruction into one agent's next turn."""
        solver = self.solvers.get(model_spec)
        if not solver:
            return False
        solver.bump(f"OPERATOR INSTRUCTION (follow this): {text}")
        return True

    def stop_agent(self, model_spec: str) -> bool:
        self._control(model_spec).cancel.set()
        return model_spec in self.solvers

    def pause_agent(self, model_spec: str) -> bool:
        self._control(model_spec).paused = True
        return model_spec in self.solvers

    def resume_agent(self, model_spec: str) -> bool:
        self._control(model_spec).paused = False
        return model_spec in self.solvers

    def restart_agent(self, model_spec: str) -> bool:
        ctrl = self._control(model_spec)
        ctrl.paused = False
        ctrl.restart.set()
        return model_spec in self.solvers

    async def add_context_file(self, model_spec: str, filename: str, data: bytes) -> bool:
        """Copy a file into this agent's sandbox (/challenge/workspace) and tell it."""
        solver = self.solvers.get(model_spec)
        sandbox = getattr(solver, "sandbox", None)
        if not solver or not sandbox:
            return False
        safe = os.path.basename(filename) or "context.bin"
        try:
            await sandbox.write_file(f"/challenge/workspace/{safe}", data)
        except Exception as e:
            logger.warning(f"[{self.meta.name}/{model_spec}] add_context_file failed: {e}")
            return False
        solver.bump(
            f"OPERATOR added a context file at /challenge/workspace/{safe} "
            f"({len(data)} bytes). Read/use it."
        )
        return True

    def _create_solver(self, model_spec: str, lease: Lease | None = None):
        """Create the right solver type based on provider.

        - claude-sdk/* → ClaudeSolver (Claude Agent SDK, subscription-first)
        - codex/* → CodexSolver (Codex App Server, subscription-first)
        - copilot/* → Pydantic AI Solver using the leased account's gho_ token
        - bedrock/*, azure/*, zen/*, google/* → Pydantic AI Solver (API key)

        For CLI providers the lease supplies an isolated config dir; for token
        providers (copilot) it supplies the credential injected into settings.
        """
        provider = provider_from_spec(model_spec)

        def _submit_fn(flag): return self.try_submit_flag(flag, model_spec)
        _notify = self._make_notify_fn(model_spec)

        # CLI providers consume the leased config dir; token providers don't.
        config_dir = lease.config_dir if (lease and provider in ("claude-sdk", "codex")) else None

        if provider == "claude-sdk":
            from backend.agents.claude_solver import ClaudeSolver
            return ClaudeSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                config_dir=config_dir,
            )

        if provider == "codex":
            from backend.agents.codex_solver import CodexSolver
            return CodexSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                config_dir=config_dir,
            )

        # Pydantic AI solver. For copilot, inject the leased token so the
        # solver authenticates with this pool account (not per-user settings).
        settings_override = None
        if provider == "copilot" and lease and lease.secret:
            try:
                settings_override = self.settings.model_copy(
                    update={"github_copilot_oauth_token": lease.secret}
                )
            except Exception:
                settings_override = self.settings
        return self._create_pydantic_solver(model_spec, settings=settings_override)

    def _make_notify_fn(self, model_spec: str):
        """Create a callback that pushes solver messages to the coordinator inbox."""
        async def _notify(message: str) -> None:
            if self.coordinator_inbox:
                self.coordinator_inbox.put_nowait(
                    f"[{self.meta.name}/{model_spec}] {message}"
                )
        return _notify

    def _create_pydantic_solver(self, model_spec: str, sandbox=None, owns_sandbox: bool | None = None, settings=None) -> Solver:
        """Create a Pydantic AI solver. Pass sandbox to reuse an existing container (quota fallback).

        `settings` overrides the swarm's settings (used to inject a leased
        copilot token for a specific pool account).
        """
        solver = Solver(
            model_spec=model_spec,
            challenge_dir=self.challenge_dir,
            meta=self.meta,
            ctfd=self.ctfd,
            cost_tracker=self.cost_tracker,
            settings=settings or self.settings,
            cancel_event=self.cancel_event,
            sandbox=sandbox,
            owns_sandbox=owns_sandbox,
        )
        solver.deps.message_bus = self.message_bus
        solver.deps.model_spec = model_spec
        solver.deps.no_submit = self.no_submit
        solver.deps.submit_fn = lambda flag: self.try_submit_flag(flag, model_spec)
        solver.deps.notify_coordinator = self._make_notify_fn(model_spec)
        return solver

    def _gather_sibling_insights(self, exclude_model: str) -> str:
        parts: list[str] = []
        for model, finding in self.findings.items():
            if model != exclude_model and finding:
                parts.append(f"[{model}]: {finding}")
        return "\n\n".join(parts) if parts else "No sibling insights available yet."

    # Escalating cooldowns after incorrect submissions (per model)
    SUBMISSION_COOLDOWNS = [0, 30, 120, 300, 600]  # 0s, 30s, 2min, 5min, 10min

    async def try_submit_flag(self, flag: str, model_spec: str) -> tuple[str, bool]:
        """Cooldown-gated, deduplicated flag submission. Returns (display, is_confirmed)."""
        async with self._flag_lock:
            if self.confirmed_flag:
                return f"ALREADY SOLVED — flag already confirmed: {self.confirmed_flag}", True

            normalized = flag.strip()

            # Dedup exact flags across all models
            if normalized in self._submitted_flags:
                return "INCORRECT — already tried this exact flag.", False

            # Escalating cooldown after incorrect submissions
            wrong_count = self._submit_count.get(model_spec, 0)
            cooldown_idx = min(wrong_count, len(self.SUBMISSION_COOLDOWNS) - 1)
            cooldown = self.SUBMISSION_COOLDOWNS[cooldown_idx]
            if cooldown > 0:
                last_time = self._last_submit_time.get(model_spec, 0)
                elapsed = time.monotonic() - last_time
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    return (
                        f"COOLDOWN — wait {remaining}s before submitting again. "
                        f"You have {wrong_count} incorrect submissions. "
                        "Use this time to do deeper analysis and verify your flag.",
                        False,
                    )

            self._submitted_flags.add(normalized)

            from backend.tools.core import do_submit_flag
            display, is_confirmed = await do_submit_flag(self.ctfd, self.meta.name, flag)
            if is_confirmed:
                self.confirmed_flag = normalized
            else:
                self._submit_count[model_spec] = wrong_count + 1
                self._last_submit_time[model_spec] = time.monotonic()
            return display, is_confirmed

    async def _run_solver(self, model_spec: str) -> SolverResult | None:
        pool_provider = pool_provider_for_spec(provider_from_spec(model_spec))
        pool = get_account_pool()
        use_pool = bool(pool_provider) and pool.has_accounts(pool_provider)

        lease: Lease | None = None
        if use_pool:
            lease = await pool.lease(pool_provider)
            if lease is None:
                # No account free right now — park this solver. The coordinator
                # backs off and retries the challenge once an account frees up.
                self._parked_specs.add(model_spec)
                logger.info(
                    f"[{self.meta.name}/{model_spec}] Parked — no '{pool_provider}' account available"
                )
                return SolverResult(
                    flag=None, status=PARKED, findings_summary="parked: no account available",
                    step_count=0, cost_usd=0.0, log_path="",
                )

        ctrl = self._control(model_spec)
        solver = self._create_solver(model_spec, lease=lease)
        solver.cancel_event = ctrl.cancel  # per-agent stop, independent of swarm-wide
        self.solvers[model_spec] = solver
        self._ran_specs.add(model_spec)

        try:
            result, final_solver, lease = await self._run_solver_loop(
                solver, model_spec, lease, pool_provider
            )
            solver = final_solver
            if result.status == PARKED:
                self._parked_specs.add(model_spec)
            return result
        except Exception as e:
            logger.error(f"[{self.meta.name}/{model_spec}] Fatal: {e}", exc_info=True)
            return None
        finally:
            if lease is not None:
                await get_account_pool().release(lease)
            await solver.stop()

    async def _run_solver_loop(
        self, solver, model_spec: str, lease: Lease | None = None, pool_provider: str | None = None
    ) -> tuple[SolverResult, SolverProtocol, Lease | None]:
        """Inner loop: start → run → bump → run → ...

        Returns the final result, the (possibly re-created) solver, and the
        currently-held pool lease so the caller can release it.
        """
        pool = get_account_pool()
        bump_count = 0
        consecutive_errors = 0
        result = SolverResult(
            flag=None, status=CANCELLED, findings_summary="",
            step_count=0, cost_usd=0.0, log_path="",
        )
        await solver.start()

        ctrl = self._control(model_spec)
        while not self.cancel_event.is_set() and not ctrl.cancel.is_set():
            # Operator pause: idle between turns until resumed / stopped.
            while ctrl.paused and not self.cancel_event.is_set() and not ctrl.cancel.is_set():
                await asyncio.sleep(0.5)
            if self.cancel_event.is_set() or ctrl.cancel.is_set():
                break

            # Operator restart: tear down and re-create this agent (fresh sandbox,
            # re-leased account) without disturbing siblings.
            if ctrl.restart.is_set():
                ctrl.restart.clear()
                await solver.stop()
                if lease is not None:
                    await pool.release(lease)
                    lease = await pool.lease(pool_provider) if pool_provider else None
                solver = self._create_solver(model_spec, lease=lease)
                solver.cancel_event = ctrl.cancel
                self.solvers[model_spec] = solver
                await solver.start()
                logger.info(f"[{self.meta.name}/{model_spec}] Restarted by operator")

            result = await solver.run_until_done_or_gave_up()

            # Only broadcast useful findings — skip errors and broken solvers
            if (result.status not in (ERROR, QUOTA_ERROR)
                    and not (result.step_count == 0 and result.cost_usd == 0)
                    and result.findings_summary
                    and not result.findings_summary.startswith(("Error:", "Turn failed:"))):
                self.findings[model_spec] = result.findings_summary
                await self.message_bus.post(model_spec, result.findings_summary[:500])

            if result.status == FLAG_FOUND:
                self._cancel_all()
                self.winner = result
                logger.info(
                    f"[{self.meta.name}] Flag found by {model_spec}: {result.flag}"
                )
                return result, solver, lease

            if result.status == CANCELLED:
                break

            # Quota exhaustion.
            if result.status == QUOTA_ERROR:
                # Pooled provider: put this account on cooldown and rotate to the
                # next available subscription account (subscriptions-only failover).
                if lease is not None and pool_provider:
                    cooldown = parse_cooldown_seconds(result.findings_summary)
                    await pool.mark_cooldown(lease, cooldown)
                    await solver.stop()  # fresh sandbox for the next account
                    next_lease = await pool.lease(pool_provider)
                    if next_lease is None:
                        # Every account of this provider is busy/cooling — park.
                        logger.warning(
                            f"[{self.meta.name}/{model_spec}] All '{pool_provider}' accounts "
                            "exhausted — parking challenge for retry"
                        )
                        result = SolverResult(
                            flag=None, status=PARKED,
                            findings_summary="parked: all accounts cooling down",
                            step_count=0, cost_usd=0.0, log_path="",
                        )
                        return result, solver, None
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Quota hit — rotating to account "
                        f"'{next_lease.label}'"
                    )
                    lease = next_lease
                    solver = self._create_solver(model_spec, lease=lease)
                    solver.cancel_event = ctrl.cancel
                    self.solvers[model_spec] = solver
                    await solver.start()
                    continue

                # No pool in use: legacy fallback to API-backed Pydantic AI solver.
                fallback_spec = _quota_fallback_spec(model_spec)
                if fallback_spec:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Quota exhausted — falling back to {fallback_spec}"
                    )
                    existing_sandbox = solver.sandbox
                    # Detach sandbox from old solver so stop() doesn't destroy it
                    solver.sandbox = None  # type: ignore[assignment]
                    await solver.stop()
                    solver = self._create_pydantic_solver(fallback_spec, sandbox=existing_sandbox, owns_sandbox=True)
                    self.solvers[model_spec] = solver
                    await solver.start()
                    continue
                # No fallback available, treat as error
                break

            if result.status in (GAVE_UP, ERROR):
                if result.step_count == 0 and result.cost_usd == 0:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Broken (0 steps, $0) — not bumping"
                    )
                    break

                # Track consecutive errors — stop after 3 in a row
                if result.status == ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        logger.warning(
                            f"[{self.meta.name}/{model_spec}] {consecutive_errors} consecutive errors — giving up"
                        )
                        break
                else:
                    consecutive_errors = 0

                bump_count += 1
                # Cooldown between bumps — check cancellation during wait
                try:
                    await asyncio.wait_for(
                        self.cancel_event.wait(),
                        timeout=min(bump_count * 30, 300),
                    )
                    break  # cancelled during cooldown
                except TimeoutError:
                    pass  # cooldown elapsed, proceed with bump
                insights = self._gather_sibling_insights(model_spec)
                solver.bump(insights)
                logger.info(
                    f"[{self.meta.name}/{model_spec}] Bumped ({bump_count}), resuming"
                )
                continue

        return result, solver, lease

    def _compute_parked_until(self) -> None:
        """If every pooled solver parked and none ran, record when to retry."""
        if self.winner is not None:
            self.parked_until = None
            return
        if not self._parked_specs or self._ran_specs:
            self.parked_until = None
            return
        pool = get_account_pool()
        times = []
        for spec in self._parked_specs:
            pp = pool_provider_for_spec(provider_from_spec(spec))
            if pp:
                t = pool.earliest_cooldown(pp)
                if t:
                    times.append(t)
        # If no cooldown time known (all leases simply busy), retry soon.
        if times:
            self.parked_until = min(times)
        else:
            import datetime as _dt
            self.parked_until = _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=30)

    async def run(self) -> SolverResult | None:
        """Run all solvers in parallel. Returns the winner's result or None."""
        tasks = [
            asyncio.create_task(self._run_solver(spec), name=f"solver-{spec}")
            for spec in self.model_specs
        ]

        try:
            while tasks:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                for task in done:
                    try:
                        result = task.result()
                    except Exception:
                        continue
                    if result and result.status == FLAG_FOUND:
                        self._cancel_all()
                        for p in pending:
                            p.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        return result

                tasks = list(pending)

            self._cancel_all()
            self._compute_parked_until()
            return self.winner
        except Exception as e:
            logger.error(f"[{self.meta.name}] Swarm error: {e}", exc_info=True)
            self._cancel_all()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return None

    def kill(self) -> None:
        """Cancel all agents for this challenge."""
        self._cancel_all()

    def _agent_status(self, spec: str) -> str:
        ctrl = self.agent_controls.get(spec)
        if ctrl and ctrl.cancel.is_set() and not self.cancel_event.is_set():
            return "stopped"
        if ctrl and ctrl.paused:
            return "paused"
        if self.winner and self.winner.flag:
            return "won"
        if spec in self.solvers and not self.cancel_event.is_set():
            return "running"
        return "finished"

    def get_status(self) -> dict:
        """Get per-agent progress and findings."""
        return {
            "challenge": self.meta.name,
            "cancelled": self.cancel_event.is_set(),
            "winner": self.winner.flag if self.winner else None,
            "agents": {
                spec: {
                    "findings": self.findings.get(spec, ""),
                    "status": self._agent_status(spec),
                    "paused": bool(spec in self.agent_controls and self.agent_controls[spec].paused),
                    "stopped": bool(spec in self.agent_controls and self.agent_controls[spec].cancel.is_set()),
                }
                for spec in self.model_specs
            },
        }
