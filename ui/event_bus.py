"""Global event bus — receives events from the backend and broadcasts to WebSocket clients."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_LOG_LINES = 500  # per challenge
MAX_HISTORY = 200  # global event history for reconnects


@dataclass
class UIEvent:
    """A single UI event pushed over WebSocket."""

    type: str
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "data": self.data, "ts": self.timestamp})


class EventBus:
    """
    Singleton event bus.

    Backend code calls ``emit(type, data)`` to broadcast events.
    WebSocket connections subscribe via ``subscribe()`` / ``unsubscribe()``.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[UIEvent] = deque(maxlen=MAX_HISTORY)
        self._lock = asyncio.Lock()

        # Live state — updated by events so new clients get a full snapshot
        self.challenges: dict[str, dict] = {}  # name -> challenge state
        self.cost_summary: dict[str, Any] = {}
        self.ctfd_status: dict[str, Any] = {"connected": False, "url": ""}
        self.total_cost: float = 0.0
        self.total_tokens: int = 0
        self.logs: dict[str, deque] = {}  # challenge_name -> log lines

    # ------------------------------------------------------------------ #
    #  Backend → bus                                                       #
    # ------------------------------------------------------------------ #

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event to all connected clients and update live state."""
        evt = UIEvent(type=event_type, data=data)
        self._history.append(evt)
        self._update_state(evt)

        msg = evt.to_json()
        dead: set[asyncio.Queue] = set()
        async with self._lock:
            subs = list(self._subscribers)

        for q in subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.add(q)

        if dead:
            async with self._lock:
                self._subscribers -= dead

    def emit_sync(self, event_type: str, data: dict[str, Any]) -> None:
        """Thread-safe sync wrapper — schedules coroutine on the running loop."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.emit(event_type, data))
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ #
    #  WebSocket subscription                                              #
    # ------------------------------------------------------------------ #

    async def subscribe(self) -> asyncio.Queue:
        """Register a new WebSocket client. Returns a queue to read events from."""
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.add(q)

        # Send full state snapshot as first message
        snapshot = {
            "type": "snapshot",
            "data": {
                "challenges": self.challenges,
                "cost_summary": self.cost_summary,
                "total_cost": self.total_cost,
                "total_tokens": self.total_tokens,
                "ctfd_status": self.ctfd_status,
                "logs": {k: list(v) for k, v in self.logs.items()},
            },
            "ts": time.time(),
        }
        q.put_nowait(json.dumps(snapshot))
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    # ------------------------------------------------------------------ #
    #  State updaters                                                      #
    # ------------------------------------------------------------------ #

    def _update_state(self, evt: UIEvent) -> None:  # noqa: C901
        d = evt.data
        match evt.type:
            case "challenge_new" | "challenge_update":
                name = d.get("name", "")
                if name:
                    self.challenges.setdefault(
                        name, {"name": name, "models": {}, "status": "pending"}
                    )
                    for k, v in d.items():
                        if k != "models":
                            self.challenges[name][k] = v

            case "challenge_started":
                name = d.get("name", "")
                if name:
                    self.challenges.setdefault(
                        name, {"name": name, "models": {}, "status": "running"}
                    )
                    self.challenges[name]["status"] = "running"
                    for k, v in d.items():
                        # Keep "models" as a dict for per-model status tracking;
                        # store the list of model specs under "model_specs" instead
                        if k == "models":
                            self.challenges[name]["model_specs"] = v
                        else:
                            self.challenges[name][k] = v
                    if name not in self.logs:
                        self.logs[name] = deque(maxlen=MAX_LOG_LINES)

            case "challenge_solved":
                name = d.get("name", "")
                if name and name in self.challenges:
                    self.challenges[name]["status"] = "solved"
                    self.challenges[name]["flag"] = d.get("flag", "")
                    self.challenges[name]["winner_model"] = d.get("winner_model", "")

            case "challenge_failed":
                name = d.get("name", "")
                if name and name in self.challenges:
                    self.challenges[name]["status"] = "failed"

            case "solver_update":
                ch = d.get("challenge", "")
                model = d.get("model", "")
                if ch and model:
                    self.challenges.setdefault(ch, {"name": ch, "models": {}, "status": "running"})
                    # Ensure models is a dict (not a list from challenge_started)
                    if not isinstance(self.challenges[ch].get("models"), dict):
                        self.challenges[ch]["models"] = {}
                    self.challenges[ch]["models"][model] = {
                        "status": d.get("status", "running"),
                        "steps": d.get("steps", 0),
                        "cost": d.get("cost", 0.0),
                        "findings": d.get("findings", ""),
                    }

            case "log_line":
                ch = d.get("challenge", "")
                if ch:
                    self.logs.setdefault(ch, deque(maxlen=MAX_LOG_LINES))
                    self.logs[ch].append(
                        {
                            "ts": evt.timestamp,
                            "model": d.get("model", ""),
                            "text": d.get("text", ""),
                            "level": d.get("level", "info"),
                        }
                    )

            case "cost_update":
                self.total_cost = d.get("total_cost", self.total_cost)
                self.total_tokens = d.get("total_tokens", self.total_tokens)
                self.cost_summary = d.get("by_model", self.cost_summary)

            case "ctfd_status":
                self.ctfd_status.update(d)


# Module-level singleton
_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
