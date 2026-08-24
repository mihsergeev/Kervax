"""server_metrics: swap-активность (in/out) + разбивка памяти (slab/dirty/writeback)

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("swap_in", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("swap_out", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("mem_slab", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("mem_dirty", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("mem_writeback", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_metrics", "mem_writeback")
    op.drop_column("server_metrics", "mem_dirty")
    op.drop_column("server_metrics", "mem_slab")
    op.drop_column("server_metrics", "swap_out")
    op.drop_column("server_metrics", "swap_in")
