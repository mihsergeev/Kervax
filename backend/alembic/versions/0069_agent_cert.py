"""Срок сертификата, снятый агентом при локальной проверке.

Сайт за белым списком панель проверить не может: своя проверка сертификата
упирается в тот же обрыв, и в карточке висело «сайт недоступен (таймаут)» при
живом сертификате. Агент видит его на том же соединении, которым проверяет сайт.
"""

from alembic import op
import sqlalchemy as sa

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_probes",
                  sa.Column("cert_expires", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("agent_probes",
                  sa.Column("cert_issuer", sa.String(length=128), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("agent_probes", "cert_issuer")
    op.drop_column("agent_probes", "cert_expires")
