"""Shared coordinator tool logic — called by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from backend.deps import CoordinatorDeps
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND

logger = logging.getLogger(__name__)


async def do_fetch_challenges(deps: CoordinatorDeps) -> str:
    challenges = await deps.ctfd.fetch_all_challenges()
    solved = await deps.ctfd.fetch_solved_names()
    result = [
        {
            "name": ch.get("name", "?"),
            "category": ch.get("category", "?"),
            "value": ch.get("value", 0),
            "solves": ch.get("solves", 0),
            "status": "SOLVED" if ch.get("name") in solved else "unsolved",
            "description": (ch.get("description") or "")[:200],
        }
        for ch in challenges
    ]
    return json.dumps(result, indent=2)


async def do_get_solve_status(deps: CoordinatorDeps) -> str:
    solved = await deps.ctfd.fetch_solved_names()
    swarm_status = {name: swarm.get_status() for name, swarm in deps.swarms.items()}
    return json.dumps({"solved": sorted(solved), "active_swarms": swarm_status}, indent=2)


async def do_list_models(deps: CoordinatorDeps) -> str:
    """Live models per connected subscription, with free seats — for the LLM."""
    from backend.account_pool import get_account_pool
    from backend.model_discovery import catalog_by_provider

    pool = get_account_pool()
    try:
        catalog = await catalog_by_provider()
    except Exception as e:  # noqa: BLE001
        return f"Could not list models: {e}"
    if not catalog:
        return (
            "No subscription is connected, so only the run's default models are "
            "available: " + ", ".join(deps.model_specs)
        )
    lines = ["Models you can spawn, strongest first per subscription.",
             "Seats = solvers that provider can run at once right now.", ""]
    for provider, specs in catalog.items():
        lines.append(f"{provider} — {pool.free(provider)} seat(s) free")
        for spec in specs[:8]:
            lines.append(f"  {spec}")
    lines.append("")
    lines.append(f"This run's default selection: {', '.join(deps.model_specs)}")
    lines.append(
        "Pass model_specs to spawn_swarm to choose per challenge. Spending more "
        "seats on one challenge means fewer challenges run at once."
    )
    return "\n".join(lines)


async def do_spawn_swarm(
    deps: CoordinatorDeps, challenge_name: str, model_specs: list[str] | None = None
) -> str:
    if getattr(deps, "excluded_challenges", set()):
        # Support a regex exclusion marker used by the coordinator loop.
        for item in deps.excluded_challenges:
            if isinstance(item, str) and item.startswith("__regex__:"):
                return f"Challenge '{challenge_name}' excluded by regex; not spawning a swarm."
        if challenge_name in deps.excluded_challenges:
            return f"Challenge '{challenge_name}' is excluded; not spawning a swarm."

    # Retire ALL finished swarms before checking capacity
    finished = [
        name
        for name, swarm in deps.swarms.items()
        if swarm.cancel_event.is_set()
        or (name in deps.swarm_tasks and deps.swarm_tasks[name].done())
    ]
    for name in finished:
        del deps.swarms[name]
        deps.swarm_tasks.pop(name, None)

    active_count = len(deps.swarms)
    if active_count >= deps.max_concurrent_challenges:
        return f"At capacity ({active_count}/{deps.max_concurrent_challenges} challenges running). Wait for one to finish."

    if challenge_name in deps.swarms:
        return f"Swarm still running for {challenge_name}"

    # Auto-pull challenge if needed
    if challenge_name not in deps.challenge_dirs:
        challenges = await deps.ctfd.fetch_all_challenges()
        ch_data = next((c for c in challenges if c.get("name") == challenge_name), None)
        if not ch_data:
            return f"Challenge '{challenge_name}' not found on CTFd"
        output_dir = str(Path(deps.challenges_root))
        ch_dir = await deps.ctfd.pull_challenge(ch_data, output_dir)
        deps.challenge_dirs[challenge_name] = ch_dir
        deps.challenge_metas[challenge_name] = ChallengeMeta.from_yaml(
            Path(ch_dir) / "metadata.yml"
        )

    from backend.agents.swarm import ChallengeSwarm

    # The coordinator may pick models per challenge (harder challenge, stronger
    # model; a provider that is rate-limited, a different one). Anything it asks
    # for that isn't in the catalog is ignored rather than failing the spawn.
    chosen = [s for s in (model_specs or []) if s] or deps.model_specs
    swarm = ChallengeSwarm(
        challenge_dir=deps.challenge_dirs[challenge_name],
        meta=deps.challenge_metas[challenge_name],
        ctfd=deps.ctfd,
        cost_tracker=deps.cost_tracker,
        settings=deps.settings,
        model_specs=chosen,
        no_submit=deps.no_submit,
        coordinator_inbox=deps.coordinator_inbox,
    )
    deps.swarms[challenge_name] = swarm

    # Attach UI observer if available
    try:
        from ui.coordinator_bridge import SwarmObserver
        from ui.event_bus import get_bus

        SwarmObserver.observe(swarm, get_bus())
    except ImportError:
        pass

    async def _run_and_cleanup() -> None:
        result = await swarm.run()
        # Flag already submitted/confirmed by solver's submit_fn — just record the result
        if result and result.status == FLAG_FOUND:
            deps.results[challenge_name] = {
                "flag": result.flag,
                "submit": "DRY RUN" if deps.no_submit else "confirmed by solver",
            }
            deps.parked_until.pop(challenge_name, None)
        elif getattr(swarm, "parked_until", None) is not None:
            # No account was available — back off until one frees up so the
            # coordinator doesn't immediately re-spawn into the same wall.
            deps.parked_until[challenge_name] = swarm.parked_until.timestamp()
            logger.info(
                "Challenge '%s' parked until %s (no pool account available)",
                challenge_name,
                swarm.parked_until.isoformat(),
            )

    task = asyncio.create_task(_run_and_cleanup(), name=f"swarm-{challenge_name}")
    deps.swarm_tasks[challenge_name] = task
    return f"Swarm spawned for {challenge_name} with {len(chosen)} model(s): {', '.join(chosen)}"


async def do_check_swarm_status(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    return json.dumps(swarm.get_status(), indent=2)


async def do_submit_flag(deps: CoordinatorDeps, challenge_name: str, flag: str) -> str:
    if deps.no_submit:
        return f'DRY RUN — would submit "{flag.strip()}" for {challenge_name}'
    try:
        result = await deps.ctfd.submit_flag(challenge_name, flag)
        return result.display
    except Exception as e:
        return f"submit_flag error: {e}"


async def do_kill_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    swarm.kill()
    return f"Swarm for {challenge_name} cancelled"


async def do_bump_agent(
    deps: CoordinatorDeps, challenge_name: str, model_spec: str, insights: str
) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec} in {challenge_name}"
    solver.bump(insights)
    return f"Bumped {model_spec} on {challenge_name}"


async def do_read_solver_trace(
    deps: CoordinatorDeps, challenge_name: str, model_spec: str, last_n: int = 20
) -> str:
    """Read the last N trace events from a solver's JSONL log."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec}"
    trace_path = getattr(solver, "tracer", None)
    if not trace_path:
        return "No tracer on solver"
    path = trace_path.path if hasattr(trace_path, "path") else str(trace_path)
    try:
        lines = Path(path).read_text().strip().split("\n")
        recent = lines[-last_n:]
        summary = []
        for line in recent:
            try:
                d = json.loads(line)
                t = d.get("type", "?")
                if t == "tool_call":
                    args_str = str(d.get("args", ""))[:100]
                    summary.append(
                        f"step {d.get('step', '?')} CALL {d.get('tool', '?')}: {args_str}"
                    )
                elif t == "tool_result":
                    result_str = str(d.get("result", ""))[:100]
                    summary.append(
                        f"step {d.get('step', '?')} RESULT {d.get('tool', '?')}: {result_str}"
                    )
                elif t in ("finish", "error", "bump", "turn_failed"):
                    summary.append(
                        f"** {t}: {json.dumps({k: v for k, v in d.items() if k != 'ts'})}"
                    )
                elif t == "usage":
                    summary.append(
                        f"usage: in={d.get('input_tokens', 0)} out={d.get('output_tokens', 0)} cost=${d.get('cost_usd', 0):.4f}"
                    )
                else:
                    summary.append(f"{t}: {str(d)[:80]}")
            except Exception:
                summary.append(line[:100])
        return "\n".join(summary)
    except FileNotFoundError:
        return f"Trace file not found: {path}"
    except Exception as e:
        return f"Error reading trace: {e}"


async def do_broadcast(deps: CoordinatorDeps, challenge_name: str, message: str) -> str:
    """Broadcast a message to all solvers working on a challenge."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    await swarm.message_bus.broadcast(message)
    return f"Broadcast to all solvers on {challenge_name}"
