"""Эскалация алертов: отметка, что по алерту пришел отбой (повторять и глушить уже нечего)."""

import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tg_alerts", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tg_alerts", "resolved_at")
