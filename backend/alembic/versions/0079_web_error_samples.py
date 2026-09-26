"""Минуты с ошибками 5xx по логам веб-сервера.

Алерт должен говорить, где именно ошибки, а ссылка из него вести на страницу с их
историей: какой лог, какие коды, какие пути. Пишем только минуты, в которые ошибки были:
они редки, а все минуты всех логов парка - миллионы строк в месяц.
"""

from alembic import op
import sqlalchemy as sa

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_error_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("src_ts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("log", sa.String(512), nullable=False, server_default=""),
        sa.Column("label", sa.String(255), nullable=False, server_default=""),
        sa.Column("e5", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rpm", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("codes", sa.JSON(), nullable=True),
        sa.Column("paths", sa.JSON(), nullable=True),
    )
    op.create_index("ix_web_error_samples_lookup", "web_error_samples", ["server_id", "ts"])
    op.create_index("ix_web_error_samples_ts", "web_error_samples", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_web_error_samples_ts", table_name="web_error_samples")
    op.drop_index("ix_web_error_samples_lookup", table_name="web_error_samples")
    op.drop_table("web_error_samples")
