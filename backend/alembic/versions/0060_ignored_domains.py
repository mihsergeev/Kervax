"""Домены, которые не нужно предлагать к мониторингу.

Обнаружение показывает всё, что агенты видят на веб-серверах, а там всегда есть
служебное: адреса дев-стендов, внутренние панели, домены-парковки. Без «не нужен»
плашка «найдено N вне мониторинга» висит вечно и перестаёт что-либо значить.
"""

from alembic import op
import sqlalchemy as sa

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ignored_domains",
        sa.Column("domain", sa.String(length=255), primary_key=True),
        sa.Column("by_user", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ignored_domains")
