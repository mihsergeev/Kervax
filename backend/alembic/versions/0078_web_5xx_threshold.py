"""Порог доли ответов 5xx на ноду.

По умолчанию 0.05% за 15 минут - так откалибровали алерт по балансерам в прометеусе:
сломанный DNS кластера давал 0.1-0.28% ошибок, а до поломки их почти не было. Мелкие
ноды страхует нижний порог по числу ошибок в коде сборщика.
"""

from alembic import op
import sqlalchemy as sa

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("web_5xx_alert_percent", sa.Float(),
                                       nullable=False, server_default="0.05"))


def downgrade() -> None:
    op.drop_column("servers", "web_5xx_alert_percent")
