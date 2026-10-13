"""Срок домена: напоминать за 30, 7 и 1 день вместо 7 и 1.

На продление домена бывает нужна оплата и согласование, неделя - поздно. Меняем только
мониторы со старыми порогами по умолчанию: настроенные руками не трогаем.
"""

from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE checks SET domain_warn_days = '[30, 7, 1]' "
               "WHERE CAST(domain_warn_days AS TEXT) IN ('[7, 1]', '[7,1]')")


def downgrade() -> None:
    op.execute("UPDATE checks SET domain_warn_days = '[7, 1]' "
               "WHERE CAST(domain_warn_days AS TEXT) IN ('[30, 7, 1]', '[30,7,1]')")
