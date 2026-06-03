"""Add secret_enc to pooled_accounts (token-based providers, e.g. Copilot)

Revision ID: 0010_pooled_account_secret
Revises: 0009_pooled_accounts
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_pooled_account_secret"
down_revision = "0009_pooled_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pooled_accounts",
        sa.Column("secret_enc", sa.LargeBinary(), nullable=False, server_default=sa.text("''::bytea")),
    )


def downgrade() -> None:
    op.drop_column("pooled_accounts", "secret_enc")
