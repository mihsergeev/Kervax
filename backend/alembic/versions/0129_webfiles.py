"""Файлы сайтов (app/webfiles.py, helper webfiles-setup): servers.webfiles.

Веб-корни сервера, новые php и правки кода вне выкладки, последний разобранный прогон
helper'а и корни без своего монитора.
"""

import sqlalchemy as sa
from alembic import op

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("webfiles", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "webfiles")
