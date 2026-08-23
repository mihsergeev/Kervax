"""Именные учётки: разделы и группы, доступные пользователю.

NULL/пусто в обоих полях означает «без ограничений» — существующие учётки после
миграции работают ровно как раньше, ничего доназначать не нужно.
"""

from alembic import op
import sqlalchemy as sa

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("sections", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("groups", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "groups")
    op.drop_column("users", "sections")
