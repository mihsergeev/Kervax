"""server_metrics: разбивка CPU по состояниям + память cache/free

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_COLS = ["cpu_user", "cpu_system", "cpu_iowait", "cpu_irq", "mem_cache", "mem_free"]


def upgrade() -> None:
    for c in _COLS:
        op.add_column("server_metrics", sa.Column(c, sa.Float(), nullable=True))


def downgrade() -> None:
    for c in reversed(_COLS):
        op.drop_column("server_metrics", c)
