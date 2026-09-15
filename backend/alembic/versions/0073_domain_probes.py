"""Разовая проверка найденных доменов перед постановкой на мониторинг.

Мастер «Домены, найденные на серверах» проверяет каждый предложенный домен снаружи
(панелью) и изнутри сервера (агентом) и предлагает рабочий вариант. Итоги — в
domain_probes; запрос агенту идёт тем же путём, что ручная проверка монитора
(probe_requests), только без монитора: адрес в probe_requests.url, check_id = 0.
"""

from alembic import op
import sqlalchemy as sa

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "probe_requests",
        sa.Column("url", sa.String(512), nullable=False, server_default=""),
    )
    op.create_table(
        "domain_probes",
        sa.Column("domain", sa.String(255), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("by_user", sa.String(64), nullable=False, server_default=""),
        sa.Column("ext_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ext_status", sa.String(16), nullable=False, server_default=""),
        sa.Column("ext_latency_ms", sa.Integer(), nullable=True),
        sa.Column("ext_message", sa.String(512), nullable=False, server_default=""),
        sa.Column("local_server_id", sa.Integer(), nullable=True),
        sa.Column("local_request_id", sa.Integer(), nullable=True),
        sa.Column("local_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("local_status", sa.String(16), nullable=False, server_default=""),
        sa.Column("local_latency_ms", sa.Integer(), nullable=True),
        sa.Column("local_message", sa.String(512), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("domain_probes")
    op.drop_column("probe_requests", "url")
