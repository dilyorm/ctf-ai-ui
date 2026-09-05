"""Add login credentials to ctfs (platforms that issue short-lived tokens)

Revision ID: 0013_ctf_credentials
Revises: 0012_generic_adapter
Create Date: 2026-09-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_ctf_credentials"
down_revision = "0012_generic_adapter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ctfs",
        sa.Column("ctfd_user", sa.String(length=200), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "ctfs",
        sa.Column(
            "ctfd_pass_enc",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ctfs", "ctfd_pass_enc")
    op.drop_column("ctfs", "ctfd_user")
