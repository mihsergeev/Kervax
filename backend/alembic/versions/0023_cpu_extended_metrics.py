"""server_metrics: per-core/частота/температура/троттлинг + порог темп. на сервере

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("cpu_cores_pct", sa.JSON(), nullable=True))
    op.add_column("server_metrics", sa.Column("cpu_freq", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("cpu_temp", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("cpu_throttle", sa.Float(), nullable=True))
    op.add_column(
        "servers",
        sa.Column("temp_alert_c", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("servers", "temp_alert_c")
    op.drop_column("server_metrics", "cpu_throttle")
    op.drop_column("server_metrics", "cpu_temp")
    op.drop_column("server_metrics", "cpu_freq")
    op.drop_column("server_metrics", "cpu_cores_pct")
