"""История отправленных алертов.

alert_events - каждый доставленный алерт и отбой: когда, какой вид, про какой сервер или
монитор, текст как в Telegram. Раздел и группа - для показа под права учетки.
"""

from alembic import op
import sqlalchemy as sa

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False, server_default=""),
        sa.Column("target", sa.String(255), nullable=False, server_default=""),
        sa.Column("section", sa.String(20), nullable=False, server_default=""),
        sa.Column("grp", sa.String(255), nullable=False, server_default=""),
        sa.Column("recovery", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_alert_events_ts", "alert_events", ["ts"])
    op.create_index("ix_alert_events_target", "alert_events", ["target", "ts"])


def downgrade() -> None:
    op.drop_index("ix_alert_events_target", table_name="alert_events")
    op.drop_index("ix_alert_events_ts", table_name="alert_events")
    op.drop_table("alert_events")
