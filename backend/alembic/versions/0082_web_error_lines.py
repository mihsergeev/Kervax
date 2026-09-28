"""Строки с 5xx в минутах с ошибками: посмотреть ошибку в панели, не заходя на сервер.

Helper 0.19 присылает до пяти последних строк с 5xx на лог в минуту, значения секретов
в query замаскированы на ноде.
"""

from alembic import op
import sqlalchemy as sa

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_error_samples", sa.Column("lines", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("web_error_samples", "lines")
