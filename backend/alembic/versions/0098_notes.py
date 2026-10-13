"""Заметки к серверам и мониторам: видны в карточке и приезжают в алертах."""

import sqlalchemy as sa
from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for t in ("servers", "checks"):
        op.add_column(t, sa.Column("note", sa.Text(), nullable=False, server_default=""))
        op.add_column(t, sa.Column("note_by", sa.String(128), nullable=False, server_default=""))
        op.add_column(t, sa.Column("note_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for t in ("servers", "checks"):
        for c in ("note_at", "note_by", "note"):
            op.drop_column(t, c)
