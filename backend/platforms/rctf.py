"""rCTF platform client.

rCTF exposes a simple JSON API under ``/api/v1``. Relevant endpoints:

- ``GET /api/v1/challs`` — lists visible challenges (id, name, category, points, files, description)
- ``POST /api/v1/challs/{id}/submit`` — submits a flag (body: ``{flag}``)
- ``GET /api/v1/users/me`` — current user (team) info, including ``solves``

Auth is a bearer token (the team's login token, copied from the rCTF profile).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.ctfd import SubmitResult

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36"


@dataclass
class RCTFClient:
    """rCTF platform client — duck-typed to match ``CTFdClient``."""

    base_url: str = "http://localhost:8080"
    token: str = ""
    # accepted for duck-typing with CTFdClient; unused
    username: str = ""
    password: str = ""

    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _challenge_ids: dict[str, str] = field(default_factory=dict)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                follow_redirects=True,
                verify=False,
                timeout=30.0,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _get(self, path: str) -> Any:
        client = await self._ensure_client()
        resp = await client.get(f"/api/v1{path}", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        client = await self._ensure_client()
        resp = await client.post(f"/api/v1{path}", json=body, headers=self._headers())
        # rCTF returns 200 even for "wrong flag" style responses; don't raise on 4xx
        # that carry a JSON body we want to inspect.
        if resp.status_code >= 500:
            resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"kind": "unknown", "message": resp.text}

    @staticmethod
    def _normalize_challenge(ch: dict[str, Any]) -> dict[str, Any]:
        """Map rCTF challenge shape onto CTFd-like dict."""
        files = ch.get("files") or []
        norm_files: list[str] = []
        for f in files:
            if isinstance(f, dict):
                url = f.get("url") or f.get("path") or ""
                if url:
                    norm_files.append(url)
            elif isinstance(f, str):
                norm_files.append(f)
        return {
            "id": ch.get("id") or ch.get("_id") or ch.get("name"),
            "name": ch.get("name") or "",
            "category": ch.get("category") or "",
            "value": int(ch.get("points") or ch.get("value") or 0),
            "description": ch.get("description") or "",
            "files": norm_files,
            "tags": ch.get("tags") or [],
            "connection_info": ch.get("connectionInfo") or ch.get("connection_info") or "",
            "solves": int(ch.get("solves") or 0),
            "type": ch.get("type") or "standard",
            "hints": ch.get("hints") or [],
        }

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        data = await self._get("/challs")
        # rCTF returns either a bare list or { data: [...] } depending on version.
        items = data if isinstance(data, list) else (data.get("data") or [])
        result: list[dict[str, Any]] = []
        for ch in items:
            norm = self._normalize_challenge(ch)
            if norm["name"]:
                self._challenge_ids[norm["name"]] = norm["id"]
            result.append(norm)
        return result

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        # rCTF returns the full challenge body in one call, so this is the same.
        return await self.fetch_challenge_stubs()

    async def fetch_solved_names(self) -> set[str]:
        try:
            me = await self._get("/users/me")
            data = me.get("data") if isinstance(me, dict) else None
            payload = data if isinstance(data, dict) else me
            solves = payload.get("solves") or []
            names: set[str] = set()
            for s in solves:
                if isinstance(s, dict):
                    name = s.get("name") or s.get("challenge") or s.get("challengeName")
                    if name:
                        names.add(name)
            return names
        except Exception:
            logger.warning("rCTF: could not fetch solved challenges", exc_info=True)
            return set()

    async def get_challenge_id(self, name: str) -> str:
        if name in self._challenge_ids:
            return self._challenge_ids[name]
        # re-populate
        await self.fetch_challenge_stubs()
        if name not in self._challenge_ids:
            raise RuntimeError(f'Challenge "{name}" not found in rCTF')
        return self._challenge_ids[name]

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        challenge_id = await self.get_challenge_id(challenge_name)
        resp = await self._post(f"/challs/{challenge_id}/submit", {"flag": flag})
        # rCTF responses look like { kind: "goodFlag"|"badFlag"|"badToken"|"badChallenge"|"alreadySolvedChallenge"|"badRateLimit", message: "..." }
        kind = (resp.get("kind") if isinstance(resp, dict) else "") or "unknown"
        message = (resp.get("message") if isinstance(resp, dict) else "") or ""
        status_map = {
            "goodFlag": "correct",
            "badFlag": "incorrect",
            "alreadySolvedChallenge": "already_solved",
            "badRateLimit": "incorrect",
            "badChallenge": "incorrect",
        }
        status = status_map.get(kind, "unknown")
        display_map = {
            "correct": f'CORRECT — "{flag}" accepted. {message}',
            "incorrect": f'INCORRECT — "{flag}" rejected. {message}',
            "already_solved": f'ALREADY SOLVED — "{flag}" previously accepted. {message}',
            "unknown": f'Unknown response: kind={kind} message={message}',
        }
        return SubmitResult(status, message, display_map[status].strip())

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        """Download rCTF distfiles and write metadata.yml — mirrors CTFdClient."""
        from pathlib import Path
        from urllib.parse import urlparse

        import yaml
        from markdownify import markdownify as html2md

        name = challenge.get("name", f"rctf-{challenge.get('id')}")
        slug = re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "", name.lower().strip())
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-") or "challenge"

        ch_dir = Path(output_dir) / slug
        ch_dir.mkdir(parents=True, exist_ok=True)

        client = await self._ensure_client()
        for raw_url in challenge.get("files") or []:
            dist_dir = ch_dir / "distfiles"
            dist_dir.mkdir(exist_ok=True)
            url = (
                raw_url
                if raw_url.startswith("http")
                else f"{self.base_url.rstrip('/')}/{raw_url.lstrip('/')}"
            )
            url_path = urlparse(url).path
            fname = url_path.rstrip("/").rsplit("/", 1)[-1] or "file"
            dest = dist_dir / fname
            if dest.exists():
                continue
            try:
                resp = await client.get(url, follow_redirects=True, timeout=60.0)
                resp.raise_for_status()
                dest.write_bytes(resp.content)
                logger.info("Downloaded %s (%d bytes)", fname, len(resp.content))
            except Exception as e:
                logger.warning("Failed to download %s: %s", url, e)

        desc = challenge.get("description") or ""
        try:
            desc = html2md(desc, heading_style="atx", escape_asterisks=False)
        except Exception:
            pass

        tags = [t["value"] if isinstance(t, dict) else str(t) for t in (challenge.get("tags") or [])]
        meta = {
            "name": name,
            "category": challenge.get("category", ""),
            "description": desc.strip(),
            "value": challenge.get("value", 0),
            "connection_info": challenge.get("connection_info") or "",
            "tags": tags,
            "solves": challenge.get("solves", 0),
        }
        (ch_dir / "metadata.yml").write_text(
            yaml.dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False)
        )
        return str(ch_dir)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
