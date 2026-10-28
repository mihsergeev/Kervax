"""GRE до панели (app/gre.py): галочка в настройках сервера, своя /30 туннеля без пересечений
(уникальный индекс), публичный адрес ноды, состояние helper'а gre-setup и отметка, что отчет
агента пришел через туннель."""

import sqlalchemy as sa
from alembic import op

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("gre_enabled", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("servers", sa.Column("gre_net", sa.String(18), nullable=True))
    op.add_column("servers", sa.Column("gre_peer", sa.String(64), nullable=False, server_default=""))
    op.add_column("servers", sa.Column("gre_state", sa.JSON(), nullable=True))
    op.add_column("servers", sa.Column("gre_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("servers", sa.Column("gre_agent_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_servers_gre_net", "servers", ["gre_net"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_servers_gre_net", table_name="servers")
    for col in ("gre_agent_at", "gre_seen_at", "gre_state", "gre_peer", "gre_net", "gre_enabled"):
        op.drop_column("servers", col)
