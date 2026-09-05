"""FastAPI routes for the /team kanban.

Endpoints:

- ``GET /team`` — server-rendered kanban page
- ``GET /api/team/ctfs`` — list CTFs available as kanban boards
- ``GET /api/team/members`` — active users (for the assignee dropdown)
- ``GET /api/team/tasks?ctf_id=…`` — list tasks for a CTF
- ``POST /api/team/tasks/sync`` — pull fresh challenges from the CTF platform
  and upsert them as Task rows (never overwrites team-owned columns)
- ``GET /api/team/tasks/{id}`` — single task, including attachments
- ``PATCH /api/team/tasks/{id}`` — update status/assignee/flag/description/notes/writeup
- ``POST /api/team/tasks/{id}/attachments`` — upload an attachment (writeup or file)
- ``GET /api/team/tasks/{id}/attachments/{attachment_id}`` — download attachment
- ``DELETE /api/team/tasks/{id}/attachments/{attachment_id}`` — delete attachment
- ``POST /api/team/tasks/{id}/generate-writeup`` — pull the latest solver log for
  the challenge and generate a markdown writeup using the configured LLM
"""

from __future__ import annotations

import datetime as dt
import json as _json
import logging
import uuid as _uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.crypto import open_opt
from backend.db import get_db
from backend.db_models import CTF as CTFModel
from backend.db_models import Task, TaskAttachment, User
from backend.platforms import make_platform_client

logger = logging.getLogger(__name__)

router = APIRouter()

STATUS_VALUES = ("todo", "in_progress", "blocked", "needs_review", "solved", "skipped")
ASSIGNEE_TYPES = ("user", "ai")
import os as _os
# CTF distfiles get big (binaries, pcaps, disk images). Cap generously;
# override with ATTACHMENT_MAX_MB. nginx client_max_body_size must be >= this.
MAX_ATTACHMENT_BYTES = int(_os.environ.get('ATTACHMENT_MAX_MB', '512')) * 1024 * 1024


def _normalize_description(raw: str, platform: str) -> str:
    """Convert upstream descriptions to markdown. CTFd sends HTML; rCTF sends MD."""
    if not raw:
        return ""
    if platform == "ctfd" and ("<" in raw and ">" in raw):
        try:
            from markdownify import markdownify as html2md

            return html2md(raw, heading_style="atx", escape_asterisks=False).strip()
        except Exception:
            return raw
    return raw


def _get_session_user(request: Request) -> dict | None:
    return request.session.get("user")


async def _require_team_member(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    """/team is available to any active authenticated user (single-team model)."""
    sess = _get_session_user(request)
    if not sess or not sess.get("user_id"):
        raise HTTPException(status_code=401, detail="unauthorized")
    user = await db.get(User, int(sess["user_id"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def _task_to_dict(t: Task, attachments: list[TaskAttachment] | None = None) -> dict[str, Any]:
    try:
        files = _json.loads(t.files_json or "[]")
    except Exception:
        files = []
    return {
        "id": t.id,
        "ctf_id": t.ctf_id,
        "external_id": t.external_id,
        "name": t.name,
        "category": t.category,
        "points": t.points,
        "solves": t.solves,
        "platform_description_md": t.platform_description_md,
        "description_override_md": t.description_override_md,
        "notes_md": t.notes_md,
        "writeup_md": t.writeup_md,
        "flag": t.flag,
        "files": files,
        "connection_info": t.connection_info,
        "status": t.status,
        "assignee_type": t.assignee_type,
        "assignee_user_id": t.assignee_user_id,
        "priority": t.priority,
        "last_solver_status": t.last_solver_status,
        "solved_at": t.solved_at.isoformat() if t.solved_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "attachments": [
            {
                "id": a.id,
                "kind": a.kind,
                "filename": a.filename,
                "content_type": a.content_type,
                "size_bytes": a.size_bytes,
                "uploaded_by_user_id": a.uploaded_by_user_id,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in (attachments or [])
        ],
    }


def register_team_routes(app, templates: Jinja2Templates) -> None:
    """Mount /team routes on *app* using the shared Jinja templates."""

    @app.get("/team", response_class=HTMLResponse)
    async def team_page(request: Request, db: AsyncSession = Depends(get_db)):
        sess = _get_session_user(request)
        if not sess:
            return RedirectResponse("/login")
        user = await db.get(User, int(sess["user_id"]))
        if not user or not user.is_active:
            return RedirectResponse("/login")
        # Load all CTFs the team has connected
        ctfs = (
            (await db.execute(select(CTFModel).order_by(CTFModel.id.desc())))
            .scalars()
            .all()
        )
        return templates.TemplateResponse(
            request=request,
            name="team.html",
            context={
                "user": {**sess, "role": user.role},
                "ctfs": [
                    {"id": c.id, "name": c.name, "platform": c.platform or "ctfd"}
                    for c in ctfs
                ],
            },
        )

    app.include_router(router)


@router.get("/api/team/ctfs")
async def api_team_list_ctfs(
    user: User = Depends(_require_team_member), db: AsyncSession = Depends(get_db)
):
    rows = (
        (await db.execute(select(CTFModel).order_by(CTFModel.id.desc()))).scalars().all()
    )
    return JSONResponse(
        {
            "ok": True,
            "ctfs": [
                {
                    "id": c.id,
                    "name": c.name,
                    "platform": c.platform or "ctfd",
                    "ctfd_url": c.ctfd_url,
                }
                for c in rows
            ],
        }
    )


@router.get("/api/team/members")
async def api_team_members(
    user: User = Depends(_require_team_member), db: AsyncSession = Depends(get_db)
):
    rows = (
        (await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.id)))
        .scalars()
        .all()
    )
    return JSONResponse(
        {
            "ok": True,
            "members": [
                {
                    "id": u.id,
                    "email": u.email,
                    "display_name": u.display_name or u.email.split("@")[0],
                    "role": u.role,
                }
                for u in rows
            ],
        }
    )


@router.get("/api/team/tasks")
async def api_team_list_tasks(
    ctf_id: int,
    user: User = Depends(_require_team_member),
    db: AsyncSession = Depends(get_db),
):
    ctf = await db.get(CTFModel, ctf_id)
    if not ctf:
        return JSONResponse({"ok": False, "error": "CTF not found"}, status_code=404)
    rows = (
        (
            await db.execute(
                select(Task).where(Task.ctf_id == ctf_id).order_by(Task.priority.desc(), Task.id)
            )
        )
        .scalars()
        .all()
    )
    return JSONResponse(
        {
            "ok": True,
            "ctf": {"id": ctf.id, "name": ctf.name, "platform": ctf.platform or "ctfd"},
            "tasks": [_task_to_dict(t) for t in rows],
        }
    )


@router.post("/api/team/tasks/sync")
async def api_team_sync_tasks(
    request: Request,
    user: User = Depends(_require_team_member),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    ctf_id = int(body.get("ctf_id") or 0)
    if not ctf_id:
        return JSONResponse({"ok": False, "error": "ctf_id is required"}, status_code=400)
    ctf = await db.get(CTFModel, ctf_id)
    if not ctf:
        return JSONResponse({"ok": False, "error": "CTF not found"}, status_code=404)

    token = open_opt(ctf.ctfd_token_enc) or ""
    platform = ctf.platform or "ctfd"
    client = make_platform_client(
        platform=platform,
        base_url=ctf.ctfd_url,
        token=token,
    )
    try:
        # fetch_all_challenges pulls per-challenge detail (description, solves, files)
        # — fetch_challenge_stubs omits those on CTFd.
        challenges = await client.fetch_all_challenges()
        solved_names = await client.fetch_solved_names()
    except Exception as e:
        try:
            await client.close()
        except Exception:
            pass
        return JSONResponse(
            {"ok": False, "error": f"Failed to fetch challenges: {e}"}, status_code=502
        )

    try:
        existing = (
            (await db.execute(select(Task).where(Task.ctf_id == ctf_id))).scalars().all()
        )
        by_external = {t.external_id: t for t in existing}

        created = 0
        updated = 0
        now = dt.datetime.now(dt.timezone.utc)
        for ch in challenges:
            ext_id = str(ch.get("id") or ch.get("name") or "")
            if not ext_id:
                continue
            name = ch.get("name") or ext_id
            category = ch.get("category") or ""
            points = int(ch.get("value") or 0)
            solves = int(ch.get("solves") or 0)
            platform_desc = _normalize_description(ch.get("description") or "", platform)
            files = ch.get("files") or []
            connection_info = ch.get("connection_info") or ""
            is_solved_upstream = name in solved_names

            t = by_external.get(ext_id)
            if t is None:
                t = Task(
                    ctf_id=ctf_id,
                    external_id=ext_id,
                    name=name,
                    category=category,
                    points=points,
                    solves=solves,
                    platform_description_md=platform_desc,
                    files_json=_json.dumps(files),
                    connection_info=connection_info,
                    status="solved" if is_solved_upstream else "todo",
                    solved_at=now if is_solved_upstream else None,
                    created_at=now,
                    updated_at=now,
                )
                db.add(t)
                created += 1
            else:
                # Never clobber team-owned fields; refresh platform snapshot only.
                t.name = name
                t.category = category
                t.points = points
                t.solves = solves
                t.platform_description_md = platform_desc
                t.files_json = _json.dumps(files)
                t.connection_info = connection_info
                if is_solved_upstream and t.status not in ("solved",):
                    t.status = "solved"
                    t.solved_at = t.solved_at or now
                t.updated_at = now
                updated += 1

        await db.commit()
    finally:
        try:
            await client.close()
        except Exception:
            pass

    return JSONResponse(
        {"ok": True, "created": created, "updated": updated, "total": len(challenges)}
    )


@router.post("/api/team/tasks")
async def api_team_create_task(
    request: Request,
    user: User = Depends(_require_team_member),
    db: AsyncSession = Depends(get_db),
):
    """Manually create a task (for CTFs without a connected platform, or for
    challenges that couldn't be auto-fetched). Uses a ``manual-<uuid>``
    external_id so it never collides with a synced challenge."""
    body = await request.json()
    ctf_id = int(body.get("ctf_id") or 0)
    if not ctf_id:
        return JSONResponse({"ok": False, "error": "ctf_id is required"}, status_code=400)
    ctf = await db.get(CTFModel, ctf_id)
    if not ctf:
        return JSONResponse({"ok": False, "error": "CTF not found"}, status_code=404)

    name = str(body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)

    try:
        points = int(body.get("points") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "points must be int"}, status_code=400)

    status = str(body.get("status") or "todo").strip()
    if status not in STATUS_VALUES:
        status = "todo"

    now = dt.datetime.now(dt.timezone.utc)
    t = Task(
        ctf_id=ctf_id,
        external_id=f"manual-{_uuid.uuid4().hex[:12]}",
        name=name,
        category=str(body.get("category") or "").strip(),
        points=points,
        solves=0,
        platform_description_md=str(body.get("platform_description_md") or "").strip(),
        files_json="[]",
        connection_info=str(body.get("connection_info") or "").strip(),
        status=status,
        solved_at=now if status == "solved" else None,
        created_at=now,
        updated_at=now,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return JSONResponse({"ok": True, "task": _task_to_dict(t)}, status_code=201)


@router.delete("/api/team/tasks/{task_id}")
async def api_team_delete_task(
    task_id: int,
    user: User = Depends(_require_team_member),
    db: AsyncSession = Depends(get_db),
):
    """Delete a task. Only manually-created tasks (external_id starts with
    ``manual-``) can be deleted — synced tasks would just reappear on next sync."""
    t = await db.get(Task, task_id)
    if not t:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    if not (t.external_id or "").startswith("manual-"):
        return JSONResponse(
            {
                "ok": False,
                "error": "synced tasks cannot be deleted — use the Skipped column instead",
            },
            status_code=400,
        )
    await db.delete(t)
    await db.commit()
    return JSONResponse({"ok": True})


@router.get("/api/team/tasks/{task_id}")
async def api_team_get_task(
    task_id: int,
    user: User = Depends(_require_team_member),
    db: AsyncSession = Depends(get_db),
):
    t = await db.get(Task, task_id)
    if not t:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    atts = (
        (
            await db.execute(
                select(TaskAttachment)
                .where(TaskAttachment.task_id == task_id)
                .order_by(TaskAttachment.id)
            )
        )
        .scalars()
        .all()
    )
    return JSONResponse({"ok": True, "task": _task_to_dict(t, atts)})


@router.patch("/api/team/tasks/{task_id}")
async def api_team_patch_task(
    task_id: int,
    request: Request,
    user: User = Depends(_require_team_member),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    t = await db.get(Task, task_id)
    if not t:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)

    now = dt.datetime.now(dt.timezone.utc)
    if "status" in body:
        new_status = str(body["status"] or "").strip()
        if new_status not in STATUS_VALUES:
            return JSONResponse({"ok": False, "error": "invalid status"}, status_code=400)
        t.status = new_status
        if new_status == "solved" and t.solved_at is None:
            t.solved_at = now

    if "assignee_type" in body:
        at = body["assignee_type"]
        if at in (None, ""):
            t.assignee_type = None
            t.assignee_user_id = None
        elif at in ASSIGNEE_TYPES:
            t.assignee_type = at
        else:
            return JSONResponse({"ok": False, "error": "invalid assignee_type"}, status_code=400)
    if "assignee_user_id" in body:
        uid = body["assignee_user_id"]
        if uid is None:
            t.assignee_user_id = None
        else:
            try:
                t.assignee_user_id = int(uid)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"ok": False, "error": "assignee_user_id must be int or null"}, status_code=400
                )

    for field in (
        "description_override_md",
        "notes_md",
        "writeup_md",
        "flag",
    ):
        if field in body:
            setattr(t, field, str(body[field] or ""))

    if "priority" in body:
        try:
            t.priority = int(body["priority"] or 0)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "priority must be int"}, status_code=400)

    t.updated_at = now
    await db.commit()
    await db.refresh(t)

    # Side-effect: when a task is assigned to AI and moved to in_progress, push
    # it onto the running coordinator's priority list (if a run is active).
    if t.assignee_type == "ai" and t.status == "in_progress":
        try:
            from backend.run_manager import get_run_manager

            mgr = get_run_manager()
            if mgr.status().get("running"):
                if t.name not in mgr.priority_challenges:
                    mgr.toggle_priority(t.name)
                if t.name in mgr.stopped_challenges:
                    mgr.stop_challenge(t.name)  # toggles off
        except Exception as e:
            logger.debug("priority hint skipped: %s", e)

    return JSONResponse({"ok": True, "task": _task_to_dict(t)})


@router.post("/api/team/tasks/{task_id}/attachments")
async def api_team_add_attachment(
    task_id: int,
    file: UploadFile,
    kind: str = "file",
    user: User = Depends(_require_team_member),
    db: AsyncSession = Depends(get_db),
):
    t = await db.get(Task, task_id)
    if not t:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    if kind not in ("writeup", "file", "image"):
        return JSONResponse({"ok": False, "error": "invalid kind"}, status_code=400)
    data = await file.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
        return JSONResponse(
            {"ok": False, "error": f"file too large — the limit is {mb} MB"},
            status_code=413,
        )
    att = TaskAttachment(
        task_id=task_id,
        kind=kind,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        size_bytes=len(data),
        data=data,
        uploaded_by_user_id=user.id,
    )
    db.add(att)
    await db.commit()
    await db.refresh(att)
    return JSONResponse(
        {
            "ok": True,
            "attachment": {
                "id": att.id,
                "kind": att.kind,
                "filename": att.filename,
                "content_type": att.content_type,
                "size_bytes": att.size_bytes,
            },
        },
        status_code=201,
    )


@router.get("/api/team/tasks/{task_id}/attachments/{attachment_id}")
async def api_team_get_attachment(
    task_id: int,
    attachment_id: int,
    user: User = Depends(_require_team_member),
    db: AsyncSession = Depends(get_db),
):
    att = await db.get(TaskAttachment, attachment_id)
    if not att or att.task_id != task_id:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return Response(
        content=att.data,
        media_type=att.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{att.filename}"',
        },
    )


@router.delete("/api/team/tasks/{task_id}/attachments/{attachment_id}")
async def api_team_delete_attachment(
    task_id: int,
    attachment_id: int,
    user: User = Depends(_require_team_member),
    db: AsyncSession = Depends(get_db),
):
    att = await db.get(TaskAttachment, attachment_id)
    if not att or att.task_id != task_id:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    await db.delete(att)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/api/team/tasks/{task_id}/generate-writeup")
async def api_team_generate_writeup(
    task_id: int,
    user: User = Depends(_require_team_member),
    db: AsyncSession = Depends(get_db),
):
    """Generate a markdown writeup for *task* from its latest solver log.

    Reads the challenge log from the in-memory event bus (produced by the solver
    swarm during a run) and asks Anthropic for a concise writeup. Falls back to
    a template-based summary if no API key is configured.
    """
    t = await db.get(Task, task_id)
    if not t:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)

    from ui.event_bus import get_bus

    bus = get_bus()
    logs = list(bus.logs.get(t.name, []))
    challenge_state = bus.challenges.get(t.name, {})

    if not logs and not t.flag:
        return JSONResponse(
            {
                "ok": False,
                "error": "no solver log or flag available yet — run the solver first",
            },
            status_code=409,
        )

    writeup = await _generate_writeup_md(
        name=t.name,
        category=t.category,
        points=t.points,
        description=t.description_override_md or t.platform_description_md,
        flag=t.flag or challenge_state.get("flag", ""),
        logs=logs,
    )

    t.writeup_md = writeup
    t.updated_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    return JSONResponse({"ok": True, "writeup_md": writeup})


async def _generate_writeup_md(
    name: str,
    category: str,
    points: int,
    description: str,
    flag: str,
    logs: list[str],
) -> str:
    """Ask Anthropic for a writeup. Falls back to a template if no key is set."""
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    transcript = "\n".join(logs[-400:]) if logs else "(no solver log captured)"

    fallback = (
        f"# {name}\n\n"
        f"**Category:** {category}  **Points:** {points}\n\n"
        f"## Challenge\n\n{description or '_(no description)_'}\n\n"
        f"## Flag\n\n`{flag or 'unknown'}`\n\n"
        f"## Solver trace (abridged)\n\n```\n{transcript[-4000:]}\n```\n"
    )

    if not api_key:
        return fallback

    try:
        import httpx

        prompt = (
            f"You are writing a CTF writeup for the team's internal knowledge base.\n"
            f"Produce clean, concise Markdown. Include sections: Challenge, Approach, "
            f"Key Steps (numbered), Flag, and Lessons. Keep it honest about what worked.\n\n"
            f"Challenge name: {name}\n"
            f"Category: {category}  Points: {points}\n"
            f"Flag: {flag or '(unknown)'}\n\n"
            f"Challenge description:\n{description or '(none)'}\n\n"
            f"Solver trace (most recent first, truncated):\n{transcript[-12000:]}\n"
        )
        async with httpx.AsyncClient(timeout=90) as c:
            resp = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-opus-4-7",
                    "max_tokens": 4000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            parts = data.get("content") or []
            text = "".join(
                p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
            return text or fallback
    except Exception as e:
        logger.warning("Writeup generation via Anthropic failed: %s", e)
        return fallback
