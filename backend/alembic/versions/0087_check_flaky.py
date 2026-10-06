"""Сайт отвечает через раз.

checks.flaky_since - с какого момента сбои идут вперемешку с успешными проверками (flaky.py),
checks.flaky_notified - отправлен ли об этом алерт (по нему решается, нужен ли отбой).
"""

from alembic import op
import sqlalchemy as sa

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("flaky_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "checks",
        sa.Column("flaky_notified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("checks", "flaky_notified")
    op.drop_column("checks", "flaky_since")
