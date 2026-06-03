"""Team-wide shared account pool with quota failover.

The pool holds subscription accounts (Claude Code / Codex), each backed by an
isolated CLI config directory. It is a single in-process singleton shared by the
FastAPI app and the coordinator run task (they live in the same event loop).

Solvers *lease* an account before they start and *release* it when they finish.
When a solver hits a quota/limit error the swarm puts the leased account on a
cooldown and leases the next available account of the same provider. When every
account of a provider is busy or cooling down, `lease()` returns ``None`` and the
challenge is parked (see ChallengeSwarm / coordinator backoff).

Concurrency is enforced per account via ``max_concurrent`` (default 1, because
subscriptions rate-limit hard on parallel sessions). Cooldowns are persisted to
the DB so they survive a process restart.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from backend.cli_auth import is_authenticated
from backend.db import SessionLocal
from backend.db_models import PooledAccount

logger = logging.getLogger(__name__)

# Default cooldown when a provider error carries no explicit reset time.
DEFAULT_COOLDOWN_SECONDS = 3600  # 1 hour (conservative)

# Map a model-spec provider to a pool provider. Only subscription-backed
# providers participate in the pool; API-key providers are unaffected.
_SPEC_PROVIDER_TO_POOL = {
    "claude-sdk": "claude",
    "codex": "codex",
}


def pool_provider_for_spec(spec_provider: str) -> str | None:
    """Return the pool provider for a model-spec provider, or None if not pooled."""
    return _SPEC_PROVIDER_TO_POOL.get(spec_provider)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class _Account:
    id: int
    provider: str
    label: str
    config_dir: str
    max_concurrent: int
    disabled: bool
    cooldown_until: dt.datetime | None
    last_used_at: dt.datetime | None
    active_leases: int = 0

    def available(self, now: dt.datetime) -> bool:
        if self.disabled:
            return False
        if self.active_leases >= max(1, self.max_concurrent):
            return False
        if self.cooldown_until and self.cooldown_until > now:
            return False
        return True


@dataclass
class Lease:
    account_id: int
    provider: str
    config_dir: str
    label: str


@dataclass
class AccountPool:
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _accounts: dict[int, _Account] = field(default_factory=dict)

    async def reload(self) -> int:
        """(Re)load authenticated accounts from the DB, preserving live lease counts.

        Only accounts whose config directory actually contains credentials are
        admitted, so half-finished sign-ins never get leased.
        """
        async with self._lock:
            prev_leases = {aid: a.active_leases for aid, a in self._accounts.items()}
            async with SessionLocal() as db:
                rows = (await db.execute(select(PooledAccount))).scalars().all()
                new: dict[int, _Account] = {}
                for r in rows:
                    if not is_authenticated(r.provider, r.config_dir):
                        continue
                    new[r.id] = _Account(
                        id=r.id,
                        provider=r.provider,
                        label=r.label or r.config_dir,
                        config_dir=r.config_dir,
                        max_concurrent=r.max_concurrent or 1,
                        disabled=r.disabled,
                        cooldown_until=r.cooldown_until,
                        last_used_at=r.last_used_at,
                        active_leases=prev_leases.get(r.id, 0),
                    )
            self._accounts = new
            logger.info("Account pool loaded: %d authenticated account(s)", len(new))
            return len(new)

    # ── leasing ────────────────────────────────────────────────────────────

    async def lease(self, provider: str) -> Lease | None:
        """Lease the best available account of *provider*, or None if none free.

        Selection spreads load: prefer the lowest in-use ratio, then the
        least-recently-used account.
        """
        async with self._lock:
            now = _now()
            candidates = [
                a for a in self._accounts.values() if a.provider == provider and a.available(now)
            ]
            if not candidates:
                return None

            def _key(a: _Account):
                ratio = a.active_leases / max(1, a.max_concurrent)
                lru = a.last_used_at or dt.datetime.min.replace(tzinfo=dt.UTC)
                return (ratio, lru)

            acct = min(candidates, key=_key)
            acct.active_leases += 1
            acct.last_used_at = now
            asyncio.create_task(self._persist_last_used(acct.id, now))
            return Lease(
                account_id=acct.id,
                provider=acct.provider,
                config_dir=acct.config_dir,
                label=acct.label,
            )

    async def release(self, lease: Lease) -> None:
        async with self._lock:
            acct = self._accounts.get(lease.account_id)
            if acct and acct.active_leases > 0:
                acct.active_leases -= 1

    async def mark_cooldown(self, lease: Lease, seconds: int | None = None) -> dt.datetime:
        """Put a leased account on cooldown and release the lease. Returns the until-time."""
        secs = seconds or DEFAULT_COOLDOWN_SECONDS
        until = _now() + dt.timedelta(seconds=secs)
        async with self._lock:
            acct = self._accounts.get(lease.account_id)
            if acct:
                acct.cooldown_until = until
                if acct.active_leases > 0:
                    acct.active_leases -= 1
        await self._persist_cooldown(lease.account_id, until)
        logger.warning(
            "Account %s (%s) on cooldown for %ds (until %s)",
            lease.label,
            lease.provider,
            secs,
            until.isoformat(),
        )
        return until

    # ── introspection ────────────────────────────────────────────────────────

    def free(self, provider: str) -> int:
        """Number of additional leases available for *provider* right now."""
        now = _now()
        total = 0
        for a in self._accounts.values():
            if a.provider != provider or a.disabled:
                continue
            if a.cooldown_until and a.cooldown_until > now:
                continue
            total += max(0, max(1, a.max_concurrent) - a.active_leases)
        return total

    def has_accounts(self, provider: str) -> bool:
        return any(a.provider == provider and not a.disabled for a in self._accounts.values())

    def any_accounts(self) -> bool:
        return any(not a.disabled for a in self._accounts.values())

    def earliest_cooldown(self, provider: str) -> dt.datetime | None:
        """Soonest time an account of *provider* becomes available again."""
        now = _now()
        times: list[dt.datetime] = []
        for a in self._accounts.values():
            if a.provider != provider or a.disabled:
                continue
            if a.cooldown_until and a.cooldown_until > now:
                times.append(a.cooldown_until)
        return min(times) if times else None

    def snapshot(self) -> list[dict]:
        now = _now()
        out: list[dict] = []
        for a in self._accounts.values():
            if a.disabled:
                status = "disabled"
            elif a.cooldown_until and a.cooldown_until > now:
                status = "cooling"
            elif a.active_leases > 0:
                status = "in_use"
            else:
                status = "healthy"
            out.append(
                {
                    "id": a.id,
                    "provider": a.provider,
                    "label": a.label,
                    "status": status,
                    "active_leases": a.active_leases,
                    "max_concurrent": a.max_concurrent,
                    "cooldown_until": a.cooldown_until.isoformat() if a.cooldown_until else None,
                }
            )
        return out

    # ── persistence ────────────────────────────────────────────────────────

    async def _persist_cooldown(self, account_id: int, until: dt.datetime) -> None:
        try:
            async with SessionLocal() as db:
                row = await db.get(PooledAccount, account_id)
                if row:
                    row.cooldown_until = until
                    await db.commit()
        except Exception as e:
            logger.warning("Failed to persist cooldown for account %s: %s", account_id, e)

    async def _persist_last_used(self, account_id: int, when: dt.datetime) -> None:
        try:
            async with SessionLocal() as db:
                row = await db.get(PooledAccount, account_id)
                if row:
                    row.last_used_at = when
                    await db.commit()
        except Exception:
            pass


_pool: AccountPool | None = None


def get_account_pool() -> AccountPool:
    global _pool
    if _pool is None:
        _pool = AccountPool()
    return _pool


def parse_cooldown_seconds(error_text: str) -> int | None:
    """Best-effort extraction of a reset/retry window from a provider error string.

    Handles common shapes like ``retry-after: 120``, ``try again in 5m``,
    ``resets in 3 hours``, ``"reset_in_seconds": 7200``. Returns None when no
    explicit window is found (caller falls back to DEFAULT_COOLDOWN_SECONDS).
    """
    if not error_text:
        return None
    import re

    t = error_text.lower()

    m = re.search(r"(?:retry[\s_-]?after|reset[\s_-]?in[\s_-]?seconds)[\"'\s:=]+(\d+)", t)
    if m:
        return int(m.group(1))

    # "try again in 30s" / "resets in 5 minutes" / "in 2 hours"
    m = re.search(r"(?:in|after)\s+(\d+)\s*(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours)", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("s"):
            return n
        if unit.startswith("m"):
            return n * 60
        if unit.startswith("h"):
            return n * 3600
    return None
