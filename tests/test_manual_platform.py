"""Manual platform — solve hand-entered challenges, record candidate flags.

For a CTF that can't be connected (Cloudflare, captcha login), the operator adds
challenges as kanban Tasks and submits flags themselves. The swarm must be able
to read those Tasks, stage their files, and record a found flag as a candidate
without any upstream platform.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend import db as db_mod
from backend.db_models import CTF, Base, Task, TaskAttachment, User


@pytest_asyncio.fixture
async def session_factory(monkeypatch, tmp_path):
    """An isolated SQLite DB wired into the module SessionLocal the client uses."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    import backend.platforms.manual as manual_mod

    monkeypatch.setattr(manual_mod, "SessionLocal", maker)
    return maker


async def _seed(maker):
    async with maker() as db:
        u = User(email="op@x.com", password_hash="x")
        db.add(u)
        await db.flush()
        ctf = CTF(user_id=u.id, name="BHMEA", platform="manual", ctfd_url="manual://local")
        db.add(ctf)
        await db.flush()
        solved = Task(ctf_id=ctf.id, external_id="manual-a", name="done", status="solved")
        todo = Task(
            ctf_id=ctf.id, external_id="manual-b", name="TechShop",
            category="web", points=500, status="todo",
            platform_description_md="SSTI to RCE", connection_info="nc host 1337",
        )
        db.add_all([solved, todo])
        await db.flush()
        db.add(TaskAttachment(task_id=todo.id, kind="file", filename="app.zip", data=b"PK\x03\x04zip"))
        await db.commit()
        return ctf.id, todo.id


async def test_fetches_only_unsolved_challenges(session_factory):
    from backend.platforms.manual import ManualPlatformClient

    ctf_id, _ = await _seed(session_factory)
    client = ManualPlatformClient(ctf_id)

    chals = await client.fetch_all_challenges()
    assert [c["name"] for c in chals] == ["TechShop"]
    c = chals[0]
    assert c["category"] == "web"
    assert c["value"] == 500
    assert "SSTI" in c["description"]
    assert c["connection_info"] == "nc host 1337"

    assert await client.fetch_solved_names() == {"done"}


async def test_pull_writes_metadata_and_attachments(session_factory, tmp_path):
    from backend.platforms.manual import ManualPlatformClient

    ctf_id, _ = await _seed(session_factory)
    client = ManualPlatformClient(ctf_id)
    chal = (await client.fetch_all_challenges())[0]

    ch_dir = await client.pull_challenge(chal, str(tmp_path / "challenges"))

    import pathlib

    meta = pathlib.Path(ch_dir, "metadata.yml").read_text()
    assert "TechShop" in meta and "web" in meta
    staged = pathlib.Path(ch_dir, "distfiles", "app.zip")
    assert staged.read_bytes() == b"PK\x03\x04zip"


async def test_submit_records_candidate_and_stops(session_factory):
    from backend.platforms.manual import ManualPlatformClient

    ctf_id, todo_id = await _seed(session_factory)
    client = ManualPlatformClient(ctf_id)
    await client.fetch_all_challenges()  # populate name->id

    res = await client.submit_flag("TechShop", "BHMEA{ssti_rce}")

    # "correct" so the swarm stops burning seats, but clearly a candidate.
    assert res.status == "correct"
    assert "CANDIDATE" in res.display and "BHMEA{ssti_rce}" in res.display

    async with session_factory() as db:
        t = (await db.execute(select(Task).where(Task.id == todo_id))).scalar_one()
    assert t.flag == "BHMEA{ssti_rce}"
    assert t.status == "needs_review"  # surfaced for the operator to submit


async def test_empty_flag_is_rejected(session_factory):
    from backend.platforms.manual import ManualPlatformClient

    ctf_id, _ = await _seed(session_factory)
    client = ManualPlatformClient(ctf_id)
    res = await client.submit_flag("TechShop", "   ")
    assert res.status == "incorrect"
