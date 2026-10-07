"""Первый отчет агента.

servers.first_report_at - пока нода только что поставлена, установщик еще ставит helper'ы и прокси
Docker, и панель их не требует. Ноды, которые уже отчитывались, получают дату заведения: для них
пауза давно прошла.
"""

from alembic import op
import sqlalchemy as sa

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("first_report_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE servers SET first_report_at = created_at WHERE last_seen IS NOT NULL")


def downgrade() -> None:
    op.drop_column("servers", "first_report_at")
