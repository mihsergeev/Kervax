"""Алерты по глубине очередей RabbitMQ: порог на сервер + переопределения на очередь.

queue_alert_depth = 0 — фича выключена (значение по умолчанию): включать её всем
подряд нельзя, у каждого свои «нормальные» глубины очередей.
"""

from alembic import op
import sqlalchemy as sa

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("queue_alert_depth", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("servers", sa.Column("queue_alert_over", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "queue_alert_over")
    op.drop_column("servers", "queue_alert_depth")
