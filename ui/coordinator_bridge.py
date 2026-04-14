"""Bridge between the coordinator backend and the UI event bus.

Patches the coordinator_loop and swarm to emit events to the UI event bus
so the dashboard gets real-time updates.

Usage (from cli.py or coordinator code):
    from ui.coordinator_bridge import install_bridge
    install_bridge(deps, cost_tracker)
"""

from __future__ import annotations

import asyncio
import logging
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
                bus.emit_sync("challenge_failed", {"name": name})
            return result

        swarm.run = patched_run

        # Patch get_status to emit solver_update periodically
        original_get_status = swarm.get_status

        async def status_poller():
            while not swarm.cancel_event.is_set():
                await asyncio.sleep(3)
                try:
                    status = original_get_status()
                    for model_spec, info in status.get("agents", {}).items():
                        bus.emit_sync(
                            "solver_update",
                            {
                                "challenge": name,
                                "model": model_spec,
                                "status": info.get("status", "running"),
                                "findings": info.get("findings", ""),
                            },
                        )
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
