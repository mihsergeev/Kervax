"""users, audit_log, app_settings

Revision ID: 0001
Revises:
Create Date: 2026-07-10

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("totp_secret", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "totp_last_counter", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "token_version", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("username", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_index("ix_audit_log_ts", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("users")
