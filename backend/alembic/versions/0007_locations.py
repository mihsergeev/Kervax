"""locations + location_results + checks.check_locations

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column(
            "check_locations", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_table(
        "location_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("message", sa.String(length=512), nullable=False, server_default=""),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_location_results_check_id", "location_results", ["check_id"])
    op.create_index(
        "ix_location_results_location_id", "location_results", ["location_id"]
    )
    op.create_index(
        "ix_location_results_lookup",
        "location_results",
        ["check_id", "location_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_location_results_lookup", table_name="location_results")
    op.drop_index("ix_location_results_location_id", table_name="location_results")
    op.drop_index("ix_location_results_check_id", table_name="location_results")
    op.drop_table("location_results")
    op.drop_table("locations")
    op.drop_column("checks", "check_locations")
