"""Add pooled_accounts table (team-wide shared subscription pool)

Revision ID: 0004_pooled_accounts
Revises: 0003_codex_subscription
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_pooled_accounts"
down_revision = "0003_codex_subscription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pooled_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("config_dir", sa.String(length=500), nullable=False),
        sa.Column("max_concurrent", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "disabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("config_dir", name="uq_pooled_accounts_config_dir"),
    )
    op.create_index("ix_pooled_accounts_provider", "pooled_accounts", ["provider"])
    op.create_index("ix_pooled_accounts_owner_user_id", "pooled_accounts", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_pooled_accounts_owner_user_id", table_name="pooled_accounts")
    op.drop_index("ix_pooled_accounts_provider", table_name="pooled_accounts")
    op.drop_table("pooled_accounts")
