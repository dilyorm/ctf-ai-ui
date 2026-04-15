"""user settings

Revision ID: 0002_user_settings
Revises: 0001_init
Create Date: 2026-04-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_user_settings"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_settings",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ctfd_url", sa.String(length=500), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "ctfd_token_enc",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("'\\x'"),
        ),
        sa.Column(
            "anthropic_api_key_enc",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("'\\x'"),
        ),
        sa.Column(
            "openai_api_key_enc",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("'\\x'"),
        ),
        sa.Column(
            "gemini_api_key_enc",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("'\\x'"),
        ),
        sa.Column(
            "claude_cli_path",
            sa.String(length=500),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "claude_config_dir",
            sa.String(length=500),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "exclude_challenges",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "exclude_challenge_regex",
            sa.String(length=512),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("user_settings")
