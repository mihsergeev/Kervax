"""Порог алерта по занятости коннектов СУБД.

Исчерпание пула подключений выводит приложение из строя так же надёжно, как
упавшая база, но выглядит иначе: в логе приложения «sorry, too many clients
already», а метрики самой базы при этом в норме — она жива и отвечает. Порог
задаётся по серверу, как у conntrack; 0 выключает проверку для всей ноды.
"""

from alembic import op
import sqlalchemy as sa

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("db_conn_alert_percent", sa.Integer(), nullable=False, server_default="85"),
    )


def downgrade() -> None:
    op.drop_column("servers", "db_conn_alert_percent")
