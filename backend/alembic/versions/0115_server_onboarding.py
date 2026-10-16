"""Подключение нового сервера (app/onboard.py): кто и когда его закрыл. Все серверы, которые уже
есть в панели, считаются подключенными: их алерты идут как шли."""

import sqlalchemy as sa
from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("onboard_done_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("servers", sa.Column("onboard_by", sa.String(128), nullable=False, server_default=""))
    op.execute(sa.text("UPDATE servers SET onboard_done_at = CURRENT_TIMESTAMP"))


def downgrade() -> None:
    op.drop_column("servers", "onboard_by")
    op.drop_column("servers", "onboard_done_at")
