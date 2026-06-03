"""Bridge between the coordinator backend and the UI event bus.

Patches the coordinator_loop and swarm to emit events to the UI event bus
so the dashboard gets real-time updates.

Usage (from cli.py or coordinator code):
    from ui.coordinator_bridge import install_bridge
    install_bridge(deps, cost_tracker)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any

logger = logging.getLogger(__name__)

# Global operator inbox — set when coordinator starts
_operator_inbox: asyncio.Queue | None = None


def get_operator_inbox() -> asyncio.Queue | None:
    return _operator_inbox


def set_operator_inbox(inbox: asyncio.Queue) -> None:
    global _operator_inbox
    _operator_inbox = inbox


class UILogHandler(logging.Handler):
    """Logging handler that emits log lines to the UI event bus."""

    def __init__(self, challenge: str = "", model: str = "") -> None:
        super().__init__()
        self.challenge = challenge
        self.model = model

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from ui.event_bus import get_bus

            bus = get_bus()
            bus.emit_sync(
                "log_line",
                {
                    "challenge": self.challenge or _extract_challenge(record.getMessage()),
                    "model": self.model,
                    "text": self.format(record),
                    "level": record.levelname.lower(),
                },
            )
        except Exception:
            pass


def _extract_challenge(msg: str) -> str:
    """Try to extract challenge name from log message like '[challenge/model] ...'."""
    if msg.startswith("["):
        end = msg.find("]")
        if end > 0:
            inner = msg[1:end]
            parts = inner.split("/")
            if parts:
                return parts[0]
    return "coordinator"


def install_bridge(deps: Any, cost_tracker: Any) -> None:
    """
    Monkey-patch the coordinator deps to emit UI events.

    Call this after creating deps but before running the event loop.
    """
    from ui.event_bus import get_bus

    bus = get_bus()

    # Store the operator inbox so the UI can send messages
    set_operator_inbox(deps.operator_inbox)

    # Update CTFd status
    bus.emit_sync(
        "ctfd_status",
        {
            "connected": True,
            "url": deps.ctfd.base_url,
            "user": deps.ctfd.username or "token",
        },
    )

    # Wrap CTFdClient methods to emit events
    _patch_ctfd(deps.ctfd, bus)

    # Wrap swarm creation to emit challenge events
    _patch_deps(deps, cost_tracker, bus)


def _patch_ctfd(ctfd: Any, bus: Any) -> None:
    """Patch CTFdClient to emit connection status events."""
    original_get = ctfd._get

    async def patched_get(path: str):
        try:
            result = await original_get(path)
            bus.emit_sync("ctfd_status", {"connected": True, "url": ctfd.base_url})
            return result
        except Exception as e:
            bus.emit_sync(
                "ctfd_status", {"connected": False, "url": ctfd.base_url, "error": str(e)}
            )
            raise

    ctfd._get = patched_get


def _patch_deps(deps: Any, cost_tracker: Any, bus: Any) -> None:
    """Patch deps to observe swarm creation and cost updates."""

    # Track cost periodically
    async def cost_updater():
        while True:
            await asyncio.sleep(5)
            try:
                by_model = cost_tracker.get_usage_by_model()
                bus.emit_sync(
                    "cost_update",
                    {
                        "total_cost": cost_tracker.total_cost_usd,
                        "total_tokens": cost_tracker.total_tokens,
                        "by_model": {
                            model: {
                                "cost_usd": info["cost"],
                                "input_tokens": info["input"],
                                "cached_tokens": info["cached"],
                                "output_tokens": info["output"],
                            }
                            for model, info in by_model.items()
                        },
                    },
                )
            except Exception:
                pass

    asyncio.create_task(cost_updater(), name="ui-cost-updater")


class SwarmObserver:
    """
    Wraps a ChallengeSwarm to emit UI events as the swarm progresses.
    Install by calling ``observe(swarm, bus)``.
    """

    @staticmethod
    def observe(swarm: Any, bus: Any) -> None:
        name = swarm.meta.name
        category = getattr(swarm.meta, "category", "")
        value = getattr(swarm.meta, "value", 0)

        # Emit challenge_started
        bus.emit_sync(
            "challenge_started",
            {
                "name": name,
                "category": category,
                "value": value,
                "models": swarm.model_specs,
                "status": "running",
            },
        )

        # Patch run() to emit result
        original_run = swarm.run

        async def patched_run():
            result = await original_run()
            if result and result.status == "flag_found":
                bus.emit_sync(
                    "challenge_solved",
                    {
                        "name": name,
                        "flag": result.flag or "",
                        "winner_model": _find_winner_model(swarm),
                        "cost_usd": result.cost_usd,
                        "steps": result.step_count,
                    },
                )
            else:
                # A cancelled swarm is usually operator-initiated stop/exclude or
                # auto-kill after solve; don't mark it as "failed".
                status = getattr(result, "status", "") if result else ""
                if status == "cancelled":
                    bus.emit_sync("challenge_update", {"name": name, "status": "stopped"})
                else:
                    bus.emit_sync("challenge_failed", {"name": name})
            return result

        swarm.run = patched_run

        # Patch get_status to emit solver_update periodically
        original_get_status = swarm.get_status

        async def status_poller():
            # Track per-solver trace offsets so we can stream trace events as UI log lines.
            trace_offsets: dict[str, int] = {}

            def _solver_progress(solver: Any) -> tuple[int, float, str]:
                """Best-effort: return (steps, cost_usd, findings)."""
                steps = 0
                cost = 0.0
                findings = ""

                # ClaudeSolver uses private fields
                if hasattr(solver, "_step_count"):
                    try:
                        steps = int(getattr(solver, "_step_count") or 0)
                    except Exception:
                        pass
                if hasattr(solver, "_cost_usd"):
                    try:
                        cost = float(getattr(solver, "_cost_usd") or 0.0)
                    except Exception:
                        pass
                if hasattr(solver, "_findings"):
                    try:
                        findings = str(getattr(solver, "_findings") or "")
                    except Exception:
                        pass

                # Pydantic-AI Solver stores step count in a list
                if steps == 0 and hasattr(solver, "_step_count"):
                    try:
                        sc = getattr(solver, "_step_count")
                        if isinstance(sc, list) and sc:
                            steps = int(sc[0])
                    except Exception:
                        pass

                return steps, cost, findings

            def _emit_trace_lines(challenge: str, model: str, trace_path: str) -> None:
                """Tail a JSONL solver trace file and emit as UI log lines."""
                try:
                    p = Path(trace_path)
                    if not p.exists():
                        return

                    off = trace_offsets.get(model, 0)
                    data = p.read_bytes()
                    if off > len(data):
                        off = 0
                    new = data[off:]
                    trace_offsets[model] = len(data)
                    if not new:
                        return

                    text = new.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except Exception:
                            continue

                        t = evt.get("type", "")
                        if t in (
                            "tool_call",
                            "tool_result",
                            "error",
                            "finish",
                            "bump",
                            "flag_confirmed",
                        ):
                            msg = None
                            if t == "tool_call":
                                msg = f"[{evt.get('step', '?')}] call {evt.get('tool', '?')}: {evt.get('args', '')!s}"
                            elif t == "tool_result":
                                msg = f"[{evt.get('step', '?')}] result {evt.get('tool', '?')}: {evt.get('result', '')!s}"
                            elif t == "error":
                                msg = f"error: {evt.get('error', '')}"
                            elif t == "finish":
                                msg = f"finish: {evt.get('status', '')} flag={evt.get('flag', '') or ''} cost=${evt.get('cost_usd', '')}"
                            elif t == "bump":
                                msg = f"bump: {evt.get('insights', '')}"
                            elif t == "flag_confirmed":
                                msg = f"flag confirmed: {evt.get('flag', '')}"

                            if msg:
                                bus.emit_sync(
                                    "log_line",
                                    {
                                        "challenge": challenge,
                                        "model": model,
                                        "text": msg[:2000],
                                        "level": "info" if t != "error" else "error",
                                    },
                                )
                except Exception:
                    return

            while not swarm.cancel_event.is_set():
                await asyncio.sleep(3)
                try:
                    status = original_get_status()
                    for model_spec, info in status.get("agents", {}).items():
                        solver = getattr(swarm, "solvers", {}).get(model_spec)
                        steps, cost, findings = _solver_progress(solver) if solver else (0, 0.0, "")

                        bus.emit_sync(
                            "solver_update",
                            {
                                "challenge": name,
                                "model": model_spec,
                                "status": info.get("status", "running"),
                                "steps": steps,
                                "cost": cost,
                                "findings": findings or info.get("findings", ""),
                            },
                        )

                        # Stream trace JSONL into UI logs if available
                        if solver is not None:
                            tracer = getattr(solver, "tracer", None)
                            trace_path = getattr(tracer, "path", "") if tracer else ""
                            if trace_path:
                                _emit_trace_lines(name, model_spec, str(trace_path))
                    # Cost update from swarm
                    bus.emit_sync(
                        "cost_update",
                        {
                            "total_cost": swarm.cost_tracker.total_cost_usd
                            if hasattr(swarm, "cost_tracker")
                            else 0,
                            "total_tokens": swarm.cost_tracker.total_tokens
                            if hasattr(swarm, "cost_tracker")
                            else 0,
                            "by_model": {},
                        },
                    )
                except Exception:
                    pass

        asyncio.create_task(status_poller(), name=f"ui-status-{name}")


def _find_winner_model(swarm: Any) -> str:
    """Try to determine which model found the flag."""
    if swarm.winner:
        for spec, result in swarm.findings.items():
            if swarm.winner.flag and swarm.winner.flag in result:
                return spec
    return ""
