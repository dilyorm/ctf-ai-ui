"""Manual platform — solve challenges you enter by hand, submit flags yourself.

For a CTF whose platform can't be connected (Cloudflare, captcha-gated login,
an unusual API), the operator adds each challenge as a kanban Task — name,
category, points, description, connection info, and file attachments — and the
swarm attacks those exactly as it would platform-pulled ones.

There is nothing to submit *to*, so a found flag is recorded as a candidate on
its Task (status → needs_review) for the operator to verify and submit on the
real platform. Recording a candidate stops that challenge's swarm and frees its
seats for the next challenge, which is the point during a live CTF: surface a
strong candidate per challenge and move on, rather than burning a subscription
re-solving something already solved.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.ctfd import SubmitResult
from backend.db import SessionLocal
from backend.db_models import CTF as _CTF
from backend.db_models import Task, TaskAttachment

logger = logging.getLogger(__name__)

# Statuses that mean "don't attack this": already handled by a human or the AI.
_DONE_STATUSES = {"solved", "skipped", "needs_review"}


def _slug(name: str) -> str:
    slug = re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "", name.lower().strip())
    slug = re.sub(r"[\s_]+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-") or "challenge"


class ManualPlatformClient:
    """PlatformClient backed by the CTF's hand-entered kanban Tasks."""

    def __init__(self, ctf_id: int) -> None:
        self.ctf_id = int(ctf_id)
        self.base_url = f"manual://ctf/{self.ctf_id}"
        self.token = ""
        # name -> task id, filled by fetch; used by submit to find the row.
        self._task_ids: dict[str, int] = {}

    # ── reads ────────────────────────────────────────────────────────────────

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        return await self.fetch_all_challenges()

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        async with SessionLocal() as db:
            rows = (
                await db.execute(select(Task).where(Task.ctf_id == self.ctf_id))
            ).scalars().all()
        out: list[dict[str, Any]] = []
        for t in rows:
            if t.status in _DONE_STATUSES:
                continue
            self._task_ids[t.name] = t.id
            try:
                files = json.loads(t.files_json) if t.files_json else []
            except json.JSONDecodeError:
                files = []
            out.append(
                {
                    "id": t.external_id or str(t.id),
                    "name": t.name,
                    "category": t.category or "",
                    "value": t.points or 0,
                    "description": (t.description_override_md or t.platform_description_md or ""),
                    "connection_info": t.connection_info or "",
                    "files": files,
                    "solves": t.solves or 0,
                    "tags": [],
                    "_task_id": t.id,
                }
            )
        return out

    async def fetch_solved_names(self) -> set[str]:
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(Task.name).where(
                        Task.ctf_id == self.ctf_id, Task.status == "solved"
                    )
                )
            ).scalars().all()
        return set(rows)

    async def get_challenge_id(self, name: str) -> int | str:
        return self._task_ids.get(name, name)

    # ── pull: write a challenge dir the sandbox can mount ─────────────────────

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        import yaml

        name = challenge.get("name", "challenge")
        ch_dir = Path(output_dir) / _slug(name)
        ch_dir.mkdir(parents=True, exist_ok=True)

        task_id = challenge.get("_task_id")
        if task_id is None:
            task_id = self._task_ids.get(name)
        if task_id is not None:
            async with SessionLocal() as db:
                atts = (
                    await db.execute(
                        select(TaskAttachment).where(
                            TaskAttachment.task_id == int(task_id),
                            TaskAttachment.kind != "writeup",
                        )
                    )
                ).scalars().all()
                if atts:
                    dist = ch_dir / "distfiles"
                    dist.mkdir(exist_ok=True)
                    for a in atts:
                        safe = re.sub(r"[\\/]", "_", a.filename or "file") or "file"
                        (dist / safe).write_bytes(a.data or b"")
                        logger.info("manual: staged %s (%d bytes)", safe, len(a.data or b""))

        meta = {
            "name": name,
            "category": challenge.get("category", ""),
            "description": (challenge.get("description") or "").strip(),
            "value": challenge.get("value", 0),
            "connection_info": challenge.get("connection_info") or "",
            "tags": [],
            "solves": challenge.get("solves", 0),
        }
        (ch_dir / "metadata.yml").write_text(
            yaml.safe_dump(meta, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        return str(ch_dir)

    # ── "submit": record a candidate for the operator to verify ───────────────

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        flag = (flag or "").strip()
        if not flag:
            return SubmitResult("incorrect", "empty flag", "Empty flag — nothing to record.")

        task_id = self._task_ids.get(challenge_name)
        recorded = False
        if task_id is not None:
            async with SessionLocal() as db:
                t = await db.get(Task, task_id)
                if t is not None:
                    t.flag = flag
                    # Leave a human-solved/needs_review task alone; otherwise move
                    # it to review so the operator sees the candidate to submit.
                    if t.status not in ("solved", "skipped"):
                        t.status = "needs_review"
                    t.last_solver_status = "flag candidate (manual submit)"
                    t.updated_at = dt.datetime.now(dt.UTC)
                    await db.commit()
                    recorded = True

        where = " Saved to the board (Needs Review)." if recorded else ""
        display = (
            f'CANDIDATE FLAG recorded: "{flag}".{where} '
            "Verify it and submit on the real platform yourself — this run does "
            "not auto-submit."
        )
        # Report as accepted so the swarm stops this challenge and moves its
        # seats to the next one. It is a *candidate*, made explicit above.
        return SubmitResult("correct", "candidate recorded", display)

    async def close(self) -> None:
        return None


async def create_manual_ctf_placeholder_url() -> str:
    """A CTF row needs a non-empty url; manual CTFs don't use one."""
    return "manual://local"


async def resolve_ctf_id(base_url: str) -> int:
    """Extract the ctf id from a ``manual://ctf/<id>`` base url."""
    m = re.search(r"manual://ctf/(\d+)", base_url or "")
    return int(m.group(1)) if m else 0


async def manual_ctf_exists(ctf_id: int) -> bool:
    async with SessionLocal() as db:
        return await db.get(_CTF, int(ctf_id)) is not None
