"""server_metrics: oom_kill — OOM-киллов за интервал

Revision ID: 0039
Revises: 0038
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("oom_kill", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_metrics", "oom_kill")
