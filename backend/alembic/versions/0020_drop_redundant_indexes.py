"""drop redundant single-column indexes covered by composite indexes

На больших тайм-сериях (server_metrics/check_samples/location_samples) одиночные
индексы по ведущему столбцу композита лишь дублируют его — это лишняя запись при
вставке и лишнее место. Композиты (server_id, ts) / (check_id, ts) /
(check_id, location_id, ts) полностью покрывают запросы по этому столбцу.
ts-индексы оставляем — их использует прунинг (WHERE ts < cutoff).

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-13

"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_REDUNDANT = [
    ("ix_server_metrics_server_id", "server_metrics", "server_id"),
    ("ix_check_samples_check_id", "check_samples", "check_id"),
    ("ix_location_samples_check_id", "location_samples", "check_id"),
]


def upgrade() -> None:
    for name, _table, _col in _REDUNDANT:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def downgrade() -> None:
    for name, table, col in _REDUNDANT:
        op.create_index(name, table, [col])
