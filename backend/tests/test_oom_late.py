"""OOM, о котором сообщаем поздно, помечен "с опозданием".

Повтор в течение часа после прошлого сообщения ждет конца часа, а если панель в это время лежала,
уходит еще позже. Текст при этом был как у свежего алерта, и человек шел разбираться с тем, что
было два часа назад.
"""
import copy
from datetime import datetime, timedelta, timezone

from app import oom
from test_oom import _block, _server


def _ep(end: float) -> dict:
    return {"start": end - 5, "end": end, "kind": "global", "kills": [], "units": {}}


def test_late_text():
    now = 1_900_000_000.0
    assert oom.late_text(_ep(now - 120), now) == "", "обычная задержка - не опоздание"
    assert oom.late_text(_ep(now - 3000), None) == ""
    assert oom.late_text(_ep(now - 52 * 60), now) == "с опозданием на 52 мин"
    held = oom.late_text(_ep(now - 107 * 60), now, held=True)
    assert held.startswith("с опозданием на 1 ч 47 мин: повторы в течение часа")


def test_held_repeat_after_panel_pause_is_marked_late():
    """Повтор придержан на час, панель легла до конца часа, после подъема сводка помечена."""
    now = datetime.fromtimestamp(1_900_000_000, timezone.utc)
    b = _block(now.timestamp())
    st = {"oom_end": 0.0}
    rep = {"clock_unix": now.timestamp(), "extras": {"oom": b}}
    fire, done, now_apply = oom.alert_step(_server(rep, total=3), st, rep, now, timezone.utc)
    assert fire is not None and "с опозданием" not in fire[1], "свежий OOM помечен опоздавшим"
    st.update(now_apply)
    st.update(done)
    later = now + timedelta(minutes=2)
    b2 = copy.deepcopy(b)
    b2["kills"].append(dict(b2["kills"][-1], ts=later.timestamp() - 100, pid=5555))
    b2["ts"] = later.timestamp() - 10
    rep2 = {"clock_unix": later.timestamp(), "extras": {"oom": b2}}
    fire, _d, upd = oom.alert_step(_server(rep2, total=4), st, rep2, later, timezone.utc)
    assert fire is None and len(upd["oom_held"]) == 1
    st.update(upd)
    # панель лежала, поднялась через 1 ч 50 мин
    back = now + timedelta(minutes=110)
    b2["ts"] = back.timestamp() - 10
    rep3 = {"clock_unix": back.timestamp(), "extras": {"oom": b2}}
    fire, _d, _ = oom.alert_step(_server(rep3, total=4), st, rep3, back, timezone.utc)
    assert fire is not None
    head = fire[1].splitlines()[0]
    assert "с опозданием на 1 ч" in head and "повторы в течение часа" in head
