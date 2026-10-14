"""Где ломается сайт: фазы запроса у монитора и в истории, проверка изнутри сервера."""

import sqlalchemy as sa
from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("last_phases", sa.JSON(), nullable=True))
    op.add_column("checks", sa.Column("last_inside", sa.JSON(), nullable=True))
    for col in ("ph_dns", "ph_connect", "ph_tls", "ph_wait"):
        op.add_column("check_samples", sa.Column(col, sa.Integer(), nullable=True))
    op.add_column("probe_requests", sa.Column("kind", sa.String(16), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("probe_requests", "kind")
    for col in ("ph_wait", "ph_tls", "ph_connect", "ph_dns"):
        op.drop_column("check_samples", col)
    op.drop_column("checks", "last_inside")
    op.drop_column("checks", "last_phases")
