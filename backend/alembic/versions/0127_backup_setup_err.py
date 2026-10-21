"""Упавшая настройка бэкапа у сервера (app/backup_state.py): servers.backup_setup_err.

Заполняется из последней задачи "настроить бэкап" каждого сервера, если она закончилась
ошибкой. Сервер, у которого бэкап с тех пор появился, панель в "Проблемы" не выносит: живой
бэкап важнее старой ошибки.
"""

import sqlalchemy as sa
from alembic import op

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("backup_setup_err", sa.JSON(), nullable=True))
    bind = op.get_bind()
    jobs = sa.table("backup_setup_jobs", sa.column("id", sa.Integer), sa.column("server_id", sa.Integer),
                    sa.column("backup_server_id", sa.Integer), sa.column("status", sa.String),
                    sa.column("message", sa.Text), sa.column("created_at", sa.DateTime(timezone=True)))
    srv = sa.table("servers", sa.column("id", sa.Integer), sa.column("backup_setup_err", sa.JSON))
    last: dict[int, tuple] = {}
    # задачи развертывания бэкап-сервера (server_id = backup_server_id) - не настройка клиента
    rows = bind.execute(sa.select(jobs.c.id, jobs.c.server_id, jobs.c.status, jobs.c.message, jobs.c.created_at)
                        .where(jobs.c.server_id != jobs.c.backup_server_id).order_by(jobs.c.id)).all()
    for jid, sid, st, msg, ts in rows:
        last[sid] = (jid, st, msg, ts)
    for sid, (jid, st, msg, ts) in last.items():
        if st != "error":
            continue
        bind.execute(sa.update(srv).where(srv.c.id == sid).values(backup_setup_err={
            "ts": ts.isoformat() if ts else None, "msg": (msg or "")[:250], "job": jid}))


def downgrade() -> None:
    op.drop_column("servers", "backup_setup_err")
