"""In-process run manager.

Single-server mode: only one active run at a time (global).
We still associate the run with the user who started it for audit/control.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from backend.agents.claude_coordinator import run_claude_coordinator
from backend.agents.codex_coordinator import run_codex_coordinator
from backend.config import Settings

logger = logging.getLogger(__name__)


class GlobalRunManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._started_by_user_id: int | None = None
        self._started_at: dt.datetime | None = None
        self._last_result: dict | None = None
        self._last_error: str | None = None
        self._max_concurrent: int = 10
        # Per-challenge runtime controls (names are challenge slugs/display names)
        self.stopped_challenges: set[str] = set()
        self.priority_challenges: set[str] = set()

    def status(self) -> dict:
        t = self._task
        running = bool(t and not t.done())
        return {
            "running": running,
            "started_by_user_id": self._started_by_user_id,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "last_result": self._last_result,
            "last_error": self._last_error,
            "max_concurrent": self._max_concurrent,
            "stopped_challenges": sorted(self.stopped_challenges),
            "priority_challenges": sorted(self.priority_challenges),
        }

    def stop_challenge(self, name: str) -> dict:
        """Toggle stopped state for a specific challenge."""
        if name in self.stopped_challenges:
            self.stopped_challenges.discard(name)
            return {"ok": True, "stopped": False, "name": name}
        self.stopped_challenges.add(name)
        return {"ok": True, "stopped": True, "name": name}

    def toggle_priority(self, name: str) -> dict:
        """Toggle high-priority flag for a specific challenge."""
        if name in self.priority_challenges:
            self.priority_challenges.discard(name)
            return {"ok": True, "priority": False, "name": name}
        self.priority_challenges.add(name)
        return {"ok": True, "priority": True, "name": name}

    def set_max_concurrent(self, n: int) -> dict:
        self._max_concurrent = max(1, min(n, 50))
        return {"ok": True, "max_concurrent": self._max_concurrent}

    async def start(
        self,
        *,
        user_id: int,
        settings: Settings,
        model_specs: list[str],
        challenges_dir: str = "challenges",
        exclude_challenges: list[str] | None = None,
        exclude_challenge_regex: str | None = None,
        no_submit: bool = False,
        coordinator_backend: str = "claude",
        coordinator_model: str | None = None,
        msg_port: int = 0,
    ) -> dict:
        async with self._lock:
            if self._task and not self._task.done():
                return {"ok": False, "error": "run already active"}

            self._started_by_user_id = user_id
            self._started_at = dt.datetime.now(dt.timezone.utc)
            self._last_result = None
            self._last_error = None
            self.stopped_challenges = set()
            self.priority_challenges = set()

            async def _runner() -> None:
                try:
                    if coordinator_backend == "codex":
                        result = await run_codex_coordinator(
                            settings=settings,
                            model_specs=model_specs,
                            challenges_root=challenges_dir,
                            exclude_challenges=exclude_challenges,
                            exclude_challenge_regex=exclude_challenge_regex,
                            no_submit=no_submit,
                            coordinator_model=coordinator_model,
                            msg_port=msg_port,
                        )
                    else:
                        result = await run_claude_coordinator(
                            settings=settings,
                            model_specs=model_specs,
                            challenges_root=challenges_dir,
                            exclude_challenges=exclude_challenges,
                            exclude_challenge_regex=exclude_challenge_regex,
                            no_submit=no_submit,
                            coordinator_model=coordinator_model,
                            msg_port=msg_port,
                        )
                    self._last_result = result
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("run failed: %s", e, exc_info=True)
                    self._last_error = str(e)
                finally:
                    # Keep started_by/at for visibility; running bit comes from task state.
                    pass

            self._task = asyncio.create_task(_runner(), name="global-ctf-run")
            return {"ok": True}

    async def stop(self, *, user_id: int, force: bool = False) -> dict:
        async with self._lock:
            if not self._task or self._task.done():
                return {"ok": True, "stopped": False}
            if not force and self._started_by_user_id not in (None, user_id):
                return {"ok": False, "error": "only run owner can stop"}

            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            return {"ok": True, "stopped": True}


_mgr: GlobalRunManager | None = None


def get_run_manager() -> GlobalRunManager:
    global _mgr
    if _mgr is None:
        _mgr = GlobalRunManager()
    return _mgr
