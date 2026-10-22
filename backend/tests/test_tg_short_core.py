"""Короткий формат алертов в Telegram (app/tg_fmt.py, app/srv_short.py): что видит человек в чате."""

import re
from types import SimpleNamespace

from app import alerts, srv_short, tg_fmt
from app.collector import _server_alert_text

BAD = re.compile(r"[—–≥≤↳«»·…ёЁ•]")


def test_render_is_the_users_example():
    text = tg_fmt.render("🔴🧠", '<a href="u">k8s-a</a>', "Кончилась память (OOM)",
                         ["Убит: mariadbd", "Под: wordpress-mariadb (лимит 1гб)"])
    assert text == ('🔴🧠 <a href="u">k8s-a</a>\n<b>Кончилась память (OOM)</b>\n'
                    "Убит: mariadbd\nПод: wordpress-mariadb (лимит 1гб)")


def test_render_keeps_names_lowercase_and_cuts_to_two_facts():
    text = tg_fmt.render("⚠️🐳", "n", "прокси держит docker-сокет", ["caddy-proxy-caddy-1", "b", "c"])
    assert text.split("\n") == ["⚠️🐳 n", "<b>Прокси держит docker-сокет</b>", "caddy-proxy-caddy-1", "b"]


def test_only_keyboard_symbols_reach_the_chat():
    text = tg_fmt.render("⚠️", "n", "диск 86% ≥ 85% — всё «плохо»…", ["a · b", "↳ лог: ошибка"])
    assert not BAD.search(text)
    assert "лог" not in text  # строка с ↳ - подробности для панели


def test_numbers_like_a_human():
    assert tg_fmt.mb(1012) == "1гб"
    assert tg_fmt.size(58.3 * 1024 ** 3) == "58гб"
    assert tg_fmt.size(7.0 * 1024 ** 3) == "7гб"
    assert tg_fmt.size(340 * 1024 ** 2) == "340мб"
    assert tg_fmt.dur(5.908) == "6 с"
    assert tg_fmt.dur(3 * 3600 + 120) == "3 ч"
    assert tg_fmt.count(25209) == "25 тыс."
    assert tg_fmt.days(1) == "1 день" and tg_fmt.days(3) == "3 дня" and tg_fmt.days(14) == "14 дней"


def test_names_cut_at_three():
    assert tg_fmt.names(["a", "b", "c", "d", "e"]) == "a, b, c и еще 2"
    assert tg_fmt.names(["a", "a", "b"]) == "a, b"


def test_server_alert_is_short_and_keeps_the_full_text_for_history():
    m = _server_alert_text("disk", "srv", "диск 86% ≥ 85% - больше всего места: /var 58 ГБ", "https://p/?server=1",
                           short=("Диск / заполнен на 86%", ["Занимает: /var (58гб)"]))
    assert str(m) == ('⚠️💽 <a href="https://p/?server=1">srv</a>\n<b>Диск / заполнен на 86%</b>\n'
                      "Занимает: /var (58гб)")
    assert "больше всего места" in m.full and "больше всего" not in str(m)
    assert m.line == '⚠️💽 <a href="https://p/?server=1">srv</a>: диск / заполнен на 86%, занимает: /var (58гб)'


def test_recovery_is_one_line():
    m = _server_alert_text("disk", "srv", "диск снова в норме, 82%", recovery=True)
    assert str(m) == "✅ srv: диск снова в норме, 82%" and m.recovery


def test_tail_goes_to_the_full_text_only():
    m = _server_alert_text("offline", "srv", "недоступен", short=("Не на связи", []))
    t = m.tail("\n↳ за час до этого: перезагрузка", "\nмашина не отвечает")
    assert str(t).endswith("\nмашина не отвечает") and "за час" not in str(t)
    assert "за час до этого" in t.full
    old = alerts.Msg("🔥 srv — недоступен")
    assert str(old.tail("\nеще")) == "🔥 srv — недоступен\nеще"


def test_digest_is_one_line_per_alert():
    msgs = [_server_alert_text("disk", f"s{i}", "x", short=("Диск заполнен на 90%", ["/var"])) for i in range(3)]
    d = alerts._digest(msgs + [alerts.Msg("🔥 old — недоступен\n↳ подробности")])
    assert d.split("\n") == ["⚠️💽 s0: диск заполнен на 90%, /var",
                             "⚠️💽 s1: диск заполнен на 90%, /var", "⚠️💽 s2: диск заполнен на 90%, /var",
                             "🔥 old: недоступен"]


def test_storm_keeps_the_short_format_and_lists_servers_last():
    msgs = [_server_alert_text("units", f"s{i}", "упал node-exporter.service: код 1", f"https://p/{i}",
                               level=2, ref=f"srv:{i}", short=("Упал node-exporter.service", ["код 1"]))
            for i in range(4)]
    out = alerts.merge_storm(msgs)
    assert len(out) == 1
    lines = str(out[0]).split("\n")
    assert lines[0] == "🔴⚙️ <b>4 сервера</b>"
    assert lines[1:3] == ["<b>Упал node-exporter.service</b>", "код 1"]
    assert lines[3].count("<a href") == 4
    assert "↳" in out[0].full and len(out[0].parts) == 4


def test_icons_level_first_then_topic():
    assert srv_short.icon("oom", 2) == "🔴🧠"
    assert srv_short.icon("disk", 3) == "🚨💽"
    assert srv_short.icon("offline", 1) == "🔴📡"  # недоступен - всегда авария
    assert srv_short.icon("cpu", 1) == "⚠️⚡"
    assert srv_short.icon("known", 1) == "📌"


def _srv(rep):
    return SimpleNamespace(name="srv", last_report=rep)


def test_cpu_and_mem_name_the_eater_without_lists():
    rep = {"top_cpu": [{"comm": "ffmpeg", "cpu": 1235}, {"comm": "prometheus", "cpu": 121}],
           "top_mem": [{"comm": "php-fpm7.4", "rss": 6.3 * 1024 ** 3, "shared": 0},
                       {"comm": "postgres", "rss": 257 * 1024 ** 2, "shared": 0}]}
    assert srv_short.short("cpu", {"value": 96}, _srv(rep)) == ("CPU загружен на 96%", ["Грузит: ffmpeg (12 ядер)"])
    assert srv_short.short("mem", {"value": 93}, _srv(rep)) == ("Память занята на 93%", ["Ест: php-fpm7.4 (6.3гб)"])


def test_kube_pods_by_workload():
    pods = [{"ns": "tech1", "name": "bot-send-worker-6b6c6bc9f7-7jvcr", "reason": "CrashLoopBackOff"},
            {"ns": "tech1", "name": "bot-send-worker-6b6c6bc9f7-s2bb2", "reason": "CrashLoopBackOff"}]
    assert srv_short.short("kube_pod", {"items": pods}, _srv({})) == (
        "Не поднимаются поды", ["tech1/bot-send-worker x2 (CrashLoopBackOff)"])


def test_backup_server_repos():
    ctx = {"names": ["front-a", "front-b"],
           "whys": ["бэкап опоздал: обычно раз в сутки, последний 30 ч назад"] * 2}
    assert srv_short.short("backup_repo", ctx, _srv({})) == (
        "Опаздывают бэкапы", ["front-a, front-b"])


def test_dump_names_without_engine_paths():
    ctx = {"engines": "mysql@k8s.default.deploy.wordpress-mariadb, pg@k8s.default.sts.postgres"}
    assert srv_short.short("backup_dump", ctx, _srv({})) == ("Не обновляются дампы баз", ["wordpress-mariadb, postgres"])


def test_unknown_kind_falls_back_to_the_full_text():
    what, facts = srv_short.short("nope", {}, _srv({}), "диск sdc тормозит: задержка 105 мс")
    assert (what, facts) == ("Диск sdc тормозит", ["задержка 105 мс"])


def test_site_texts():
    assert srv_short.site("bad", "down", 'истекло время ожидания (Get "http://10.1.2.3/": context deadline '
                                          "exceeded) — сервис default/web:80 в обход шлюза") == (
        "🔴🌐", "Не отвечает", ["истекло время ожидания", "сервис default/web:80 в обход шлюза"])
    assert srv_short.site("flaky", "down", "отвечает через раз: 4 из 20 последних проверок не прошли, "
                                           "последняя ошибка: плохой ответ от бэкенда (HTTP 502)") == (
        "🟠🌐", "Отвечает через раз", ["плохой ответ от бэкенда (HTTP 502)"])
    assert srv_short.site("locpart", "", "🟢 Финляндия, Хельсинки — доступен\n🔴 Казахстан, Алматы — недоступен") == (
        "🟠🌍", "Недоступен: Алматы", ["Доступен: Хельсинки"])
    assert srv_short.site("domain", "", "регистрация домена example.com истекает через 27 дн. (мониторов на нем: 9)") == (
        "⚠️🌐", "домен истекает через 27 дней", [])
    assert srv_short.site("recovery", "up", "HTTP 200 · 454 мс") == "снова отвечает"
