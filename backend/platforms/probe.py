"""Probe an unknown CTF platform and draft a generic adapter spec.

The connector flow: give this module a site URL, credentials, and a free-text
note. It detects CTFd / rCTF outright, or for an unknown platform discovers what
it can and returns a list of :class:`ProbeQuestion` s for the operator to answer
(the "agent asks for the missing detail" step). Once an adapter is drafted it is
validated with a harmless wrong-flag submission before being trusted.

Everything is deterministic and offline-testable: pass an ``httpx.AsyncClient``
built on a mock transport and no network is touched.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from backend.platforms.generic import (
    GenericHTTPClient,
    ctfd_adapter,
    rctf_adapter,
)

logger = logging.getLogger(__name__)

# Endpoints worth trying when auto-discovering an unknown platform.
LIST_CANDIDATES: tuple[str, ...] = (
    "/api/v1/challenges",
    "/api/challenges",
    "/api/v1/challs",
    "/api/challs",
    "/challenges.json",
    "/api/challenges.json",
)
WRONG_FLAG = "flagrunner{probe_validation_not_a_real_flag}"


@dataclass
class ProbeQuestion:
    """A single thing the connector needs the operator to confirm."""

    id: str
    prompt: str
    kind: str = "text"  # "text" | "choice"
    applies_to: str = ""  # auth | list | submit | solved
    options: list[str] = field(default_factory=list)
    suggestion: str = ""


@dataclass
class ProbeResult:
    kind: str  # "ctfd" | "rctf" | "generic" | "unknown"
    confidence: float
    adapter: dict[str, Any] | None
    questions: list[ProbeQuestion]
    log: list[str]
    validated: bool = False
    validation: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["questions"] = [asdict(q) for q in self.questions]
        return d


def _looks_like_challenge(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    keys = {k.lower() for k in obj}
    has_name = bool(keys & {"name", "title"})
    has_meta = bool(keys & {"id", "_id", "category", "cat", "value", "points", "score"})
    return has_name and has_meta


def _extract_array(data: Any) -> tuple[list[dict], str]:
    """Find the challenge array in a JSON body. Returns (items, items_path)."""
    if isinstance(data, list):
        return ([x for x in data if isinstance(x, dict)], "")
    if isinstance(data, dict):
        for path in ("data", "challenges", "results", "data.challenges"):
            cur: Any = data
            for part in path.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if isinstance(cur, list) and cur and all(isinstance(x, dict) for x in cur):
                return (cur, path)
    return ([], "")


class _Prober:
    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = client
        self.log: list[str] = []

    def _note(self, msg: str) -> None:
        self.log.append(msg)
        logger.info("probe %s: %s", self.base_url, msg)

    async def _get(self, path: str, headers: dict[str, str] | None = None) -> httpx.Response | None:
        try:
            return await self.client.get(path, headers=headers or {}, timeout=20.0)
        except Exception as e:  # network / DNS / TLS
            self._note(f"GET {path} failed: {e}")
            return None

    async def detect_ctfd(self) -> ProbeResult | None:
        for api_base in ("/api/v1", "/public-api"):
            headers = {"Authorization": f"Token {self.token}"} if self.token else {}
            resp = await self._get(f"{api_base}/challenges?per_page=5", headers=headers)
            if resp is None:
                continue
            if resp.status_code in (200, 403) and "application/json" in resp.headers.get("content-type", ""):
                try:
                    body = resp.json()
                except Exception:
                    continue
                items, _ = _extract_array(body)
                # CTFd returns {"success": true, "data": [...]}; even an empty
                # data list with success is a strong signal.
                if isinstance(body, dict) and "data" in body:
                    self._note(f"CTFd API detected at {api_base} (HTTP {resp.status_code})")
                    return ProbeResult("ctfd", 0.95, ctfd_adapter(api_base), [], self.log)
                if items and all(_looks_like_challenge(x) for x in items[:3]):
                    self._note(f"CTFd-shaped list at {api_base}")
                    return ProbeResult("ctfd", 0.8, ctfd_adapter(api_base), [], self.log)
        return None

    async def detect_rctf(self) -> ProbeResult | None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = await self._get("/api/v1/challs", headers=headers)
        if resp is None or resp.status_code >= 400:
            return None
        try:
            body = resp.json()
        except Exception:
            return None
        items, _ = _extract_array(body)
        if isinstance(body, dict) and body.get("kind") == "goodChallenges":
            self._note("rCTF API detected (kind=goodChallenges)")
            return ProbeResult("rctf", 0.95, rctf_adapter(), [], self.log)
        if items and all(_looks_like_challenge(x) for x in items[:3]):
            self._note("rCTF-shaped list at /api/v1/challs")
            return ProbeResult("rctf", 0.7, rctf_adapter(), [], self.log)
        return None

    async def discover_generic(self) -> ProbeResult:
        """Try to locate a challenge list; ask the operator for the rest."""
        for path in LIST_CANDIDATES:
            for headers in self._auth_variants():
                resp = await self._get(path, headers=headers)
                if resp is None or resp.status_code >= 400:
                    continue
                if "application/json" not in resp.headers.get("content-type", ""):
                    continue
                try:
                    body = resp.json()
                except Exception:
                    continue
                items, items_path = _extract_array(body)
                if items and all(_looks_like_challenge(x) for x in items[:3]):
                    self._note(f"Found a challenge list at {path} (items_path={items_path or 'root'})")
                    adapter = self._draft_adapter(path, items_path, headers, items[0])
                    return ProbeResult(
                        "generic",
                        0.5,
                        adapter,
                        self._submit_questions(),
                        self.log,
                    )
        self._note("Could not auto-locate a challenge list.")
        return ProbeResult("unknown", 0.0, None, self._full_questions(), self.log)

    def _auth_variants(self) -> list[dict[str, str]]:
        if not self.token:
            return [{}]
        return [
            {"Authorization": f"Bearer {self.token}"},
            {"Authorization": f"Token {self.token}"},
            {"Authorization": self.token},
            {"X-API-Key": self.token},
            {},
        ]

    def _draft_adapter(
        self, list_path: str, items_path: str, headers: dict[str, str], sample: dict
    ) -> dict[str, Any]:
        # Infer the auth mode from whichever header variant worked.
        auth: dict[str, Any] = {"mode": "none"}
        if headers.get("Authorization", "").startswith("Bearer "):
            auth = {"mode": "bearer", "header": "Authorization", "prefix": "Bearer "}
        elif headers.get("Authorization", "").startswith("Token "):
            auth = {"mode": "token_header", "header": "Authorization", "prefix": "Token "}
        elif "Authorization" in headers:
            auth = {"mode": "token_header", "header": "Authorization", "prefix": ""}
        elif "X-API-Key" in headers:
            auth = {"mode": "token_header", "header": "X-API-Key", "prefix": ""}

        keys = {k.lower(): k for k in sample}
        fields: dict[str, str] = {}
        if "title" in keys and "name" not in keys:
            fields["name"] = keys["title"]
        for cand in ("points", "score"):
            if cand in keys:
                fields["points"] = keys[cand]
                break
        return {
            "auth": auth,
            "list": {"method": "GET", "path": list_path, "items_path": items_path, **({"fields": fields} if fields else {})},
            # submit intentionally left blank — the operator must confirm it.
            "submit": {"method": "POST", "path": "", "body_template": {"flag": "{flag}"}, "success": {}},
        }

    def _submit_questions(self) -> list[ProbeQuestion]:
        return [
            ProbeQuestion(
                "submit.path",
                "What is the flag-submission endpoint? Use {id} or {name} for the challenge "
                "(e.g. /api/v1/challenges/{id}/attempt).",
                applies_to="submit",
                suggestion="/api/v1/challenges/{id}/attempt",
            ),
            ProbeQuestion(
                "submit.body",
                "What JSON body does submission expect? Use {flag} and {id} "
                '(e.g. {"challenge_id":"{id}","submission":"{flag}"}).',
                applies_to="submit",
                suggestion='{"submission":"{flag}"}',
            ),
            ProbeQuestion(
                "submit.success",
                "How do you tell a correct flag from a wrong one? A JSON field + value "
                '(e.g. data.status == "correct") or leave blank and I will guess from the response text.',
                applies_to="submit",
                suggestion="data.status == correct",
            ),
        ]

    def _full_questions(self) -> list[ProbeQuestion]:
        return [
            ProbeQuestion(
                "list.path",
                "What endpoint lists the challenges? (e.g. /api/v1/challenges)",
                applies_to="list",
                suggestion="/api/v1/challenges",
            ),
            ProbeQuestion(
                "auth.mode",
                "How is the API authenticated?",
                kind="choice",
                applies_to="auth",
                options=["bearer", "token_header", "cookie", "query", "none"],
                suggestion="bearer",
            ),
            *self._submit_questions(),
        ]


async def probe_platform(
    base_url: str,
    token: str = "",
    context: str = "",
    platform_hint: str = "auto",
    client: httpx.AsyncClient | None = None,
) -> ProbeResult:
    """Probe *base_url* and return a draft adapter plus any operator questions."""
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            follow_redirects=True,
            verify=False,
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (Flagrunner connector)"},
        )
    prober = _Prober(base_url, token, client)
    try:
        hint = (platform_hint or "auto").lower()
        if hint == "ctfd":
            return ProbeResult("ctfd", 1.0, ctfd_adapter(), [], ["Forced CTFd adapter."])
        if hint == "rctf":
            return ProbeResult("rctf", 1.0, rctf_adapter(), [], ["Forced rCTF adapter."])

        if hint in ("auto", "ctfd"):
            res = await prober.detect_ctfd()
            if res:
                return res
        if hint in ("auto", "rctf"):
            res = await prober.detect_rctf()
            if res:
                return res
        return await prober.discover_generic()
    finally:
        if owns_client:
            await client.aclose()


async def validate_adapter(
    base_url: str,
    token: str,
    adapter: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> tuple[bool, str]:
    """Sanity-check an adapter: can we list challenges and submit a wrong flag?

    Submits an obviously-fake flag to the first challenge and expects an
    ``incorrect`` (or at least non-``correct``) verdict, mirroring how a real
    solve would be checked without touching a genuine flag.
    """
    gc = GenericHTTPClient(base_url=base_url, token=token, adapter=adapter, _client=client)
    try:
        stubs = await gc.fetch_challenge_stubs()
    except Exception as e:
        return False, f"Could not list challenges: {e}"
    if not stubs:
        return False, "Challenge list came back empty (auth or endpoint may be wrong)."

    if not (adapter.get("submit") or {}).get("path"):
        return True, f"Listed {len(stubs)} challenges. Submission endpoint still needs to be set."

    try:
        result = await gc.submit_flag(stubs[0]["name"], WRONG_FLAG)
    except Exception as e:
        return False, f"Listed {len(stubs)} challenges, but a test submission failed: {e}"
    if result.status in ("incorrect", "unknown"):
        return True, f"Listed {len(stubs)} challenges; a wrong flag was correctly rejected ({result.status})."
    if result.status == "correct":
        return False, "A deliberately-wrong flag was accepted as correct — the success matcher is misconfigured."
    return True, f"Listed {len(stubs)} challenges; submission returned '{result.status}'."
