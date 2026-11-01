"""Durable fresh verification requests from the home page."""
import sqlalchemy as sa
from alembic import op

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_rechecks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("active_key", sa.String(100), nullable=True, unique=True),
        sa.Column("object_type", sa.String(8), nullable=False),
        sa.Column("object_id", sa.Integer(), nullable=False, index=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("section", sa.String(16), nullable=False),
        sa.Column("requested_by", sa.String(64), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True)),
        sa.Column("done_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("message", sa.String(512), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("alert_rechecks")
