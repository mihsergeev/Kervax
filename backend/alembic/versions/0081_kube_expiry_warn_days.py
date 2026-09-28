"""Сроки Kubernetes: несколько порогов вместо одного.

Было kube_expiry_alert_days - одно предупреждение за 14 дней. Стало
kube_expiry_warn_days - список порогов, по умолчанию [7, 1]: сообщение за неделю и
еще одно за день. Выключенная проверка (0) остается выключенной (пустой список).
"""

from alembic import op
import sqlalchemy as sa

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None

servers = sa.table(
    "servers",
    sa.column("kube_expiry_alert_days", sa.Integer),
    sa.column("kube_expiry_warn_days", sa.JSON),
)


def upgrade() -> None:
    op.add_column("servers", sa.Column("kube_expiry_warn_days", sa.JSON(), nullable=True))
    op.execute(servers.update().where(servers.c.kube_expiry_alert_days > 0)
               .values(kube_expiry_warn_days=[7, 1]))
    op.execute(servers.update().where(servers.c.kube_expiry_alert_days <= 0)
               .values(kube_expiry_warn_days=[]))
    op.drop_column("servers", "kube_expiry_alert_days")


def downgrade() -> None:
    op.add_column("servers", sa.Column("kube_expiry_alert_days", sa.Integer(), nullable=False,
                                       server_default="14"))
    op.drop_column("servers", "kube_expiry_warn_days")
