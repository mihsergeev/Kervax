"""Каким путём агент проверил сайт: через localhost или сервис кластера в обход шлюза.

Сайт в Kubernetes за белым списком шлюза изнутри сервера не проверялся: на localhost шлюз не
слушает, а саму ноду не пускает его вайтлист. Агент 2.9 в таком случае идёт в сервис маршрута
напрямую, и панель должна это показывать — иначе зелёный статус читался бы как «сайт
открывается через шлюз».
"""

from alembic import op
import sqlalchemy as sa

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_probes", sa.Column("via", sa.String(128), nullable=False, server_default=""))
    op.add_column("probe_requests", sa.Column("via", sa.String(128), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("probe_requests", "via")
    op.drop_column("agent_probes", "via")
