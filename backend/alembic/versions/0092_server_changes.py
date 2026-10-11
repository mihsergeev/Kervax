"""Изменения на сервере.

servers.changes - последний снимок helper'а changes-setup (отпечатки SSH-ключей, пользователи,
sudo, хэши кронов, включенные юниты), лента изменений между снимками и отметки: последнее
изменение доступа и до какого момента изменения отмечены как свои.
"""

from alembic import op
import sqlalchemy as sa

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("changes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "changes")
