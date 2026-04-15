"""SQLAlchemy ORM models."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )

    credentials: Mapped[list["Credential"]] = relationship(back_populates="user")
    model_prefs: Mapped[list["UserModelPref"]] = relationship(back_populates="user")
    ctfs: Mapped[list["CTF"]] = relationship(back_populates="user")

    # With postponed evaluation of annotations, reference the class name directly.
    # Quoting only the inner name ("UserSettings" | None) evaluates as a string-literal union and
    # breaks SQLAlchemy's annotation parsing.
    settings: Mapped[UserSettings | None] = relationship(back_populates="user", uselist=False)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    # Default CTFd connection for this user
    ctfd_url: Mapped[str] = mapped_column(String(500), default="")
    ctfd_token_enc: Mapped[bytes] = mapped_column(LargeBinary, default=b"")

    # API keys (optional; stored encrypted at rest)
    anthropic_api_key_enc: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    openai_api_key_enc: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    gemini_api_key_enc: Mapped[bytes] = mapped_column(LargeBinary, default=b"")

    # Claude CLI config (non-secret)
    claude_cli_path: Mapped[str] = mapped_column(String(500), default="")
    claude_config_dir: Mapped[str] = mapped_column(String(500), default="")

    # Auto-spawn exclusions (non-secret)
    exclude_challenges: Mapped[str] = mapped_column(Text, default="")
    exclude_challenge_regex: Mapped[str] = mapped_column(String(512), default="")

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )

    user: Mapped[User] = relationship(back_populates="settings")


class Credential(Base):
    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_credentials_user_provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    # Provider examples: anthropic_api_key, openai_api_key, claude_cli, codex_cli, opencode_copilot
    provider: Mapped[str] = mapped_column(String(64))

    # Encrypted secret payload (nonce+ciphertext etc.)
    secret: Mapped[bytes] = mapped_column(LargeBinary)

    # Optional metadata (json string) for non-secret settings.
    meta_json: Mapped[str] = mapped_column(Text, default="{}")

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )

    user: Mapped[User] = relationship(back_populates="credentials")


class UserModelPref(Base):
    __tablename__ = "user_model_prefs"
    __table_args__ = (UniqueConstraint("user_id", "model_spec", name="uq_user_model_spec"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    model_spec: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="model_prefs")


class CTF(Base):
    __tablename__ = "ctfs"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_ctf_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    name: Mapped[str] = mapped_column(String(200))
    ctfd_url: Mapped[str] = mapped_column(String(500))
    ctfd_token_enc: Mapped[bytes] = mapped_column(LargeBinary)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )

    user: Mapped[User] = relationship(back_populates="ctfs")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ctf_id: Mapped[int] = mapped_column(ForeignKey("ctfs.id", ondelete="CASCADE"), index=True)

    status: Mapped[str] = mapped_column(
        String(32), default="created"
    )  # created|running|paused|stopped|done|error
    max_concurrent_challenges: Mapped[int] = mapped_column(Integer, default=10)

    include_names: Mapped[str] = mapped_column(Text, default="")
    exclude_names: Mapped[str] = mapped_column(Text, default="")
    exclude_regex: Mapped[str] = mapped_column(String(512), default="")

    priority_names: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
