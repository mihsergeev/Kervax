"""Ручная проверка локальных сайтов через агента.

«Проверить сейчас» у сайта за белым списком ходило из панели — туда, куда монитор
как раз не ходит. Панель, стоящая в белом списке, получала «работает», агент изнутри
продолжал видеть сбой, и через минуту всё снова краснело. Теперь кнопка просит
свежую проверку у агента на ноде: запрос живёт в probe_requests, признак «есть
запросы» — на строке сервера, а результат ручной проверки на время перекрывает
плановый ответ агента (agent_probes.manual_until).
"""

from alembic import op
import sqlalchemy as sa

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "probe_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("code", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(512), nullable=False, server_default=""),
        sa.Column("kw_up_found", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("kw_down_found", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cert_expires", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cert_issuer", sa.String(128), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default=""),
        sa.Column("message", sa.String(512), nullable=False, server_default=""),
    )
    op.create_index("ix_probe_requests_check_id", "probe_requests", ["check_id"])
    op.create_index("ix_probe_requests_server_id", "probe_requests", ["server_id"])
    op.add_column(
        "servers", sa.Column("probe_pending_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "agent_probes", sa.Column("manual_until", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("agent_probes", "manual_until")
    op.drop_column("servers", "probe_pending_at")
    op.drop_index("ix_probe_requests_server_id", table_name="probe_requests")
    op.drop_index("ix_probe_requests_check_id", table_name="probe_requests")
    op.drop_table("probe_requests")
