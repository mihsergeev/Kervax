"""Mark server external addresses that came over a trusted path."""
from alembic import op
import sqlalchemy as sa

revision = "0153"
down_revision = "0152"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing addresses may come from the leftmost X-Forwarded-For: they stay unverified until
    # the next agent report over a trusted path.
    op.add_column("servers", sa.Column("external_ip_ok", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Last verified address per family and the address-change alerts already sent (per pair).
    op.add_column("servers", sa.Column("confirmed_ip4", sa.String(64), nullable=False, server_default=""))
    op.add_column("servers", sa.Column("confirmed_ip6", sa.String(64), nullable=False, server_default=""))
    op.add_column("servers", sa.Column("ip_alerts", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("servers", "ip_alerts")
    op.drop_column("servers", "confirmed_ip6")
    op.drop_column("servers", "confirmed_ip4")
    op.drop_column("servers", "external_ip_ok")
