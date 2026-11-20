"""Index the notification outbox hot paths and retention: pending effects, button links, episodes."""
from alembic import op
import sqlalchemy as sa

revision = "0152"
down_revision = "0151"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification_deliveries", sa.Column("effects_pending", sa.Boolean(), nullable=False,
                                                       server_default=sa.false()))
    # флаг ставится только на подтвержденные без записанных последствий: обычно единицы строк
    if op.get_bind().dialect.name == "postgresql":
        op.execute("UPDATE notification_deliveries SET effects_pending = true WHERE status = 'confirmed' "
                   "AND (target ->> 'effects_recorded') IS DISTINCT FROM 'true'")
    else:
        op.execute("UPDATE notification_deliveries SET effects_pending = 1 WHERE status = 'confirmed' "
                   "AND json_extract(target, '$.effects_recorded') IS NOT 1")
    op.create_index("ix_notification_delivery_effects", "notification_deliveries", ["id"],
                    postgresql_where=sa.text("effects_pending"), sqlite_where=sa.text("effects_pending = 1"))
    op.create_index("ix_notification_events_tg_alert_id", "notification_events", ["tg_alert_id"])
    op.create_index("ix_notification_event_episode", "notification_events", ["stream_key", "episode"])


def downgrade() -> None:
    # target["effects_recorded"] пишется и дальше, старый код читает его без колонки
    op.drop_index("ix_notification_event_episode", table_name="notification_events")
    op.drop_index("ix_notification_events_tg_alert_id", table_name="notification_events")
    op.drop_index("ix_notification_delivery_effects", table_name="notification_deliveries")
    op.drop_column("notification_deliveries", "effects_pending")
