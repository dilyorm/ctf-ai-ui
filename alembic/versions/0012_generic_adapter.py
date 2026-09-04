"""Add adapter_json to ctfs (generic-platform adapter spec)

Revision ID: 0012_generic_adapter
Revises: 0011_ctf_api_base
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_generic_adapter"
down_revision = "0011_ctf_api_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ctfs",
        sa.Column("adapter_json", sa.Text(), nullable=False, server_default=sa.text("''")),
    )


def downgrade() -> None:
    op.drop_column("ctfs", "adapter_json")
