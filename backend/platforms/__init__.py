"""CTF platform adapters.

Two implementations are currently supported:

- ``ctfd``: talks to CTFd (the reference platform).
- ``rctf``: talks to rCTF (``/api/v1/challs`` + ``/api/v1/challs/{id}/submit``).

Both adapters expose an identical duck-typed interface (see ``PlatformClient``)
so the rest of the codebase (poller, swarm, solvers) doesn't need to know which
platform it's talking to.
"""

from __future__ import annotations

import json
from typing import Any

from backend.platforms.base import PlatformClient
from backend.platforms.rctf import RCTFClient

SUPPORTED_PLATFORMS = ("ctfd", "rctf", "tfc", "generic", "manual")


def make_platform_client(
    platform: str,
    base_url: str,
    token: str = "",
    username: str = "admin",
    password: str = "admin",
    api_base: str = "/api/v1",
    adapter: dict[str, Any] | str | None = None,
    ctf_id: int = 0,
) -> PlatformClient:
    """Return the appropriate client for *platform* (ctfd | rctf | generic).

    For ``generic``, *adapter* is the config-driven spec (dict or JSON string)
    describing where the list/submit/solved endpoints live. See
    ``backend.platforms.generic`` for the spec shape.
    """
    p = (platform or "ctfd").lower().strip()

    if p == "manual":
        # Solve hand-entered challenges (kanban Tasks); the operator submits
        # flags themselves. ctf_id says which CTF's Tasks to read; fall back to
        # parsing a ``manual://ctf/<id>`` base url (no event loop needed).
        import re as _re

        from backend.platforms.manual import ManualPlatformClient

        cid = ctf_id
        if not cid:
            m = _re.search(r"manual://ctf/(\d+)", base_url or "")
            cid = int(m.group(1)) if m else 0
        return ManualPlatformClient(ctf_id=cid)

    if p == "generic":
        from backend.platforms.generic import GenericHTTPClient

        spec: dict[str, Any] = {}
        if isinstance(adapter, str) and adapter.strip():
            try:
                spec = json.loads(adapter)
            except json.JSONDecodeError:
                spec = {}
        elif isinstance(adapter, dict):
            spec = adapter
        return GenericHTTPClient(base_url=base_url, token=token, adapter=spec)

    if p == "rctf":
        return RCTFClient(base_url=base_url, token=token)

    if p == "tfc":
        # The Few Chosen mints 10-minute JWTs, so it needs the login itself
        # rather than a long-lived token.
        from backend.platforms.tfc import TFCClient

        return TFCClient(
            base_url=base_url or "https://ctf.thefewchosen.com",
            username=username,
            password=password,
            token=token,
        )

    # default to CTFd — importing here avoids a circular-ish coupling.
    from backend.ctfd import CTFdClient

    return CTFdClient(
        base_url=base_url,
        token=token,
        username=username,
        password=password,
        api_base=api_base or "/api/v1",
    )


__all__ = ["PlatformClient", "RCTFClient", "SUPPORTED_PLATFORMS", "make_platform_client"]
