"""Частота CPU занижена (cpu_cap): питание или BMC урезали процессор, а тепловой троттлинг молчит.
Повод - сервер потерял второй ввод питания, и BMC держал CPU на 800 МГц вместо 3.9 ГГц 15 часов:
счетчик троттлинга 0, процессор холодный. Беда - CPU занят, а частота на половине пика и ниже
20 минут подряд. Все данные синтетические."""
from datetime import datetime, timedelta, timezone

import pytest

from app import collector, srv_short
from app.models import Server

MIN = timedelta(minutes=1)


def _srv(now, state=None, **kw):
    base = dict(id=1, name="node-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                offline_after_seconds=120, alert_sustain_seconds=900, last_report={},
                alert_state=state or {}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                web_5xx_alert_percent=0, kube_expiry_warn_days=[])
    base.update(kw)
    return Server(**base)


class Node:
    """Отчет раз в минуту и состояние, как его ведет цикл алертов (_since, _cool, _peak, уровень)."""

    def __init__(self, now=None, state=None):
        self.now = now or datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        self.s = _srv(self.now, state)
        self.cx: dict = {}

    def report(self, freq, cpu, **extra):
        self.now += MIN
        self.s.last_seen = self.now
        self.s.last_report = {"cpu_freq": freq, "cpu_percent": cpu, **extra}
        cond = collector._server_conditions(self.s, self.now)
        if "cpu_cap" not in cond:
            return None
        lvl, self.cx = cond["cpu_cap"]
        st = dict(self.s.alert_state or {})
        st.update(cpu_cap=lvl, cpu_cap_since=self.cx["since"], cpu_cap_cool=self.cx.get("cool"),
                  cpu_cap_peak=self.cx["peak"])
        self.s.alert_state = st
        return lvl

    def run(self, n, freq, cpu):
        return [self.report(freq, cpu) for _ in range(n)]


def _warm(n=None):
    """Нода в норме: под нагрузкой ~3.9 ГГц, пик выучен."""
    node = n or Node()
    node.run(10, 3900, 20)
    return node


def test_capped_busy_cpu_alerts_after_20_minutes():
    node = _warm()
    # 800 из 3900 при CPU 85%: первые 20 отчетов - выдержка (момент начала - первый из них)
    assert node.run(20, 800, 85) == [0] * 20
    assert node.report(800, 85) == 1
    assert {k: node.cx[k] for k in ("value", "max", "cpu", "minutes")} == {
        "value": 800, "max": 3900, "cpu": 85, "minutes": 20}
    full = collector._fmt_rule(collector.settings_store.SERVER_ALERT_KINDS["cpu_cap"][1], node.s, node.cx)
    assert full.startswith("CPU ограничен по частоте: 800 из 3900 МГц при загрузке 85% уже 20 мин.")
    assert "журнал IPMI" in full
    what, facts = srv_short.short("cpu_cap", node.cx, node.s, full, 1)
    assert what == "CPU ограничен по частоте: 800 из 3900 МГц" and facts[0] == "Загрузка 85%, уже 20 мин"
    # проблема, а не предупреждение: в Telegram красным
    assert srv_short.icon("cpu_cap", 1) == "🔴🔌"
    # держится дальше - уровень тот же, минуты растут
    assert node.run(30, 800, 90) == [1] * 30 and node.cx["minutes"] == 50


def test_alert_ignores_throttle_counter_and_cold_cpu():
    """Как в живом случае: троттлинга по счетчику нет, температура упала - алерт все равно есть."""
    node = _warm()
    for _ in range(20):
        assert node.report(800, 85, cpu_throttle=0, cpu_temp=31) == 0
    assert node.report(800, 85, cpu_throttle=0, cpu_temp=31) == 1


def test_idle_hardware_at_min_frequency_is_quiet():
    node = _warm()
    # простой: железо сидит на минимальной частоте при CPU ~1%
    assert set(node.run(120, 800, 1)) == {0}
    # и у самого порога загрузки
    assert set(node.run(60, 800, 29)) == {0}


def test_vm_with_constant_frequency_is_quiet():
    node = Node()
    assert set(node.run(120, 2400, 95)) == {0}
    assert node.cx["peak"]["mhz"] == 2400


def test_short_dips_are_quiet():
    """Самый длинный отрезок без беды на парке - 9 отчетов: каждый раз выдержка с нуля."""
    node = _warm()
    for _ in range(5):
        assert node.run(9, 800, 85) == [0] * 9
        assert node.report(3800, 85) == 0
        assert node.cx["since"] is None


def test_half_peak_is_the_line():
    node = _warm()
    assert set(node.run(40, 2000, 85)) == {0}  # чуть выше половины пика
    assert node.run(21, 1950, 85)[-1] == 1  # ровно половина - уже беда


def test_recovers_when_frequency_returns():
    node = _warm()
    node.run(21, 800, 85)
    assert node.s.alert_state["cpu_cap"] == 1
    # частота вернулась: беды нет, дальше отбой ведет общая выдержка (в этих тестах она выключена)
    assert node.report(3800, 85) == 0
    assert node.cx["since"] is None and node.cx["cool"] is not None
    # после отбоя новая беда - снова с полной выдержкой
    assert node.run(20, 800, 85) == [0] * 20
    assert node.report(800, 85) == 1


def test_iowait_is_not_load():
    """Ядра ждут диск (ребилд, медленный бэкап) и законно простаивают на минимальной частоте."""
    node = _warm()
    for _ in range(40):
        assert node.report(800, 60, cpu_iowait=45) == 0


def test_idle_while_still_capped_does_not_recover():
    """Загрузка упала, а частота так и урезана: "частота CPU восстановилась" было бы неправдой.
    Держим, пока частота хоть раз не поднимется выше половины пика."""
    node = _warm()
    node.run(21, 800, 85)
    assert node.run(60, 800, 5) == [1] * 60
    assert node.report(3800, 5) == 0


def test_settle_window_after_frequency_returned():
    """Уровень в состоянии еще держит общая выдержка отбоя. Частота уже поднималась: простой на
    минимальной частоте - не беда (иначе починенный простаивающий сервер не получил бы отбоя
    никогда). Урезали снова под нагрузкой - та же история, уровень сразу, без выдержки 20 минут."""
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    st = {"cpu_cap": 1, "cpu_cap_cool": now.isoformat(), "cpu_cap_peak": {"mhz": 3900, "at": now.isoformat()}}
    s = _srv(now, dict(st), last_report={"cpu_freq": 800, "cpu_percent": 3})
    lvl, cx = collector._server_conditions(s, now + MIN)["cpu_cap"]
    assert lvl == 0 and cx["cool"] == now.isoformat()
    s = _srv(now, dict(st), last_report={"cpu_freq": 800, "cpu_percent": 85})
    lvl, cx = collector._server_conditions(s, now + MIN)["cpu_cap"]
    assert lvl == 1 and cx["cool"] is None and cx["since"] is not None


def test_no_alert_without_learned_peak():
    """Первые отчеты сразу низкие: пика выше еще не видели, сравнивать не с чем."""
    node = Node()
    assert set(node.run(60, 800, 85)) == {0}
    assert node.cx["peak"]["mhz"] == 800
    # частота выросла - пик растет с ней
    node.report(3900, 40)
    assert node.cx["peak"]["mhz"] == 3900


def test_no_condition_without_frequency_or_load():
    node = Node()
    assert node.report(None, 85) is None
    assert node.report(0, 85) is None
    assert node.report(3900, None) is None


def test_peak_relearns_after_30_days_off_peak():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    # у пика была 29 дней назад: пик держится, 1900 из 3900 при нагрузке - беда
    node = Node(now, {"cpu_cap_peak": {"mhz": 3900, "at": (now - timedelta(days=29)).isoformat()}})
    assert node.report(1900, 85) == 0
    assert node.cx["peak"]["mhz"] == 3900 and node.cx["since"] is not None
    # не была у пика больше 30 дней (заменили железо): учим заново с текущей
    node = Node(now, {"cpu_cap_peak": {"mhz": 3900, "at": (now - timedelta(days=31)).isoformat()}})
    assert set(node.run(40, 1900, 85)) == {0}
    assert node.cx["peak"]["mhz"] == 1900
    # частота у пика держит отметку свежей, но пишется она не чаще раза в час
    at = node.cx["peak"]["at"]
    assert at == (now + MIN).isoformat()
    node.run(20, 1850, 85)  # 59 минут с отметки
    assert node.cx["peak"]["at"] == at
    node.report(1850, 85)
    assert node.cx["peak"]["at"] == node.now.isoformat()
    # пока висит алерт, пик не переучивается: месяц урезания в ожидании блока питания - не норма
    node = Node(now, {"cpu_cap": 1, "cpu_cap_peak": {"mhz": 3900, "at": (now - timedelta(days=31)).isoformat()}})
    assert node.report(800, 85) == 1 and node.cx["peak"]["mhz"] == 3900


def test_home_problems_show_announced_cap_as_problem():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    s = _srv(now, {"cpu_cap": 1, "cpu_cap_peak": {"mhz": 3900, "at": now.isoformat()}},
             last_report={"cpu_freq": 800, "cpu_percent": 85})
    p = [x for x in collector.server_problems(s, now) if x["kind"] == "cpu_cap"]
    assert len(p) == 1 and p[0]["level"] == 2 and p[0]["mute"] == "cpu_cap"
    # раздел - карточка частоты, как у ссылки из алерта
    assert p[0]["text"] == "частота CPU 800 из 3900 МГц" and p[0]["sec"] == "freq"
    # частота уже поднялась, уровень держит выдержка отбоя: строка не выдает исправную частоту за беду
    s.alert_state = {**s.alert_state, "cpu_cap_cool": now.isoformat()}
    s.last_report = {"cpu_freq": 3800, "cpu_percent": 85}
    p = [x for x in collector.server_problems(s, now) if x["kind"] == "cpu_cap"]
    assert p[0]["text"] == "частота CPU снова 3800 МГц, отбой после выдержки"
    # до алерта (идет выдержка) строки нет
    s.alert_state = {"cpu_cap_peak": {"mhz": 3900, "at": now.isoformat()}}
    assert [x for x in collector.server_problems(s, now) if x["kind"] == "cpu_cap"] == []


async def test_cpu_cap_alert_and_recovery_reach_chat(tmp_path, monkeypatch):
    """Весь путь через цикл алертов: пик пишется в alert_state, алерт уходит один раз красным,
    отбой - когда частота вернулась (общую выдержку отбоя здесь выключает conftest)."""
    from sqlalchemy import select

    from app.alerts import full_text
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    db = (tmp_path / "cap.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def rep(freq, cpu):
        return {"uptime_seconds": 100, "cpu_freq": freq, "cpu_percent": cpu, "cpu_throttle": 0, "cpu_temp": 31}

    async with factory() as s:
        s.add(Server(name="node-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0, last_report=rep(3900, 20)))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    clock = [now]

    async def tick(freq, cpu):
        clock[0] += MIN
        async with factory() as s:
            srv = await s.scalar(select(Server))
            srv.last_report, srv.last_seen = rep(freq, cpu), clock[0]
            await s.commit()
        await collector.evaluate_servers(factory, settings, clock[0])

    async def state():
        async with factory() as s:
            return (await s.scalar(select(Server))).alert_state or {}

    for _ in range(5):
        await tick(3900, 20)
    assert (await state())["cpu_cap_peak"]["mhz"] == 3900
    for _ in range(20):
        await tick(800, 85)
    assert sent == []
    await tick(800, 85)
    assert len(sent) == 1 and sent[0].startswith("🔴🔌")
    assert "<b>CPU ограничен по частоте: 800 из 3900 МГц</b>" in sent[0]
    assert "проверьте блоки питания и журнал IPMI" in full_text(sent[0])
    sent.clear()
    await tick(3800, 85)
    await tick(3800, 85)
    assert len(sent) == 1 and sent[0].startswith("✅") and "частота CPU восстановилась" in sent[0]
    await engine.dispose()


@pytest.mark.calm
async def test_cap_again_inside_settle_is_one_story(tmp_path, monkeypatch):
    """С боевой выдержкой отбоя (_SETTLE_S): питание моргнуло внутри нее - это одна история."""
    from sqlalchemy import select

    from app.alerts import full_text
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    db = (tmp_path / "cap2.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def rep(freq, cpu):
        return {"uptime_seconds": 100, "cpu_freq": freq, "cpu_percent": cpu, "cpu_throttle": 0, "cpu_temp": 31}

    async with factory() as s:
        s.add(Server(name="node-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0, last_report=rep(3900, 20)))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    clock = [now]

    async def tick(freq, cpu):
        clock[0] += MIN
        async with factory() as s:
            srv = await s.scalar(select(Server))
            srv.last_report, srv.last_seen = rep(freq, cpu), clock[0]
            await s.commit()
        await collector.evaluate_servers(factory, settings, clock[0])

    async def state():
        async with factory() as s:
            return (await s.scalar(select(Server))).alert_state or {}

    for _ in range(5):
        await tick(3900, 20)
    assert (await state())["cpu_cap_peak"]["mhz"] == 3900
    for _ in range(20):
        await tick(800, 85)
    assert sent == []
    await tick(800, 85)
    assert len(sent) == 1 and sent[0].startswith("🔴🔌")
    assert "<b>CPU ограничен по частоте: 800 из 3900 МГц</b>" in sent[0]
    assert "проверьте блоки питания и журнал IPMI" in full_text(sent[0])
    sent.clear()
    # частота вернулась на 10 минут, и питание снова пропало - внутри общей выдержки отбоя:
    # ни ложного "восстановилась", ни тихого повтора
    for _ in range(10):
        await tick(3800, 85)
    for _ in range(25):
        await tick(800, 85)
    assert sent == []
    for _ in range(20):
        await tick(3800, 85)
    assert len(sent) == 1 and sent[0].startswith("✅") and "частота CPU восстановилась" in sent[0]
    await engine.dispose()
