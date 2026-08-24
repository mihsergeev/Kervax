"""сейф доступов к бэкапам (только шифротекст)

Revision ID: 0056
Revises: 0055
"""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_vault",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("repo", sa.String(length=200), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("server_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("repo", name="uq_backup_vault_repo"),
    )
    op.create_index("ix_backup_vault_repo", "backup_vault", ["repo"])


def downgrade() -> None:
    op.drop_index("ix_backup_vault_repo", table_name="backup_vault")
    op.drop_table("backup_vault")
