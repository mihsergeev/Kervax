"""Короткие тексты серверных алертов для Telegram (формат A, app/tg_fmt.py).

На каждый вид: что случилось одной фразой и одна-две строки конкретики - что именно и где.
Данные - тот же контекст, по которому собирается полный текст (_server_conditions в
collector.py): числа, списки из problems() модулей ("items"), имена. Вид без своей короткой
версии получает ее из полного текста запасным разбором (tg_fmt.split_detail).

Полный текст никуда не делся: он уходит в историю алертов и виден в панели по ссылке.
"""

from __future__ import annotations

import importlib
import re
from datetime import datetime

from app import going_down, mem_starve, podnames, tg_fmt

# Тема - второй эмодзи после уровня: по нему в ленте видно, про что алерт
TOPIC = {
    "offline": "📡", "reboot": "🔄", "cpu": "⚡", "cpu_spin": "🌀", "mem": "🧠", "oom": "🧠",
    "mem_leak": "📈", "mem_limit": "🧠", "pressure": "🫁", "temp": "🌡", "throttle": "🌡", "disktemp": "🌡",
    "gpu_heat": "🌡", "clock": "🕐", "dns": "🌐", "disk": "💽", "inode": "💽", "disk_forecast": "💽",
    "disk_health": "💽", "disk_slow": "🐢", "net_health": "📶", "link": "🔗", "traffic": "📊",
    "conntrack": "🔗", "listen_gone": "🔌", "exposed": "🔓", "port_new": "🚪", "cf_realip": "🎭", "access_change": "🔑",
    "access_risk": "🗝", "ssh_risk": "🔐", "docker_sock": "🐳", "docker_risk": "🐳", "units": "⚙️",
    "unit_loop": "⚙️", "workers": "👷", "cron_stuck": "⏲", "log_errors": "📜", "queue": "🐇",
    "queue_stuck": "⏳", "queue_failed": "🧨", "fpm": "🐘", "vpn_torrent": "🧲", "vpn_heavy": "📶", "gitlab": "🦊", "autoban": "🚫", "web_5xx": "🌐", "web_config": "🧾",
    "web_cert": "🔏", "webfiles": "🕵️", "db_conn": "🛢", "db_health": "🛢", "gpu": "🎛", "llm": "💬",
    "docker_down": "🐳", "docker_loop": "🐳", "docker_restarts": "🐳", "kube_pod": "☸️",
    "kube_restarts": "☸️", "kube_job": "☸️", "kube_cron": "☸️", "kube_node": "☸️",
    "kube_expiry": "⏳", "flux_down": "☸️", "flux_stale": "☸️", "backup_missing": "💾",
    "backup_failed": "💾", "backup_stale": "💾", "backup_growth": "💾", "backup_dump": "💾",
    "backup_dump_space": "💾", "backup_cron": "💾", "backup_custom": "💾", "backup_restore": "💾",
    "backup_repo": "💾", "backup_unmonitored": "💾", "backup_check": "💾", "backup_lock": "🔒",
    "backup_prune": "🧹", "backup_rotation": "🧹", "backup_mirror": "🪞", "known": "📌", "uncovered": "🧩",
    "bogus_requests": "🧟", "maint": "🚧",
}

# Уровень по умолчанию, если у вида нет своей шкалы (уровень 1 = "сработало"): авария - красным,
# остальное - предупреждением. У видов со шкалой (диск, юниты, OOM, часы) берется их уровень.
BASE = {
    "offline": 2, "docker_down": 2, "docker_loop": 2, "kube_pod": 2, "kube_job": 2, "kube_node": 2,
    "flux_down": 2, "web_5xx": 2, "web_config": 2, "backup_failed": 2, "backup_stale": 2,
    "backup_dump": 2, "backup_dump_space": 2, "backup_cron": 2, "backup_custom": 2,
    "backup_restore": 2, "backup_check": 2, "port_new": 2, "cf_realip": 2, "ssh_risk": 2, "access_risk": 2, "autoban": 2,
    "listen_gone": 2, "link": 2, "unit_loop": 2, "db_conn": 2, "dns": 2,
    # смена доступа - событие безопасности; висячий лок, сломанная чистка и проблемные репозитории
    # бэкап-сервера - поломка, а не напоминание. Главная красит их так же (level ниже)
    "access_change": 2, "backup_lock": 2, "backup_prune": 2, "backup_repo": 2, "backup_mirror": 2,
}
# эти виды знаками уровня не помечаются: событие, а не поломка
NO_LEVEL = frozenset({"known", "uncovered"})


def level(key: str, lvl: int = 1) -> int:
    """Уровень, с которым вид показывается: сильнейший из своего и базового для вида. Один
    источник для Telegram (icon) и "Что сломано" (collector.server_problems): раньше главная
    красила CPU красным, а алерт про него приходил желтым, и наоборот."""
    return min(max(int(lvl or 0), BASE.get(key, 1), 1), 3)


def icon(key: str, lvl: int = 1) -> str:
    """Эмодзи уровня и темы: "🔴🧠". Уровень - сильнейший из своего и базового для вида."""
    topic = TOPIC.get(key, "🖥")
    if key in NO_LEVEL:
        return topic
    return tg_fmt.LEVEL.get(level(key, lvl), "⚠️") + topic


# Отбой одной строкой: "✅ сервер: <это>"
RECOVERY = {
    "offline": "снова на связи", "cpu": "CPU снова в норме", "mem": "память снова в норме",
    "disk": "диск снова в норме", "inode": "inode снова в норме", "temp": "CPU остыл",
    "throttle": "троттлинга больше нет", "disktemp": "диск остыл", "conntrack": "conntrack снова в норме",
    "clock": "часы снова точные", "dns": "DNS снова отвечает", "db_conn": "подключений к СУБД снова хватает",
    "web_5xx": "ошибок 5xx больше нет", "disk_health": "диски снова в порядке",
    "gpu": "GPU снова в порядке", "gpu_heat": "GPU остыли", "llm": "LLM снова отвечает быстро",
    "queue_stuck": "очереди снова разбираются", "queue_failed": "задачи больше не падают",
    "workers": "воркеры снова работают", "cron_stuck": "кроны больше не висят",
    "log_errors": "ошибок в логах снова обычно", "fpm": "PHP-FPM снова справляется",
    "vpn_torrent": "торрент через VPN больше не качают", "vpn_heavy": "клиенты VPN больше не качают помногу",
    "gitlab": "GitLab снова в порядке", "autoban": "автобан снова в порядке",
    "disk_forecast": "диск больше не грозит заполниться", "units": "упавших юнитов нет",
    "cpu_spin": "пустых циклов больше нет", "backup_growth": "прирост бэкапа снова обычный",
    "docker_sock": "прокси больше не держит docker-сокет", "docker_risk": "контейнеров с root на хосте нет",
    "flux_down": "Flux снова выкатывает", "flux_stale": "репозиторий чартов снова отвечает",
    "kube_cron": "CronJob снова отрабатывают", "kube_pod": "поды поднялись",
    "kube_expiry": "сроки в кластере в порядке", "exposed": "лишних открытых портов нет",
    "cf_realip": "белые списки больше не обходятся подменой заголовка",
    "pressure": "сервер больше не задыхается", "net_health": "потерь в сети больше нет",
    "link": "связь между серверами в порядке", "traffic": "трафик в пределах тарифа",
    "kube_node": "ноды кластера снова готовы", "mem_leak": "память больше не растет",
    "mem_limit": "больше не упирается в лимит памяти",
    "listen_gone": "пропавшие порты снова слушают", "disk_slow": "диск больше не тормозит",
    "web_cert": "сертификаты снова в сроке", "web_config": "конфиги снова в порядке",
    "webfiles": "признаков взлома в файлах сайтов больше нет",
    "db_health": "базы снова в порядке", "ssh_risk": "SSH больше не в опасности",
    "access_risk": "лишних root и пустых паролей нет", "kube_restarts": "частых перезапусков нет",
    "docker_restarts": "частых перезапусков нет", "backup_restore": "проверка восстановления проходит",
    "backup_missing": "бэкап настроен", "backup_failed": "бэкап снова проходит",
    "backup_stale": "бэкап снова свежий", "backup_dump": "дампы снова свежие",
    "backup_dump_space": "дамп снова снимается", "backup_cron": "дамп-CronJob снова отрабатывает",
    "backup_custom": "свои бэкапы снова отрабатывают", "backup_repo": "репозитории снова в норме",
    "backup_rotation": "ротация снова чистит", "backup_lock": "локов больше нет",
    "backup_check": "проверка репозиториев снова проходит", "backup_prune": "чистка снова работает",
    "backup_mirror": "копии на резервном снова в порядке",
    "queue": "очередь разобрана", "unit_loop": "больше не перезапускается",
    "docker_loop": "больше не перезапускается", "docker_down": "контейнер снова работает",
}

# Короткая версия у модулей: (модуль, функция). Функция получает список из problems() (ctx["items"])
_MODULE = {
    "workers": ("workers", "tg_short"), "cron_stuck": ("cron_watch", "tg_short"),
    "log_errors": ("log_errors", "tg_short"), "queue_stuck": ("queue_watch", "tg_short"),
    "queue_failed": ("queue_watch", "tg_short_failed"), "fpm": ("fpm_pools", "tg_short"), "vpn_torrent": ("vpn_flow", "tg_short_torrent"),
    "vpn_heavy": ("vpn_flow", "tg_short_heavy"), "gitlab": ("gitlab", "tg_short"),
    "autoban": ("autoban", "tg_short"),
    "db_health": ("db_health", "tg_short"), "backup_custom": ("custom_backups", "tg_short"),
    "backup_restore": ("backup_verify", "tg_short"), "exposed": ("open_ports", "tg_short_exposed"),
    "port_new": ("open_ports", "tg_short_new"), "cf_realip": ("cf_realip", "tg_short"), "access_change": ("changes", "tg_short"),
    "access_risk": ("access_audit", "tg_short"), "ssh_risk": ("ssh_audit", "tg_short"),
    "docker_sock": ("docker_exposure", "tg_short"), "docker_risk": ("docker_risk", "tg_short"),
    "listen_gone": ("listen_watch", "tg_short"), "link": ("netlinks", "tg_short"),
    "traffic": ("traffic", "tg_short"), "mem_leak": ("mem_trend", "tg_short"),
    "mem_limit": ("mem_limit", "tg_short"),
    "docker_restarts": ("restarts", "tg_short"), "kube_restarts": ("restarts", "tg_short"),
    "gpu": ("gpu", "tg_short"), "gpu_heat": ("gpu", "tg_short_heat"), "llm": ("gpu", "tg_short_llm"),
    "web_cert": ("web_certs", "tg_short"), "web_config": ("web_config", "tg_short"),
    "webfiles": ("webfiles", "tg_short"),
    "disk_forecast": ("disk_forecast", "tg_short"), "backup_growth": ("backup_growth", "tg_short"),
    "oom": ("oom", "tg_short"),
}


def _from_module(key: str, items):
    mod_name, fn_name = _MODULE[key]
    try:
        mod = importlib.import_module(f"app.{mod_name}")
    except ImportError:
        return None
    fn = getattr(mod, fn_name, None)
    if fn is None:
        return None
    return fn(items)


def _fresh_first(items: list, fresh) -> list:
    """Новое при открытом алерте - только оно: о прежнем уже сказали, в чате оно было повтором
    ("Еще: ..." с теми же именами в каждом сообщении). В полном тексте прежнее - строкой "и еще N,
    о них уже сообщали". Нового среди items не нашлось (ключи другие) - все, как есть."""
    if not fresh or not isinstance(items, list):
        return items

    def is_new(x) -> bool:
        if isinstance(x, dict):
            return str(x.get("key") or x.get("unit") or "") in fresh
        if isinstance(x, (tuple, list)):
            return any(str(v) in fresh for v in x if isinstance(v, str))
        return str(x) in fresh
    new = [x for x in items if is_new(x)]
    return new or items


def _cores(pct: float) -> str:
    c = pct / 100
    if c < 1.5:
        return "1 ядро"
    n = round(c)
    return f"{n} {'ядра' if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else 'ядер' if n % 10 != 1 or n % 100 == 11 else 'ядро'}"


def top_proc(rep: dict, kind: str) -> str:
    """Главный пожиратель: "ffmpeg (12 ядер)" или "php-fpm7.4 (6гб)". Одноименные складываются."""
    rows = rep.get("top_cpu" if kind == "cpu" else "top_mem") or []
    by: dict[str, float] = {}
    for p in rows:
        if not isinstance(p, dict):
            continue
        name = str(p.get("comm") or "").strip()
        if not name:
            continue
        if kind == "cpu":
            by[name] = by.get(name, 0.0) + float(p.get("cpu") or 0)
        else:
            by[name] = by.get(name, 0.0) + max(float(p.get("rss") or 0) - float(p.get("shared") or 0), 0.0)
    if not by:
        return ""
    name, v = max(by.items(), key=lambda kv: kv[1])
    if v <= 0:
        return ""
    return f"{name} ({_cores(v)})" if kind == "cpu" else f"{name} ({tg_fmt.size(v)})"


def _pod_short(ns: str, name: str, ctrl: str = "") -> str:
    return f"{ns}/{podnames.workload(name, ctrl)}" if ns else podnames.workload(name, ctrl)


def _grouped(names: list[str]) -> list[str]:
    """Одинаковые имена схлопнуть: "bot-send-worker x2"."""
    cnt: dict[str, int] = {}
    for n in names:
        cnt[n] = cnt.get(n, 0) + 1
    return [f"{n} x{c}" if c > 1 else n for n, c in cnt.items()]


def _kube_pods(items: list[dict]) -> tuple[str, list[str]]:
    by: dict[str, list[str]] = {}
    for p in items:
        w = _pod_short(str(p.get("ns") or ""), str(p.get("name") or "?"), str(p.get("ctrl") or ""))
        by.setdefault(w, []).append(str(p.get("reason") or p.get("phase") or ""))
    parts = []
    for w, whys in by.items():
        why = next((x for x in whys if x), "")
        parts.append(f"{w}{f' x{len(whys)}' if len(whys) > 1 else ''}" + (f" ({why})" if why else ""))
    what = "Не поднимается под" if len(items) == 1 else "Не поднимаются поды"
    return what, [tg_fmt.names(parts, 2)]


def _units(items: list[dict], why_fn) -> tuple[str, list[str]]:
    if len(items) == 1:
        u = items[0]
        return f"Упал {u.get('unit')}", [why_fn(u)]
    return "Упали юниты", [tg_fmt.names((f"{u.get('unit')} ({why_fn(u)})" for u in items), 2)]


_PRESSURE_WHAT = {
    "mem": "Серверу не хватает памяти", "thrash": "Кончилась память", "io": "Сервер ждет диск", "dstate": "Процессы зависли",
    "fds": "Кончаются файловые дескрипторы", "cpu": "Серверу не хватает CPU",
}


def _dump_name(engine: str) -> str:
    """"mysql@k8s.default.deploy.wordpress-mariadb" -> "wordpress-mariadb", "pg@feed" -> "feed"."""
    tail = engine.partition("@")[2] or engine
    return tail.rsplit(".", 1)[-1] if tail.startswith("k8s.") else tail


def _split_names(text: str) -> list[str]:
    """"a (почему), b (почему); c" -> ["a", "b", "c"]."""
    out = []
    for part in re.split(r";\s+|,\s+(?![^()]*\))", str(text or "")):
        part = part.strip()
        if not part or part.startswith("и еще") or part.startswith("и ещё") or re.match(r"^\d+ шт\.", part):
            continue
        out.append(part.split(" (")[0].strip())
    return out


# Сырая ошибка в скобках: без кириллицы, кроме "(HTTP 503)". Обрезанная (без закрывающей скобки
# в конце) - тоже: "(Get "https://example.com": dial tcp 127.0.0.1:443: c" в чате не нужна
_RAW_ERR = re.compile(r"\s*\((?![^()]*[а-яА-ЯёЁ])(?!HTTP \d{3}\))[^()]*(?:\)+|$)")
_GO_URL = re.compile(r'^(?:Get|Post|Head) "[^"]*":\s*')
_MULTI_IP = re.compile(r"^(\d+)/(\d+) адресов OK; ")
_DN = re.compile(r"(\d+) дн\.?")


def _days_words(text: str) -> str:
    return _DN.sub(lambda m: tg_fmt.days(int(m.group(1))), text)


SITE_RECOVERY = {"recovery": "снова отвечает", "recstable": "снова отвечает стабильно", "locrec": "открывается отовсюду",
                 "flakyrec": "снова отвечает стабильно", "tamperrec": "признаков взлома больше нет"}


# как агент проверил сайт изнутри (app.checks.via_text): в обход шлюза или прокси, через адрес
_VIA = re.compile(r"(?:в обход \S+|^через адрес \S+)$")


def _reason(text: str) -> str:
    """Причина одной строкой: без сырой ошибки Go/httpx в скобках и без "Get "<адрес>": "."""
    why = _GO_URL.sub("", str(text or "").strip())
    while True:
        cut = _RAW_ERR.sub("", why)
        if cut == why:
            break
        why = cut
    return why.strip() or str(text or "").strip()


def _multi_ip(rest: str, ok: int, total: int) -> list[str]:
    """"1.2.3.4 — сервер отклонил соединение — порт закрыт (...); 5.6.7.8 — ..." -> сколько адресов
    не отвечает и первый из них с причиной."""
    items = [x for x in rest.split("; ") if x.strip()]
    first = items[0].split(" — ") if items else []
    facts = [f"не отвечает {total - ok} из {total} адресов"]
    if len(first) > 1:
        facts.append(f"{first[0].strip()}: {_reason(first[1])}")
    return facts


def site(kind: str, status: str, msg: str) -> tuple[str, str, list[str]] | str:
    """Короткий сайтовый алерт: (эмодзи, что случилось, конкретика) или строка отбоя.

    bad - "Не отвечает" + причина без сырой ошибки Go; через раз - последняя ошибка; локации -
    откуда не открывается; сроки - сколько осталось, без "(мониторов на нем: 9)"."""
    msg = str(msg or "")
    if kind in SITE_RECOVERY:
        return SITE_RECOVERY[kind]
    if kind == "bad":
        multi = _MULTI_IP.match(msg)
        if multi:
            facts = _multi_ip(msg[multi.end():], int(multi.group(1)), int(multi.group(2)))
            return ("⚠️🌐", "Тормозит", facts[1:]) if status == "degraded" else ("🔴🌐", "Не отвечает", facts)
        parts = [p.strip() for p in msg.split(" — ") if p.strip()]
        # путь проверки изнутри после " · " ("... · контейнер app-1:80 в обход caddy"): в чат тоже
        head, dot, way = parts[-1].rpartition(" · ") if parts else ("", "", "")
        if dot and _VIA.search(way):
            parts = parts[:-1] + [head, way]
        why = _reason(parts[0]) if parts else ""
        m = re.search(r"медленно: (\d+) мс", why)
        if m:
            why = re.sub(r",?\s*медленно: \d+ мс", "", why).strip()
            why = (why + ", " if why else "") + f"ответ за {tg_fmt.dur(int(m.group(1)) / 1000)}"
        # После тире - пояснения и советы ("так бывает у Kubernetes, проверяйте из панели"): они в
        # панели и в истории алертов. В чат кроме причины только то, через что проверяли
        facts = [why] + [p for p in parts[1:] if _VIA.search(p)]
        if status == "degraded":
            return "⚠️🌐", "Тормозит", facts
        return "🔴🌐", "Не отвечает", facts
    if kind == "tamper":
        # app/tamper.alert_text: что случилось и строки конкретики через перевод строки
        lines = [x for x in msg.split("\n") if x.strip()]
        return "🚨🕵️", lines[0] if lines else "Похоже на взлом", lines[1:3]
    if kind == "flaky":
        last = msg.partition("последняя ошибка: ")[2]
        return "🟠🌐", "Отвечает через раз", [_reason(last.split(" — ")[0])] if last else []
    if kind == "locpart":
        bad = [ln for ln in msg.split("\n") if ln.startswith("🔴")]
        good = [ln for ln in msg.split("\n") if ln.startswith("🟢")]

        def city(ln: str) -> str:
            place = ln[1:].split(" — ")[0].strip()
            return place.split(", ")[-1] if ", " in place else place
        what = "Недоступен: " + tg_fmt.names([city(x) for x in bad], 2) if bad else "Открывается не отовсюду"
        return "🟠🌍", what, [tg_fmt.fact("Доступен", tg_fmt.names([city(x) for x in good], 2))] if good else []
    if kind in ("ssl", "domain"):
        text = re.sub(r"\s*\(мониторов на нем: \d+\)", "", tg_fmt.clean(msg))
        text = re.sub(r"^регистрация домена (?:\S+ )?(?=истек|продлен)", "домен ", text)
        text = text.replace("истекла", "истек").replace("продлена", "продлен")
        expired = bool(re.search(r"\bистек\b", text))
        return ("🔴" if expired else "⚠️") + ("🔐" if kind == "ssl" else "🌐"), _days_words(text), []
    return "🔴🌐", tg_fmt.split_detail(msg)[0], tg_fmt.split_detail(msg)[1]


_CRON_WHAT = {"fail": "Упал CronJob", "nostart": "CronJob не запускается", "hang": "Завис CronJob"}
_CRON_DAYS = re.compile(r"(\d+(?:\.\d+)?) дн(?!\w)")


def _kube_cron(ctx: dict) -> tuple[str, list[str]]:
    """Один CronJob: упал (причина и строка из конца лога, если агент ее прислал), не запускается
    (сколько и почему) или завис. Несколько - именами, разбор в панели."""
    items = [x for x in ctx.get("items") or [] if isinstance(x, dict) and x.get("name")]
    if not items:  # контекст старого формата
        names = _split_names(ctx.get("jobs") or "")
        return ("Не отработал CronJob" if len(names) == 1 else "Не отрабатывают CronJob"), [tg_fmt.names(names)]
    if len(items) > 1:
        return "Не отрабатывают CronJob", [tg_fmt.names([x["name"] for x in items])]
    it = items[0]
    detail = _CRON_DAYS.sub(lambda m: tg_fmt.days(int(float(m.group(1)))), str(it.get("detail") or ""))
    facts = [f"{it['name']} - {detail}" if detail else str(it["name"])]
    if it.get("log"):
        facts.append(tg_fmt.fact("Последняя строка лога", str(it["log"])))
    return _CRON_WHAT.get(str(it.get("kind") or ""), "Не отработал CronJob"), facts


def short(key: str, ctx: dict, s, detail: str = "", level: int = 1,
          now: datetime | None = None) -> tuple[str, list[str]]:
    """(что случилось, конкретика) для вида key. detail - полный текст: из него запасной разбор."""
    try:
        out = _short(key, ctx, s, level)
    except Exception:  # noqa: BLE001 - короткий текст не повод терять алерт
        out = None
    if out is None:
        out = tg_fmt.split_detail(detail)
    what, facts = out
    if not str(what or "").strip():
        # текста нет (алерт ушел, когда беда уже стихала): хотя бы что это за алерт
        from app import settings_store
        what = settings_store.SERVER_ALERT_KINDS.get(key, (key,))[0]
    return tg_fmt.cap(what), [f for f in facts if f]


def _avg_tail(ctx: dict) -> str:
    """CPU, RAM, температуры и conntrack: в value среднее за выдержку (collector ставит value_now)"""
    avg = ctx.get("avg")
    if "value_now" not in ctx or not isinstance(avg, dict) or not avg.get("min"):
        return ""
    return f" в среднем за {int(avg['min'])} мин"


def _short(key: str, ctx: dict, s, level: int):
    rep = (getattr(s, "last_report", None) or {}) if s is not None else {}
    items = ctx.get("items")
    if isinstance(items, list):
        items = _fresh_first(items, ctx.get("fresh"))
    if key in _MODULE and items is not None:
        return _from_module(key, items)
    v = ctx.get("value")
    if key == "offline":
        # агент предупредил об остановке и сервер не вернулся (app/going_down.py)
        return going_down.short(ctx.get("down")) or "Не на связи", []
    if key == "reboot":
        return "Перезагрузился", []
    if key == "cpu":
        top = top_proc(rep, "cpu")
        return f"CPU загружен на {v}%{_avg_tail(ctx)}", [tg_fmt.fact("Грузит", top)] if top else []
    if key == "mem":
        top = top_proc(rep, "mem")
        return f"Память занята на {v}%{_avg_tail(ctx)}", [tg_fmt.fact("Ест", top)] if top else []
    if key in ("disk", "inode"):
        mount = tg_fmt.field(ctx.get("mount") or "")  # от агента: в одну строку и не длиннее 200
        where = f" {mount}" if mount else ""
        what = (f"Диск{where} заполнен на {v}%" if key == "disk" else f"Кончаются inode на{where or ' диске'}")
        if ctx.get("fast"):
            what = (f"Диск{where} быстро заполняется, уже {v}%" if key == "disk"
                    else f"Быстро кончаются inode на{where or ' диске'}")
        top = ctx.get("top") or ""
        return what, [tg_fmt.fact("Больше всех файлов" if key == "inode" else "Занимает", top)] if top else []
    if key == "temp":
        return f"CPU греется до {v}°C{_avg_tail(ctx)}", []
    if key == "throttle":
        return "CPU сбрасывает частоту от перегрева", []
    if key == "disktemp":
        return f"Диск греется до {v}°C{_avg_tail(ctx)}", []
    if key == "conntrack":
        return f"Таблица conntrack заполнена на {v}%{_avg_tail(ctx)}", []
    if key == "clock":
        return f"Часы ушли на {v}", []
    if key == "db_conn":
        return f"Кончаются подключения к {ctx.get('engine') or 'СУБД'}", [f"занято {ctx.get('used')} из {ctx.get('limit')}"]
    if key == "dns":
        d = str(ctx.get("details") or "")
        m = re.search(r"резолвится (\d+) мс", d)
        if m:
            return "DNS тормозит", [f"ответ за {tg_fmt.dur(int(m.group(1)) / 1000)}"]
        m = re.search(r"не резолвится \(([^)]+)\)", d)
        if m:
            return "DNS не отвечает", [m.group(1)] if m.group(1) != "нет ответа" else []
        ms = [int(x) for x in re.findall(r"резолверы:.*?(\d+) мс", d)] or [int(x) for x in re.findall(r"(\d+) мс", d)]
        if ms:
            return "DNS тормозит", [f"ответ за {tg_fmt.dur(min(ms) / 1000)}"]
        return "DNS не отвечает", []
    if key == "kube_expiry":
        what = str(ctx.get("what") or "срок")
        days = ctx.get("days")
        if days is None:
            return None
        days = float(days)
        left = round(days)
        when = (f"истек {tg_fmt.days(abs(left) or 1)} назад" if days < 0
                else "истекает сегодня" if left < 1 else f"истекает через {tg_fmt.days(left)}")
        facts = [str(ctx.get("where") or "")]
        if ctx.get("unused"):
            facts.append("не используется ни одним ingress")
        return f"{tg_fmt.cap(what)} {when}", facts
    if key == "flux_down":
        reason = re.sub(r"\s*\([A-Za-z]+\)$", "", str(ctx.get("reason") or ""))
        return "Flux не выкатывает", [f"{ctx.get('where')}: {reason}" if ctx.get("where") else reason]
    if key == "flux_stale":
        return "Недоступен репозиторий чартов", [str(ctx.get("where") or "")]
    if key == "web_5xx":
        what = f"{tg_fmt.pct(float(v or 0))} запросов с ошибкой 5xx"
        w = ctx.get("where_raw") or {}
        logs = [x for x in w.get("logs") or [] if x[1] > 0]
        codes = sorted((w.get("codes") or {}).items(), key=lambda kv: -kv[1])
        paths = sorted((w.get("paths") or {}).items(), key=lambda kv: -kv[1])
        facts = []
        why = ""
        ups = w.get("ups") or {}
        if ups:
            from app import web_upstream
            top_up = web_upstream.top(ups, 1)
            # ingress-nginx сам отвечает 503: у сервиса нет живых подов дольше выдержки. Это
            # и есть сбой, "N% запросов с ошибкой 5xx" тут только путает
            if top_up and top_up[0].get("k") == "noep":
                it = top_up[0]
                hosts = str(it.get("h") or "").split()
                return "Нет живых подов, nginx отвечает 503", (
                    [tg_fmt.fact("Сервис", str(it.get("s") or it.get("u") or ""))]
                    + ([tg_fmt.fact("Хост", tg_fmt.names(hosts))] if hosts else []))
            if top_up:
                why = web_upstream.KINDS.get(str(top_up[0].get("k")), "")
        if logs:
            label = str(logs[0][0]).split(" (")[0]
            label = label.rsplit("/", 1)[-1] if "/" in label else label
            label = podnames.workload(label)
            facts.append(label + (f": {codes[0][0]}" if codes else "") + (f", бэкенд {why}" if why else ""))
        elif codes:
            facts.append(f"код {codes[0][0]}")
        if paths:
            facts.append(tg_fmt.fact("Чаще всего", str(paths[0][0])))
        return what, facts
    if key == "kube_pod":
        return _kube_pods(items or []) if items else None
    if key == "disk_health":
        probs = [p for p in items or [] if p[2]]
        if not probs:
            return None
        what, facts = tg_fmt.split_detail(probs[0][2])
        what = re.sub(r"\s*\([^)]*\)", "", what)
        facts = [re.sub(r"^за сутки выросли ошибки - ", "за сутки выросли ", f) for f in facts]
        if len(probs) > 1:
            facts = facts[:1] + [f"и еще {len(probs) - 1}"]
        return what, facts
    if key == "units":
        if not items:
            return None
        from app.collector import unit_why
        return _units(items, unit_why)
    if key == "unit_loop":
        why = str(ctx.get("why") or "").replace(", последний раз: ", "")
        return f"{ctx.get('unit')} перезапускается по кругу", [why]
    if key == "cpu_spin":
        if not items:
            return None
        g = items[0]
        n = int(g.get("spin") or 0)
        owner = str(ctx.get("owner") or "")
        if owner.startswith("под "):
            ns, _, pod = owner[4:].rpartition("/")
            owner = "под " + _pod_short(ns, pod)
        return "Процессы крутятся впустую", [f"{g.get('comm') or '?'} x{n}, жгут {_cores(float(g.get('cpu') or 0))}"
                                              + (f" и еще {len(items) - 1}" if len(items) > 1 else ""),
                                              tg_fmt.cap(owner) if owner else ""]
    if key == "pressure":
        if not items:
            return None
        res = items[0][1]
        what = _PRESSURE_WHAT.get(res) or tg_fmt.split_detail(items[0][2])[0]
        facts = []
        if res == "thrash":
            # кончилась память на всем сервере (app/mem_starve.py): кто ее ест и насколько плохо
            what = mem_starve.what(rep)
            top = mem_starve.eaters(rep)
            facts.append(tg_fmt.fact("Едят", mem_starve.eaters_text(top[:2])) if top else "")
            sg = mem_starve.signs(rep)
            facts.append(tg_fmt.cap(mem_starve.numbers(sg)) if sg else "")
        elif res == "mem":
            top = top_proc(rep, "mem")
            facts.append(tg_fmt.fact("Ест", top) if top else "")
        elif res == "io":
            devs = [d for d in rep.get("disk_devs") or [] if isinstance(d, dict) and d.get("util") is not None]
            if devs:
                d = max(devs, key=lambda x: float(x.get("util") or 0))
                facts.append(f"{d.get('dev') or '?'} занят на {float(d['util']):.0f}%" if float(d["util"]) >= 50
                             else "локальные диски свободны, ждут сетевое хранилище")
        elif res == "fds":
            facts.append(tg_fmt.split_detail(items[0][2])[0])
        elif res == "dstate":
            stuck = re.findall(r"(?:завис |: |, )([\w.@:-]+) \(PID", items[0][2])
            if stuck:
                facts.append(tg_fmt.fact("Завис" if len(stuck) == 1 else "Зависли", tg_fmt.names(stuck)))
        return what, facts
    if key in ("net_health", "disk_slow", "kube_node"):
        texts = ([t for _l, _r, t in items] if key == "net_health" and items
                 else [ctx.get("detail") or ""] if key == "disk_slow" else list(items or []))
        if not texts or not texts[0]:
            return None
        what, facts = tg_fmt.split_detail(texts[0])
        if len(texts) > 1:
            facts = facts[:1] + [f"и еще {len(texts) - 1}"]
        return what, facts
    if key == "kube_cron":
        return _kube_cron(ctx)
    if key == "kube_job":
        jobs = items or []
        if not jobs:
            return None
        names = _grouped([re.sub(r"-\d{8,}$", "", f"{j.get('ns', '?')}/{j.get('name', '?')}") for j in jobs])
        reason = next((str(j.get("reason")) for j in jobs if j.get("reason")), "")
        return ("Упал Job" if len(jobs) == 1 else "Упали Job"), [tg_fmt.names(names, 2) + (f" ({reason})" if reason else "")]
    if key == "docker_down":
        why = str(ctx.get("why") or "")
        m = re.search(r"Bind for [\d.:\[\]]*:(\d+) failed: port is already allocated", why)
        if m:
            why = f"порт {m.group(1)} уже занят"
        elif len(why) > 70:
            why = why.split(": ")[-1][:70]
        return f"Не работает контейнер {ctx.get('container')}", [why] if why and why != "не работает" else []
    if key == "docker_loop":
        return f"{ctx.get('container')} перезапускается по кругу", []
    if key == "queue":
        from app import queue_watch
        return queue_watch.tg_short_depth(ctx.get("queue"), ctx.get("source"), v)
    if key == "backup_missing":
        why = str(ctx.get("why") or "").lstrip(". ")
        return "Бэкап не настроен", [why] if why else []
    if key == "backup_failed":
        return "Бэкап упал", [str(ctx.get("why") or "").lstrip(": ")]
    if key == "backup_stale":
        return f"Бэкапа нет {tg_fmt.days(max(int(float(ctx.get('days') or 0)), 1))}", []
    if key in ("backup_dump", "backup_dump_space"):
        names = [_dump_name(x.strip()) for x in str(ctx.get("engines") or "").split(",") if x.strip()]
        if key == "backup_dump_space":
            return "Дамп не снят, нет места", [f"{tg_fmt.names(names)}, свободно {ctx.get('free')}%"]
        return ("Не обновляется дамп базы" if len(names) == 1 else "Не обновляются дампы баз"), [tg_fmt.names(names)]
    if key == "backup_cron":
        names = _split_names(ctx.get("jobs") or "")
        return ("Упал дамп-CronJob" if len(names) == 1 else "Упали дамп-CronJob"), [tg_fmt.names(names)]
    if key == "backup_mirror":
        names = [str(x) for x in ctx.get("names") or []]
        return ("Копия на резервном требует внимания" if len(names) == 1 else "Копии на резервном требуют внимания"),             [tg_fmt.names(names)]
    if key in ("backup_repo", "backup_unmonitored", "backup_prune", "backup_rotation", "backup_lock",
               "backup_check"):
        names = ctx.get("names") or _split_names(ctx.get("repos") or "")
        one = len(names) == 1
        if key == "backup_repo":
            whys = [str(x) for x in ctx.get("whys") or []]
            late = whys and all(w.startswith("бэкап опоздал") for w in whys)
            head = whys[0].split(":")[0] if whys else ""
            if head.startswith("бэкап опоздал"):
                head = "Опаздывает бэкап"
            what = ("Опаздывает бэкап" if one else "Опаздывают бэкапы") if late else (
                tg_fmt.cap(_days_words(head)) if head else "Репозитории требуют внимания")
        else:
            what = {
                "backup_unmonitored": "Бэкап пишет нода без агента" if one else "Бэкап пишут ноды без агента",
                "backup_prune": "Не работает чистка репозитория" if one else "Не работает чистка репозиториев",
                "backup_rotation": "Не удаляются старые снапшоты",
                "backup_lock": "Висит лок больше суток",
                "backup_check": "Битый репозиторий" if one else "Битые репозитории",
            }[key]
        return what, [tg_fmt.names(names)]
    return None
