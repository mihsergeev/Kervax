"""История износа SSD (app/disk_wear.py): по ней панель говорит, когда диск дойдет до 90% и до
100%, а не только что износ уже 90%."""

import sqlalchemy as sa
from alembic import op

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("disk_wear", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "disk_wear")
