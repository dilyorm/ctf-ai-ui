"""Add api_base to ctfs (CTFd API mount point, e.g. /public-api for SAS CTF)

Revision ID: 0011_ctf_api_base
Revises: 0010_pooled_account_secret
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_ctf_api_base"
down_revision = "0010_pooled_account_secret"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ctfs",
        sa.Column(
            "api_base",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'/api/v1'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ctfs", "api_base")
