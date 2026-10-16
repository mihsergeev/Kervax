"""Миграция 0110: пороги сроков по умолчанию - за 7 и за 1 день, свои пороги мониторов не трогаем."""
import importlib.util
import json
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration():
    p = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0110_expiry_warn_7_1.py"
    spec = importlib.util.spec_from_file_location("m0110", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_default_thresholds_become_week_and_day():
    eng = sa.create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(sa.text("CREATE TABLE checks (id INTEGER PRIMARY KEY, ssl_warn_days JSON, domain_warn_days JSON,"
                             " ssl_alerted_days INTEGER, domain_alerted_days INTEGER)"))
        for row in ((1, "[14, 7, 1]", "[30, 7, 1]", 14, 30),
                    (2, "[21, 3]", "[30, 7, 1]", None, 7),
                    (3, "[14, 7, 1]", "[60]", None, 60)):
            conn.execute(sa.text("INSERT INTO checks VALUES (:i, :s, :d, :sa, :da)"),
                         dict(zip(("i", "s", "d", "sa", "da"), row)))
        with Operations.context(MigrationContext.configure(conn)):
            _migration().upgrade()
        got = {r[0]: (json.loads(r[1]), json.loads(r[2]), r[3], r[4])
               for r in conn.execute(sa.text("SELECT * FROM checks ORDER BY id"))}
    # напоминание за 14/30 дней забыто: иначе первый проход прислал бы "домен продлен"
    assert got[1] == ([7, 1], [7, 1], None, None)
    # свой порог сертификата не трогаем, отметка "за 7 дней" остается
    assert got[2] == ([21, 3], [7, 1], None, 7)
    assert got[3] == ([7, 1], [60], None, 60)
