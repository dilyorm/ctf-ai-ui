"""Auth helpers (password hashing + cookie sessions)."""

from __future__ import annotations

import os

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError


_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def cookie_secret() -> str:
    # Reuse UI_SECRET_KEY if set; otherwise require one for production.
    secret = (os.environ.get("UI_SECRET_KEY") or os.environ.get("APP_SECRET_KEY") or "").strip()
    if not secret:
        raise RuntimeError("Set UI_SECRET_KEY (or APP_SECRET_KEY) for session signing")
    return secret
