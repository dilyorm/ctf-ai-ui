"""init

Revision ID: 0001_init
Revises:
Create Date: 2026-04-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=False),
        sa.Column("meta_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
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
        sa.UniqueConstraint("user_id", "provider", name="uq_credentials_user_provider"),
    )
    op.create_index("ix_credentials_user_id", "credentials", ["user_id"], unique=False)

    op.create_table(
        "user_model_prefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("model_spec", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("user_id", "model_spec", name="uq_user_model_spec"),
    )
    op.create_index("ix_user_model_prefs_user_id", "user_model_prefs", ["user_id"], unique=False)

    op.create_table(
        "ctfs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("ctfd_url", sa.String(length=500), nullable=False),
        sa.Column("ctfd_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", "name", name="uq_ctf_user_name"),
    )
    op.create_index("ix_ctfs_user_id", "ctfs", ["user_id"], unique=False)

    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "ctf_id", sa.Integer(), sa.ForeignKey("ctfs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default=sa.text("'created'")
        ),
        sa.Column(
            "max_concurrent_challenges", sa.Integer(), nullable=False, server_default=sa.text("10")
        ),
        sa.Column("include_names", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("exclude_names", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "exclude_regex", sa.String(length=512), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("priority_names", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_runs_user_id", "runs", ["user_id"], unique=False)
    op.create_index("ix_runs_ctf_id", "runs", ["ctf_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_ctf_id", table_name="runs")
    op.drop_index("ix_runs_user_id", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_ctfs_user_id", table_name="ctfs")
    op.drop_table("ctfs")
    op.drop_index("ix_user_model_prefs_user_id", table_name="user_model_prefs")
    op.drop_table("user_model_prefs")
    op.drop_index("ix_credentials_user_id", table_name="credentials")
    op.drop_table("credentials")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
