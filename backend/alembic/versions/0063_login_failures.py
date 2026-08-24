"""Счётчик неудачных входов — в базу, чтобы он был общим для воркеров.

Лимитер жил в памяти процесса. В scale-режиме uvicorn поднимает несколько
воркеров, и каждый считал попытки отдельно: порог 10 превращался в 10 × число
процессов, а снаружи защита выглядела работающей. Проверено на живой панели с
тремя воркерами — из 45 попыток подбора 22 дошли до проверки пароля.
"""

from alembic import op
import sqlalchemy as sa

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_failures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_login_failures_key", "login_failures", ["key"])
    op.create_index("ix_login_failures_ts", "login_failures", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_login_failures_ts", table_name="login_failures")
    op.drop_index("ix_login_failures_key", table_name="login_failures")
    op.drop_table("login_failures")
