"""Secret encryption for credentials at rest.

We store per-user secrets encrypted in Postgres using a server-side master key.
This is meant for production-ish single-server deployments.

Implementation: Fernet (AES-128-CBC + HMAC) via `cryptography`.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    raw = (os.environ.get("APP_SECRET_KEY") or "").encode("utf-8")
    if not raw:
        raise RuntimeError(
            "APP_SECRET_KEY is required for credential encryption (set a long random string)"
        )

    # Fernet key must be urlsafe base64-encoded 32 bytes.
    key = hashlib.sha256(raw).digest()
    fernet_key = base64.urlsafe_b64encode(key)
    return Fernet(fernet_key)


def seal_str(value: str) -> bytes:
    return _fernet().encrypt(value.encode("utf-8"))


def seal_opt(value: str | None) -> bytes:
    """Encrypt an optional string; returns b"" for empty/None."""
    if not value:
        return b""
    return seal_str(value)


def open_str(blob: bytes) -> str:
    try:
        return _fernet().decrypt(blob).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("invalid secret") from e


def open_opt(blob: bytes | None) -> str:
    """Decrypt an optional blob; returns "" for empty/None."""
    if not blob:
        return ""
    if blob == b"":
        return ""
    return open_str(blob)
