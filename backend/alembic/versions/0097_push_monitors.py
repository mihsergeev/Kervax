"""Push-мониторы: скрипт или крон сам сообщает панели, что отработал; нет сигнала вовремя - алерт."""

import sqlalchemy as sa
from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("push_token", sa.String(48), nullable=False, server_default=""))
    op.add_column("checks", sa.Column("push_period", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("checks", sa.Column("push_grace", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("checks", sa.Column("push_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("checks", sa.Column("push_fail", sa.String(255), nullable=False, server_default=""))
    op.create_index("ix_checks_push_token", "checks", ["push_token"])


def downgrade() -> None:
    op.drop_index("ix_checks_push_token", table_name="checks")
    for c in ("push_fail", "push_at", "push_grace", "push_period", "push_token"):
        op.drop_column("checks", c)
