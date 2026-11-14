"""Persist notification transitions, recipient attempts and transport circuits."""
from alembic import op
import sqlalchemy as sa

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("check_incidents", sa.Column("notice_queued", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "notification_streams",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("members", sa.JSON(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_id", sa.Integer(), nullable=True),
    )
    op.create_table(
        "notification_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stream_key", sa.String(64), sa.ForeignKey("notification_streams.key"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("members", sa.JSON(), nullable=False),
        sa.Column("occurrence", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("history_recorded", sa.Boolean(), nullable=False),
        sa.Column("tg_alert_id", sa.Integer(), nullable=True),
        sa.UniqueConstraint("stream_key", "seq", name="uq_notification_event_seq"),
        sa.UniqueConstraint("stream_key", "occurrence", name="uq_notification_event_occurrence"),
    )
    op.create_index("ix_notification_events_stream_key", "notification_events", ["stream_key"])
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("notification_events.id"), nullable=False),
        sa.Column("recipient_key", sa.String(64), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("transport_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("uncertain_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", "recipient_key", name="uq_notification_delivery_recipient"),
    )
    op.create_index("ix_notification_deliveries_event_id", "notification_deliveries", ["event_id"])
    op.create_index("ix_notification_deliveries_transport_key", "notification_deliveries", ["transport_key"])
    op.create_index("ix_notification_delivery_due", "notification_deliveries", ["status", "next_attempt_at"])
    op.create_table(
        "notification_transports",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(128), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.drop_table("notification_events")
    op.drop_table("notification_transports")
    op.drop_table("notification_streams")
    op.drop_column("check_incidents", "notice_queued")
