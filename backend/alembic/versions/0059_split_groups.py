"""Группы серверов и группы сайтов — раздельно.

Наборы имён у них независимы («Infra» у нод, «Shop» у мониторов), поэтому один
общий список означал бы «выдай доступ к чему-то одноимённому». Прежняя колонка
groups переименована в server_groups: на момент миграции она нигде не заполнена,
но переименование сохраняет значения, если где-то всё же были.
"""

from alembic import op
import sqlalchemy as sa

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "groups", new_column_name="server_groups")
    op.add_column("users", sa.Column("site_groups", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "site_groups")
    op.alter_column("users", "server_groups", new_column_name="groups")
