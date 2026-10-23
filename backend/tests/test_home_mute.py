"""Заглушка до нового у групп "Требует действий" (app/home_mute.py) и у пунктов ОС (osupd:<вид>@)."""
import re
from datetime import datetime, timezone
from pathlib import Path

from app import home_mute, os_support
from app.models import Server

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())
DAY = 86400
FRONT = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _srv(rep, mutes=None, **kw):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state={}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[], **kw)


def _c(name, policy="no"):
    return {"name": name, "image": "app/x:1", "state": "running", "policy": policy, "status": "Up 3 weeks"}


def _docker(*names):
    return {"docker": {"present": True, "access": True, "containers": [_c(n) for n in names]}}


def test_new_container_brings_the_item_back():
    """Контейнеры без restart policy: заглушен набор, еще один контейнер - новый ключ, пункт вернулся,
    а старую заглушку сборщик снимает."""
    k = home_mute.keys(_srv(_docker("web", "cron")), NOW)["noboot-d"]
    assert k.startswith("home:noboot-d@")
    assert home_mute.keys(_srv(_docker("cron", "web")), NOW)["noboot-d"] == k  # порядок не важен
    s = _srv(_docker("web", "cron"), mutes=[k, "disk_health"])
    assert home_mute.stale(s, NOW) == []
    more = _srv(_docker("web", "cron", "queue"), mutes=[k])
    assert home_mute.keys(more, NOW)["noboot-d"] != k
    assert home_mute.stale(more, NOW) == [(k, 'заглушка снята, контейнеры без restart policy: появилось новое')]
    # контейнеру поставили restart policy - пункта нет, заглушка тоже уходит
    fixed = {"docker": {"present": True, "access": True, "containers": [_c("web", "always"), _c("cron", "always")]}}
    assert "noboot-d" not in home_mute.keys(_srv(fixed), NOW)
    assert home_mute.stale(_srv(fixed, mutes=[k]), NOW) == [
        (k, 'заглушка снята, контейнеры без restart policy: пункт ушел')]


def test_no_data_keeps_the_mute():
    """В отчете нет блока (старый агент, helper не прислал) - заглушку не трогаем."""
    k = home_mute.keys(_srv(_docker("web")), NOW)["noboot-d"]
    assert home_mute.stale(_srv({}, mutes=[k]), NOW) == []
    # группа с индексом всех серверов: без индекса (сборщик не строил) не трогаем
    assert home_mute.stale(_srv({"kube": {"access": True}}, mutes=["home:k-noagent@0123456789"]), NOW) == []


def test_plain_groups_have_no_fingerprint():
    """Docker без доступа: ключ без отпечатка, держится, пока пункт есть, снимается после починки."""
    rep = {"docker": {"present": True, "access": False}}
    assert home_mute.keys(_srv(rep), NOW) == {"d-acc": "home:d-acc"}
    assert home_mute.stale(_srv(rep, mutes=["home:d-acc"]), NOW) == []
    ok = {"docker": {"present": True, "access": True, "containers": []}}
    assert home_mute.stale(_srv(ok, mutes=["home:d-acc"]), NOW) == [
        ("home:d-acc", 'заглушка снята, Docker без доступа: пункт ушел')]


def test_db_dumps_ok_lifts_dbdump():
    s = _srv({}, mutes=["home:dbdump@0123456789"], db_dumps_ok=True)
    assert home_mute.stale(s, NOW) == [("home:dbdump@0123456789", 'заглушка снята, базы без отдельного дампа: пункт ушел')]


def test_unknown_group_mute_is_left_alone():
    assert home_mute.stale(_srv(_docker("web"), mutes=["home:nope@1", "home:"]), NOW) == []


def test_front_groups_match_backend():
    """Группы, у которых главная берет ключ из home_mutes, бэкенд знает: иначе галки молча не появятся."""
    src = (FRONT / "HomePage.tsx").read_text(encoding="utf-8")
    grps = set(re.findall(r"hm\(s, '([\w-]+)'\)", src))
    grps |= set(re.findall(r"hm\(s, lvl >= 2 \? '([\w-]+)' : '([\w-]+)'\)", src)[0])
    assert "hm(s, `ssh-${a.kind}`)" in src
    grps.add("ssh-ssh_password")
    assert grps == set(home_mute.SIGS)
    # у каждой группы подпись для журнала
    assert set(home_mute.LABELS) == set(home_mute.SIGS)


# --- ОС: osupd:<вид>@<отпечаток> ---

def _eol(rep, mutes=None, now=NOW):
    return next(a for a in os_support.advice(rep, now, mutes) if a["kind"] == "eol")


def test_eol_mute_until_new_state():
    """Конец поддержки глушится до смены версии ОС или состояния: обычная поддержка кончилась ->
    подключили Ubuntu Pro и подходит конец ESM - пункт вернется."""
    rep = {"os": "Ubuntu 20.04.6 LTS"}
    a = _eol(rep)
    assert a["new_key"].startswith("osupd:eol@") and not a["muted"]
    m = _eol(rep, [a["new_key"]])
    assert m["muted"] and m["muted_new"]
    assert os_support.stale_mutes(rep, [a["new_key"]], NOW) == []
    # тот же текст на другой день - тот же ключ
    assert _eol(rep, now=datetime(2027, 3, 1, tzinfo=timezone.utc))["new_key"] == a["new_key"]
    # Ubuntu Pro подключили: пункт ушел (до ESM далеко), заглушка снимается
    pro = {"os": "Ubuntu 20.04.6 LTS", "os_upd": {"esm": True}}
    assert os_support.stale_mutes(pro, [a["new_key"]], NOW) == [
        (a["new_key"], "заглушка конца поддержки ОС снята: пункт ушел, ОС Ubuntu 20.04.6 LTS")]
    late = datetime(2029, 12, 1, tzinfo=timezone.utc)
    assert _eol(pro, now=late)["new_key"] != a["new_key"]
    assert os_support.stale_mutes(pro, [a["new_key"]], late) == [
        (a["new_key"], "заглушка конца поддержки ОС снята: у поддержки ОС новое состояние")]
    # другая версия ОС - другой ключ
    assert _eol({"os": "Ubuntu 18.04.6 LTS"})["new_key"] != a["new_key"]


def test_security_and_apt_mutes():
    upd = {"security": 3, "unatt": "off", "apt_ts": TS - 20 * DAY}
    adv = {a["kind"]: a for a in os_support.advice({"os_upd": upd}, NOW)}
    sec, apt = adv["security"]["new_key"], adv["apt"]["new_key"]
    # новых обновлений больше - причина та же, заглушка держится
    more = {**upd, "security": 9}
    assert {a["kind"]: a["muted_new"] for a in os_support.advice({"os_upd": more}, NOW, [sec, apt])} == {
        "security": True, "apt": True}
    assert os_support.stale_mutes({"os_upd": more}, [sec, apt], NOW) == []
    # автообновления включили, но они не запускаются - причина новая
    on = {**upd, "unatt": "on", "unatt_ts": TS - 5 * DAY}
    assert os_support.stale_mutes({"os_upd": on}, [sec], NOW) == [
        (sec, "заглушка обновлений безопасности снята: у обновлений безопасности новая причина")]
    # apt update прошел - пункта нет
    fresh = {**upd, "apt_ts": TS - DAY}
    assert os_support.stale_mutes({"os_upd": fresh}, [apt], NOW) == [
        (apt, "заглушка списка пакетов снята: список пакетов обновлен")]
    # без os_upd не трогаем
    assert os_support.stale_mutes({}, [sec, apt], NOW) == []
