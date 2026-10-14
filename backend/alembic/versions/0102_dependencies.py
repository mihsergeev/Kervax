"""Зависимости: родитель у монитора (авто, вручную или без) и у сервера (вручную)."""

import sqlalchemy as sa
from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("parent_mode", sa.String(8), nullable=False, server_default="auto"))
    op.add_column("checks", sa.Column("parent_server_id", sa.Integer(), nullable=True))
    op.add_column("servers", sa.Column("parent_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "parent_id")
    op.drop_column("checks", "parent_server_id")
    op.drop_column("checks", "parent_mode")
