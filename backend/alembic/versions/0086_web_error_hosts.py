"""Ошибки 5xx по доменам.

web_error_samples.hosts - у лога, который пишут несколько сайтов и в формате которого есть
$host, helper webserver-setup 0.21 раскладывает ошибки минуты по доменам: [{"h": "site", "n": 41}].
По ним карточка "Ответы 5xx" и алерт говорят, какой именно сайт падает.
"""

from alembic import op
import sqlalchemy as sa

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_error_samples", sa.Column("hosts", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("web_error_samples", "hosts")
