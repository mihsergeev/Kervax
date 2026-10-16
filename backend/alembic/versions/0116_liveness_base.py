"""Эталон проверки "жив ли сервер" (app/liveness.py): достукивается ли панель до сервера, пока он
на связи. Без него "с панели не отвечает" у сервера, закрытого для панели файрволом, ничего не
значило бы."""

import sqlalchemy as sa
from alembic import op

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("liveness_base", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "liveness_base")
