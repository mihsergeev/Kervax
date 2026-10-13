"""Известные проблемы серверов и мониторов: алерт молчит, пока не стало хуже, отметка сама
закрывается, когда проблема ушла."""

import sqlalchemy as sa
from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("known_issues", sa.JSON(), nullable=True))
    op.add_column("checks", sa.Column("known_issue", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "known_issue")
    op.drop_column("servers", "known_issues")
