"""Common platform-client interface (duck-typed).

Both ``CTFdClient`` and ``RCTFClient`` implement the methods below, which lets
the poller, coordinator, and solver code treat them interchangeably.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from backend.ctfd import SubmitResult


@runtime_checkable
class PlatformClient(Protocol):
    base_url: str
    token: str

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]: ...
    async def fetch_all_challenges(self) -> list[dict[str, Any]]: ...
    async def fetch_solved_names(self) -> set[str]: ...
    async def get_challenge_id(self, name: str) -> int | str: ...
    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult: ...
    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str: ...
    async def close(self) -> None: ...


__all__ = ["PlatformClient", "SubmitResult"]
