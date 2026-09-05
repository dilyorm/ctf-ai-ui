"""The coordinator leases its own account from the shared pool.

Before this, only solvers used the pool: the coordinator read
`settings.claude_config_dir` (and the Codex one ignored configuration
altogether), so connecting a subscription on the Accounts page did nothing for
it and a run needed an API key.
"""

from __future__ import annotations

import pytest

from backend.account_pool import Lease
from backend.config import Settings
from backend.run_manager import GlobalRunManager


class FakePool:
    def __init__(self, *, accounts: bool, lease: Lease | None, free: int = 1) -> None:
        self._accounts = accounts
        self._lease = lease
        self._free = free
        self.lease_calls: list[tuple[str, int | None]] = []

    def has_accounts(self, provider: str) -> bool:
        return self._accounts

    def free(self, provider: str) -> int:
        return self._free

    async def lease(self, provider: str, *, exclude_id=None, prefer_id=None):
        self.lease_calls.append((provider, exclude_id))
        return self._lease


@pytest.fixture
def pool(monkeypatch):
    def _install(fake: FakePool) -> FakePool:
        monkeypatch.setattr("backend.account_pool.get_account_pool", lambda: fake)
        return fake

    return _install


def _lease(config_dir: str, label: str = "claude-a1b2c3", account_id: int = 7) -> Lease:
    return Lease(
        account_id=account_id, provider="claude", config_dir=config_dir, label=label
    )


@pytest.mark.parametrize(
    ("backend", "field", "provider"),
    [("claude", "claude_config_dir", "claude"), ("codex", "codex_config_dir", "codex")],
)
async def test_leased_account_config_dir_reaches_the_coordinator(
    pool, backend, field, provider
):
    fake = pool(FakePool(accounts=True, lease=_lease("/root/.claude-ctf-agents/acct-1"), free=2))
    mgr = GlobalRunManager()

    lease, run_settings = await mgr._lease_coordinator_account(backend, Settings())

    assert lease is not None
    assert fake.lease_calls == [(provider, None)]
    assert getattr(run_settings, field) == "/root/.claude-ctf-agents/acct-1"
    assert mgr.status()["coordinator_account"] == "claude-a1b2c3"


async def test_falls_back_when_no_account_is_connected(pool):
    """An API-key-only setup must keep working exactly as before."""
    pool(FakePool(accounts=False, lease=None))
    mgr = GlobalRunManager()
    settings = Settings(claude_config_dir="/preset")

    lease, run_settings = await mgr._lease_coordinator_account("claude", settings)

    assert lease is None
    assert run_settings is settings  # untouched
    assert mgr.status()["coordinator_account"] is None
    assert "No claude account" in mgr.status()["coordinator_note"]


async def test_falls_back_when_every_account_is_busy(pool):
    pool(FakePool(accounts=True, lease=None))
    mgr = GlobalRunManager()

    lease, run_settings = await mgr._lease_coordinator_account("claude", Settings())

    assert lease is None
    assert "busy or cooling" in mgr.status()["coordinator_note"]


async def test_warns_when_the_coordinator_takes_the_last_slot(pool):
    """Otherwise every solver parks and the run just looks stuck."""
    pool(FakePool(accounts=True, lease=_lease("/dir"), free=0))
    mgr = GlobalRunManager()

    await mgr._lease_coordinator_account("claude", Settings())

    note = mgr.status()["coordinator_note"]
    assert "solvers will park" in note
    assert "Max" in note  # tells the operator the actual remedy


async def test_no_note_when_capacity_remains(pool):
    pool(FakePool(accounts=True, lease=_lease("/dir"), free=3))
    mgr = GlobalRunManager()

    await mgr._lease_coordinator_account("claude", Settings())

    assert mgr.status()["coordinator_note"] is None


async def test_rotation_excludes_the_cooled_account(pool):
    fake = pool(FakePool(accounts=True, lease=_lease("/dir"), free=2))
    mgr = GlobalRunManager()

    await mgr._lease_coordinator_account("claude", Settings(), exclude_id=7)

    assert fake.lease_calls == [("claude", 7)]


async def test_unknown_backend_is_left_alone(pool):
    pool(FakePool(accounts=True, lease=_lease("/dir")))
    mgr = GlobalRunManager()
    settings = Settings()

    lease, run_settings = await mgr._lease_coordinator_account("something-else", settings)

    assert lease is None
    assert run_settings is settings


def test_sdk_env_clears_the_api_key_only_when_an_account_is_chosen():
    """A pooled subscription must not be silently overridden by the API key.

    ANTHROPIC_API_KEY beats the credentials in CLAUDE_CONFIG_DIR, so leaving it
    set would keep billing API usage while showing a connected account.
    """
    from backend.agents.claude_sdk import sdk_env

    ambient = sdk_env()
    assert "ANTHROPIC_API_KEY" not in ambient
    assert "CLAUDE_CONFIG_DIR" not in ambient
    assert ambient["IS_SANDBOX"] == "1"

    pooled = sdk_env("/root/.claude-ctf-agents/acct-1")
    assert pooled["CLAUDE_CONFIG_DIR"] == "/root/.claude-ctf-agents/acct-1"
    assert pooled["ANTHROPIC_API_KEY"] == ""
