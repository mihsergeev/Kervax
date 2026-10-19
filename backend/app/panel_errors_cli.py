"""Выгрузка журнала ошибок панели в JSON - для ops/panel-errors.py, только чтение.

    docker exec kervax-backend-1 python -m app.panel_errors_cli --days 7 [--all] [--detail 1500]

--all - вместе с разобранными. Печатает {"version", "now", "items": [...]}; вывод команд и
traceback обрезаны до --detail символов (секреты замаскированы еще при записи)."""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import panel_errors as pe
from app.config import get_settings
from app.db import create_engine_and_factory
from app.models import PanelError


async def dump(days: int, include_resolved: bool, detail: int) -> dict:
    settings = get_settings()
    engine, factory = create_engine_and_factory(settings.db_url)
    now = datetime.now(timezone.utc)
    try:
        async with factory() as s:
            q = select(PanelError).where(PanelError.last_at >= now - timedelta(days=days))
            if not include_resolved:
                q = q.where(PanelError.resolved.is_(False))
            rows = list(await s.scalars(q.order_by(PanelError.last_at.desc()).limit(2000)))
    finally:
        await engine.dispose()
    items = []
    for r in rows:
        d = pe.row_out(r)
        if len(d["detail"]) > detail:
            d["detail"] = d["detail"][:detail] + " ..."
        items.append(d)
    return {"version": settings.version, "now": now.isoformat(), "items": items}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.panel_errors_cli")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--all", action="store_true", help="вместе с разобранными")
    ap.add_argument("--detail", type=int, default=1500)
    a = ap.parse_args(argv)
    out = asyncio.run(dump(max(1, a.days), a.all, max(0, a.detail)))
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
