"""Generic, config-driven CTF platform client.

Most CTF platforms expose a small JSON API: list challenges, submit a flag,
(optionally) list what you've already solved. Rather than hand-writing a new
adapter class per platform, this client is driven by an **adapter spec** — a
plain dict (stored as JSON on the ``CTF`` row) that says where each endpoint is
and how to read its fields.

The spec is intentionally forgiving: every section has sensible defaults so a
CTFd- or rCTF-shaped API works with almost no configuration, and unknown
platforms only need the few fields that differ. The companion
``backend.platforms.probe`` module builds a draft spec by probing a live site
and asks the operator for whatever it cannot infer.

Adapter spec shape (all keys optional unless noted)::

    {
      "auth":   {"mode": "bearer"|"token_header"|"cookie"|"query"|"none",
                 "header": "Authorization", "prefix": "Bearer ",
                 "cookie_name": "session", "query_param": "token"},
      "list":   {"method": "GET", "path": "/api/v1/challenges",     # required
                 "items_path": "data",
                 "fields": {"id": "id", "name": "name", "category": "category",
                            "points": "value", "description": "description",
                            "files": "files", "connection_info": "connection_info",
                            "solves": "solves", "tags": "tags"}},
      "detail": {"method": "GET", "path": "/api/v1/challenges/{id}",
                 "items_path": "data"},                              # optional
      "submit": {"method": "POST", "path": "/api/v1/challenges/attempt",  # required
                 "body_template": {"challenge_id": "{id}", "submission": "{flag}"},
                 "success": {"status_path": "data.status",
                             "correct_values": ["correct"],
                             "already_values": ["already_solved"],
                             "incorrect_values": ["incorrect"],
                             "correct_regex": "", "incorrect_regex": ""}},
      "solved": {"method": "GET", "path": "/api/v1/users/me/solves",
                 "items_path": "data", "name_field": "challenge.name"}
    }
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.ctfd import SubmitResult

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Default field map — matches CTFd, and most rCTF-shaped payloads after aliasing.
DEFAULT_FIELDS: dict[str, str] = {
    "id": "id",
    "name": "name",
    "category": "category",
    "points": "value",
    "description": "description",
    "files": "files",
    "connection_info": "connection_info",
    "solves": "solves",
    "tags": "tags",
}
# Alternate keys tried when the mapped key is missing (keeps specs short).
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "_id"),
    "name": ("name", "title"),
    "category": ("category", "cat"),
    "points": ("value", "points", "score"),
    "description": ("description", "desc", "prompt"),
    "files": ("files", "attachments"),
    "connection_info": ("connection_info", "connectionInfo", "connection"),
    "solves": ("solves", "solveCount", "solve_count"),
    "tags": ("tags", "labels"),
}


def _get_path(obj: Any, path: str) -> Any:
    """Read a dotted path out of nested dicts/lists (``a.b.0.c``). ``""`` -> obj."""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _first_present(item: dict[str, Any], mapped_key: str, canonical: str) -> Any:
    """Return item[mapped_key] if present, else try known aliases for canonical."""
    val = _get_path(item, mapped_key)
    if val is not None:
        return val
    for alias in FIELD_ALIASES.get(canonical, ()):  # pragma: no branch
        if alias == mapped_key:
            continue
        val = _get_path(item, alias)
        if val is not None:
            return val
    return None


def _substitute(template: Any, values: dict[str, str]) -> Any:
    """Recursively replace ``{id}``/``{flag}``/``{name}`` tokens in a body template."""
    if isinstance(template, str):
        out = template
        for k, v in values.items():
            out = out.replace("{" + k + "}", v)
        return out
    if isinstance(template, dict):
        return {k: _substitute(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [_substitute(v, values) for v in template]
    return template


@dataclass
class GenericHTTPClient:
    """Config-driven :class:`~backend.platforms.base.PlatformClient`.

    ``adapter`` is the spec dict documented at the top of this module. An
    ``httpx.AsyncClient`` may be injected for testing; otherwise one is lazily
    created against ``base_url``.
    """

    base_url: str = ""
    token: str = ""
    adapter: dict[str, Any] = field(default_factory=dict)
    # accepted for duck-typing parity with CTFdClient; unused
    username: str = ""
    password: str = ""

    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _challenge_ids: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ client
    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                follow_redirects=True,
                verify=False,  # CTF services routinely use self-signed certs
                timeout=30.0,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    def _auth(self) -> dict[str, Any]:
        return self.adapter.get("auth") or {}

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        auth = self._auth()
        mode = (auth.get("mode") or ("bearer" if self.token else "none")).lower()
        if not self.token or mode in ("none", "cookie", "query"):
            return h
        header = auth.get("header") or "Authorization"
        if mode == "bearer":
            prefix = auth.get("prefix", "Bearer ")
        elif mode == "token_header":
            prefix = auth.get("prefix", "")
        else:
            prefix = auth.get("prefix", "")
        h[header] = f"{prefix}{self.token}"
        return h

    def _cookies(self) -> dict[str, str]:
        auth = self._auth()
        if (auth.get("mode") or "").lower() == "cookie" and self.token:
            return {auth.get("cookie_name") or "session": self.token}
        return {}

    def _query_auth(self) -> dict[str, str]:
        auth = self._auth()
        if (auth.get("mode") or "").lower() == "query" and self.token:
            return {auth.get("query_param") or "token": self.token}
        return {}

    async def _request(self, method: str, path: str, *, json_body: Any = None) -> httpx.Response:
        client = await self._ensure_client()
        return await client.request(
            method.upper(),
            path,
            json=json_body,
            headers=self._headers(),
            cookies=self._cookies() or None,
            params=self._query_auth() or None,
        )

    # -------------------------------------------------------------- normalize
    def _normalize(self, item: dict[str, Any]) -> dict[str, Any]:
        fields = {**DEFAULT_FIELDS, **(self.adapter.get("list", {}).get("fields") or {})}
        files_raw = _first_present(item, fields["files"], "files") or []
        files: list[str] = []
        for f in files_raw if isinstance(files_raw, list) else []:
            if isinstance(f, str):
                files.append(f)
            elif isinstance(f, dict):
                url = f.get("url") or f.get("path") or f.get("href") or ""
                if url:
                    files.append(url)
        tags_raw = _first_present(item, fields["tags"], "tags") or []
        tags = [t.get("value") if isinstance(t, dict) else t for t in tags_raw] if isinstance(tags_raw, list) else []
        try:
            points = int(_first_present(item, fields["points"], "points") or 0)
        except (TypeError, ValueError):
            points = 0
        try:
            solves = int(_first_present(item, fields["solves"], "solves") or 0)
        except (TypeError, ValueError):
            solves = 0
        return {
            "id": _first_present(item, fields["id"], "id"),
            "name": _first_present(item, fields["name"], "name") or "",
            "category": _first_present(item, fields["category"], "category") or "",
            "value": points,
            "description": _first_present(item, fields["description"], "description") or "",
            "files": files,
            "connection_info": _first_present(item, fields["connection_info"], "connection_info") or "",
            "solves": solves,
            "tags": tags,
            "type": item.get("type") or "standard",
            "hints": item.get("hints") or [],
        }

    def _list_items(self, data: Any) -> list[dict[str, Any]]:
        items_path = (self.adapter.get("list") or {}).get("items_path", "data")
        raw = _get_path(data, items_path) if items_path else data
        if raw is None and isinstance(data, list):
            raw = data
        return [x for x in (raw or []) if isinstance(x, dict)]

    # --------------------------------------------------------- PlatformClient
    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        spec = self.adapter.get("list") or {}
        if not spec.get("path"):
            raise RuntimeError("generic adapter: list.path is required")
        resp = await self._request(spec.get("method", "GET"), spec["path"])
        resp.raise_for_status()
        result: list[dict[str, Any]] = []
        for item in self._list_items(resp.json()):
            norm = self._normalize(item)
            if norm["name"]:
                self._challenge_ids[norm["name"]] = norm["id"]
                result.append(norm)
        return result

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        stubs = await self.fetch_challenge_stubs()
        detail = self.adapter.get("detail") or {}
        if not detail.get("path"):
            return stubs
        full: list[dict[str, Any]] = []
        for stub in stubs:
            try:
                path = _substitute(detail["path"], {"id": str(stub["id"]), "name": stub["name"]})
                resp = await self._request(detail.get("method", "GET"), path)
                resp.raise_for_status()
                body = resp.json()
                inner = _get_path(body, detail.get("items_path", "data")) if detail.get("items_path", "data") else body
                full.append(self._normalize(inner) if isinstance(inner, dict) else stub)
            except Exception:
                logger.warning("generic detail fetch failed for %s", stub["name"], exc_info=True)
                full.append(stub)
        return full

    async def fetch_solved_names(self) -> set[str]:
        spec = self.adapter.get("solved") or {}
        if not spec.get("path"):
            return set()
        try:
            resp = await self._request(spec.get("method", "GET"), spec["path"])
            resp.raise_for_status()
            body = resp.json()
            items = self._list_items(body) if not spec.get("items_path") else (
                _get_path(body, spec["items_path"]) or []
            )
            name_field = spec.get("name_field", "name")
            names: set[str] = set()
            for it in items:
                if isinstance(it, dict):
                    nm = _get_path(it, name_field)
                    if nm:
                        names.add(str(nm))
                elif isinstance(it, str):
                    names.add(it)
            return names
        except Exception:
            logger.warning("generic solved fetch failed", exc_info=True)
            return set()

    async def get_challenge_id(self, name: str) -> Any:
        if name in self._challenge_ids:
            return self._challenge_ids[name]
        await self.fetch_challenge_stubs()
        if name not in self._challenge_ids:
            raise RuntimeError(f'Challenge "{name}" not found on platform')
        return self._challenge_ids[name]

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        spec = self.adapter.get("submit") or {}
        if not spec.get("path"):
            raise RuntimeError("generic adapter: submit.path is required")
        cid = await self.get_challenge_id(challenge_name)
        values = {"id": str(cid), "flag": flag, "name": challenge_name}
        path = _substitute(spec["path"], values)
        body_template = spec.get("body_template", {"flag": "{flag}"})
        body = _substitute(body_template, values)
        resp = await self._request(spec.get("method", "POST"), path, json_body=body)
        return self._evaluate_submit(resp, flag)

    def _evaluate_submit(self, resp: httpx.Response, flag: str) -> SubmitResult:
        success = (self.adapter.get("submit") or {}).get("success") or {}
        try:
            data = resp.json()
        except Exception:
            data = None
        text = resp.text or ""

        status: str | None = None
        if data is not None and success.get("status_path"):
            raw = _get_path(data, success["status_path"])
            status = str(raw).lower() if raw is not None else None

        correct = {str(v).lower() for v in success.get("correct_values", ["correct", "goodflag", "solved", "true"])}
        already = {str(v).lower() for v in success.get("already_values", ["already_solved", "alreadysolvedchallenge", "already"])}
        incorrect = {str(v).lower() for v in success.get("incorrect_values", ["incorrect", "badflag", "wrong", "false"])}

        if status is not None:
            if status in correct:
                return SubmitResult("correct", text[:200], f'CORRECT — "{flag}" accepted.')
            if status in already:
                return SubmitResult("already_solved", text[:200], f'ALREADY SOLVED — "{flag}" accepted.')
            if status in incorrect:
                return SubmitResult("incorrect", text[:200], f'INCORRECT — "{flag}" rejected.')

        # Regex fallbacks on the raw body
        if success.get("correct_regex") and re.search(success["correct_regex"], text, re.I):
            return SubmitResult("correct", text[:200], f'CORRECT — "{flag}" accepted.')
        if success.get("incorrect_regex") and re.search(success["incorrect_regex"], text, re.I):
            return SubmitResult("incorrect", text[:200], f'INCORRECT — "{flag}" rejected.')

        # Last-resort heuristic on common substrings
        low = text.lower()
        if any(w in low for w in ("already", "alreadysolved")):
            return SubmitResult("already_solved", text[:200], f'ALREADY SOLVED — "{flag}".')
        if any(w in low for w in ("correct", "goodflag", '"solved":true', "well done", "congrat")):
            return SubmitResult("correct", text[:200], f'CORRECT — "{flag}" accepted.')
        if any(w in low for w in ("incorrect", "badflag", "wrong", "nope", "try again")):
            return SubmitResult("incorrect", text[:200], f'INCORRECT — "{flag}" rejected.')
        return SubmitResult("unknown", text[:200], f"Unknown submit response (HTTP {resp.status_code}).")

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        from pathlib import Path
        from urllib.parse import urlparse

        import yaml
        from markdownify import markdownify as html2md

        name = challenge.get("name", f"challenge-{challenge.get('id')}")
        slug = re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "", name.lower().strip())
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-") or "challenge"
        ch_dir = Path(output_dir) / slug
        ch_dir.mkdir(parents=True, exist_ok=True)

        client = await self._ensure_client()
        for raw_url in challenge.get("files") or []:
            dist_dir = ch_dir / "distfiles"
            dist_dir.mkdir(exist_ok=True)
            url = raw_url if raw_url.startswith("http") else f"{self.base_url.rstrip('/')}/{raw_url.lstrip('/')}"
            fname = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "file"
            dest = dist_dir / fname
            if dest.exists():
                continue
            try:
                same_host = urlparse(url).hostname == urlparse(self.base_url).hostname
                resp = await client.get(
                    url,
                    headers=self._headers() if same_host else {},
                    cookies=self._cookies() if same_host else None,
                    follow_redirects=True,
                    timeout=60.0,
                )
                resp.raise_for_status()
                dest.write_bytes(resp.content)
            except Exception as e:
                logger.warning("Failed to download %s: %s", url, e)

        desc = challenge.get("description") or ""
        try:
            desc = html2md(desc, heading_style="atx", escape_asterisks=False)
        except Exception:
            pass
        meta = {
            "name": name,
            "category": challenge.get("category", ""),
            "description": desc.strip(),
            "value": challenge.get("value", 0),
            "connection_info": challenge.get("connection_info") or "",
            "tags": challenge.get("tags") or [],
            "solves": challenge.get("solves", 0),
        }
        (ch_dir / "metadata.yml").write_text(
            yaml.dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False)
        )
        return str(ch_dir)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()


# --------------------------------------------------------------- spec presets
def ctfd_adapter(api_base: str = "/api/v1") -> dict[str, Any]:
    """A ready adapter spec matching stock CTFd (token auth)."""
    api_base = (api_base or "/api/v1").rstrip("/")
    return {
        "auth": {"mode": "token_header", "header": "Authorization", "prefix": "Token "},
        "list": {"method": "GET", "path": f"{api_base}/challenges?per_page=500", "items_path": "data"},
        "detail": {"method": "GET", "path": f"{api_base}/challenges/{{id}}", "items_path": "data"},
        "submit": {
            "method": "POST",
            "path": f"{api_base}/challenges/attempt",
            "body_template": {"challenge_id": "{id}", "submission": "{flag}"},
            "success": {"status_path": "data.status", "correct_values": ["correct"],
                        "already_values": ["already_solved"], "incorrect_values": ["incorrect"]},
        },
    }


def rctf_adapter() -> dict[str, Any]:
    """A ready adapter spec matching rCTF (bearer token auth)."""
    return {
        "auth": {"mode": "bearer", "header": "Authorization", "prefix": "Bearer "},
        "list": {"method": "GET", "path": "/api/v1/challs", "items_path": "data",
                 "fields": {"points": "points"}},
        "submit": {
            "method": "POST",
            "path": "/api/v1/challs/{id}/submit",
            "body_template": {"flag": "{flag}"},
            "success": {"status_path": "kind", "correct_values": ["goodflag"],
                        "already_values": ["alreadysolvedchallenge"],
                        "incorrect_values": ["badflag", "badratelimit", "badchallenge"]},
        },
        "solved": {"method": "GET", "path": "/api/v1/users/me", "items_path": "data.solves",
                   "name_field": "name"},
    }
