"""Локальная проверка сайта агентом (сайт за белым списком IP).

Панель до такого сайта не дотянется — снаружи соединение рвут, и монитор вечно
«недоступен», хотя сайт жив. Проверяет агент на самом сервере: ходит на localhost
с нужным Host и SNI и присылает сырой результат, а оценивает его панель.
"""

from alembic import op
import sqlalchemy as sa

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("probe_server_id", sa.Integer(), nullable=True))
    op.create_table(
        "agent_probes",
        sa.Column("check_id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False, index=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("code", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("kw_up_found", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("kw_down_found", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_table("agent_probes")
    op.drop_column("checks", "probe_server_id")
