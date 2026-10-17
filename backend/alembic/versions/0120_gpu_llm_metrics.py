"""GPU и LLM-сервисы в истории метрик (app/gpu.py): графики раздела GPU и блока LLM."""

import sqlalchemy as sa
from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("gpu", sa.JSON(), nullable=True))
    op.add_column("server_metrics", sa.Column("llm", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_metrics", "llm")
    op.drop_column("server_metrics", "gpu")
