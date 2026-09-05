"""In-process run manager.

Single-server mode: only one active run at a time (global).
We still associate the run with the user who started it for audit/control.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re

from backend.agents.claude_coordinator import run_claude_coordinator
from backend.agents.codex_coordinator import run_codex_coordinator
from backend.config import Settings

logger = logging.getLogger(__name__)

# Which pool provider backs each coordinator, and the Settings field its
# leased config directory is injected into.
_COORDINATOR_POOL = {
    "claude": ("claude", "claude_config_dir"),
    "codex": ("codex", "codex_config_dir"),
}

# A coordinator that dies on a usage limit should rotate onto another pooled
# account rather than ending the run.
_QUOTA = re.compile(
    r"429|rate.?limit|quota|too many requests|usage limit|out of credits|insufficient",
    re.I,
)
_MAX_ROTATIONS = 3


class GlobalRunManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._started_by_user_id: int | None = None
        self._started_at: dt.datetime | None = None
        self._last_result: dict | None = None
        self._last_error: str | None = None
        # Label of the pooled account the coordinator is currently signed in as.
        self._coordinator_account: str | None = None
        self._coordinator_note: str | None = None
        self._max_concurrent: int = 10
        # Per-challenge runtime controls (names are challenge slugs/display names)
        self.stopped_challenges: set[str] = set()
        self.priority_challenges: set[str] = set()
        self.excluded_challenges: set[str] = set()

    def status(self) -> dict:
        t = self._task
        running = bool(t and not t.done())
        return {
            "running": running,
            "started_by_user_id": self._started_by_user_id,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "last_result": self._last_result,
            "last_error": self._last_error,
            "coordinator_account": self._coordinator_account,
            "coordinator_note": self._coordinator_note,
            "max_concurrent": self._max_concurrent,
            "stopped_challenges": sorted(self.stopped_challenges),
            "priority_challenges": sorted(self.priority_challenges),
            "excluded_challenges": sorted(self.excluded_challenges),
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

    def toggle_exclude(self, name: str) -> dict:
        """Toggle excluded state for a specific challenge.

        Excluded challenges should not be auto-spawned again during the current run.
        """
        if name in self.excluded_challenges:
            self.excluded_challenges.discard(name)
            return {"ok": True, "excluded": False, "name": name}
        self.excluded_challenges.add(name)
        # Excluding implies stopped for the current run.
        self.stopped_challenges.add(name)
        return {"ok": True, "excluded": True, "stopped": True, "name": name}

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

            # Load the shared account pool so solvers can lease/rotate accounts.
            try:
                from backend.account_pool import get_account_pool

                await get_account_pool().reload()
            except Exception as e:
                logger.warning("Account pool reload failed at run start: %s", e)

            self._started_by_user_id = user_id
            self._started_at = dt.datetime.now(dt.UTC)
            self._last_result = None
            self._last_error = None
            self.stopped_challenges = set()
            self.priority_challenges = set()
            self.excluded_challenges = set()

            async def _run_once(run_settings: Settings) -> dict:
                if coordinator_backend == "codex":
                    return await run_codex_coordinator(
                        settings=run_settings,
                        model_specs=model_specs,
                        challenges_root=challenges_dir,
                        exclude_challenges=exclude_challenges,
                        exclude_challenge_regex=exclude_challenge_regex,
                        no_submit=no_submit,
                        coordinator_model=coordinator_model,
                        msg_port=msg_port,
                    )
                return await run_claude_coordinator(
                    settings=run_settings,
                    model_specs=model_specs,
                    challenges_root=challenges_dir,
                    exclude_challenges=exclude_challenges,
                    exclude_challenge_regex=exclude_challenge_regex,
                    no_submit=no_submit,
                    coordinator_model=coordinator_model,
                    msg_port=msg_port,
                )

            async def _runner() -> None:
                from backend.account_pool import get_account_pool

                pool = get_account_pool()
                lease = None
                exclude_id: int | None = None
                try:
                    for attempt in range(_MAX_ROTATIONS + 1):
                        lease, run_settings = await self._lease_coordinator_account(
                            coordinator_backend, settings, exclude_id=exclude_id
                        )
                        try:
                            self._last_result = await _run_once(run_settings)
                            return
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            # A usage limit on the coordinator's own account should
                            # cool it down and move to the next one, the same way a
                            # solver rotates, instead of ending the whole run.
                            if lease is None or not _QUOTA.search(str(e)) or attempt == _MAX_ROTATIONS:
                                raise
                            await pool.mark_cooldown(lease)
                            exclude_id = lease.account_id
                            lease = None  # mark_cooldown released it
                            logger.warning(
                                "Coordinator account hit a usage limit; rotating (attempt %d/%d): %s",
                                attempt + 1, _MAX_ROTATIONS, e,
                            )
                        finally:
                            if lease is not None:
                                await pool.release(lease)
                                lease = None
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("run failed: %s", e, exc_info=True)
                    self._last_error = str(e)
                finally:
                    if lease is not None:
                        await pool.release(lease)
                    self._coordinator_account = None

            self._task = asyncio.create_task(_runner(), name="global-ctf-run")
            return {"ok": True}

    async def _lease_coordinator_account(
        self, backend: str, settings: Settings, *, exclude_id: int | None = None
    ):
        """Lease a pooled account for the coordinator, if one is connected.

        Returns ``(lease, settings)`` — the settings carry the leased account's
        isolated config dir so the coordinator's CLI signs in as that account.
        When no account is connected (or none is free) this falls back to the
        ambient configuration, which is how the coordinator worked before: an
        API key, or a config dir set in Settings.
        """
        from backend.account_pool import get_account_pool

        mapping = _COORDINATOR_POOL.get(backend)
        if not mapping:
            self._coordinator_account = None
            self._coordinator_note = None
            return None, settings
        provider, field = mapping
        pool = get_account_pool()
        if not pool.has_accounts(provider):
            self._coordinator_account = None
            self._coordinator_note = (
                f"No {provider} account in the pool — the coordinator is using the "
                f"API key / server configuration."
            )
            return None, settings

        lease = await pool.lease(provider, exclude_id=exclude_id)
        if lease is None:
            self._coordinator_account = None
            self._coordinator_note = (
                f"All {provider} accounts are busy or cooling — the coordinator fell "
                f"back to the API key / server configuration."
            )
            logger.warning("Coordinator could not lease a '%s' account", provider)
            return None, settings

        self._coordinator_account = lease.label
        # The coordinator holds its account for the whole run. If that was the
        # last slot, every solver on this provider will park, so say so plainly
        # rather than letting the run look stuck.
        if pool.free(provider) == 0:
            self._coordinator_note = (
                f"The coordinator holds the only free {provider} slot, so {provider} "
                f"solvers will park. Raise 'Max' on a {provider} account (Accounts page) "
                f"to give solvers room."
            )
            logger.warning(
                "Coordinator leased the last free '%s' slot (%s); solvers will park",
                provider, lease.label,
            )
        else:
            self._coordinator_note = None
        logger.info("Coordinator signed in as pooled account %s (%s)", lease.label, provider)
        return lease, settings.model_copy(update={field: lease.config_dir})

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
            # Best-effort cleanup: canceling the coordinator can leave sandboxes running.
            try:
                from backend.sandbox import cleanup_orphan_containers

                await cleanup_orphan_containers()
            except Exception:
                pass
            return {"ok": True, "stopped": True}


_mgr: GlobalRunManager | None = None


def get_run_manager() -> GlobalRunManager:
    global _mgr
    if _mgr is None:
        _mgr = GlobalRunManager()
    return _mgr
