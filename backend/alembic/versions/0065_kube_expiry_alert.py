"""Порог предупреждения о сроках Kubernetes и Flux.

Кластер умирает по срокам молча: уже запущенное продолжает работать, дашборды
зелёные, а новое просто перестаёт выкатываться — истёкший токен Flux виден
только тем, кто пойдёт читать `Ready` у GitRepository. Сертификаты control-plane
отказывают резче, но так же без предупреждения. Порог задаётся по серверу, как
у прочих; 0 выключает проверку.
"""

from alembic import op
import sqlalchemy as sa

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("kube_expiry_alert_days", sa.Integer(), nullable=False, server_default="14"),
    )


def downgrade() -> None:
    op.drop_column("servers", "kube_expiry_alert_days")
