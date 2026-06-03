"""CTF platform adapters.

Two implementations are currently supported:

- ``ctfd``: talks to CTFd (the reference platform).
- ``rctf``: talks to rCTF (``/api/v1/challs`` + ``/api/v1/challs/{id}/submit``).

Both adapters expose an identical duck-typed interface (see ``PlatformClient``)
so the rest of the codebase (poller, swarm, solvers) doesn't need to know which
platform it's talking to.
"""

from __future__ import annotations

from backend.platforms.base import PlatformClient
from backend.platforms.rctf import RCTFClient

SUPPORTED_PLATFORMS = ("ctfd", "rctf")


def make_platform_client(
    platform: str,
    base_url: str,
    token: str = "",
    username: str = "admin",
    password: str = "admin",
) -> PlatformClient:
    """Return the appropriate client for *platform* (ctfd | rctf)."""
    p = (platform or "ctfd").lower().strip()
    if p == "rctf":
        return RCTFClient(base_url=base_url, token=token)
    # default to CTFd — importing here avoids a circular-ish coupling.
    from backend.ctfd import CTFdClient

    return CTFdClient(base_url=base_url, token=token, username=username, password=password)


__all__ = ["PlatformClient", "RCTFClient", "SUPPORTED_PLATFORMS", "make_platform_client"]
