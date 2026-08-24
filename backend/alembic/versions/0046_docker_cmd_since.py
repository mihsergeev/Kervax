"""docker_commands: since — логи за последние N секунд (0 = tail)

Revision ID: 0046
Revises: 0045
Create Date: 2026-07-17

"""
from alembic import op
import sqlalchemy as sa

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "docker_commands",
        sa.Column("since", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("docker_commands", "since")
