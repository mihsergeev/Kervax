"""Обновления и поддержка ОС: что сделать на ноде, а не что сломалось.

Агент 2.26 раз в час отдает os_upd: нужна ли перезагрузка и с каких пор, есть ли в /boot ядро
новее работающего, сколько обновлений и сколько из них безопасности, работают ли
автообновления, когда последний раз прошел apt update. Имя ОС (PRETTY_NAME) агент шлет давно,
по нему - срок поддержки дистрибутива.

Это не алерты: ни одно из этого не горит, но каждое со временем превращается в дыру. Поэтому
пункты идут в "Требует действий" на главной и в блок "Обновления ОС" карточки сервера.

У каждого пункта готовые шаги (fix): команды под эту ноду, после которых пункт уходит сам. Тексты
шагов - во фронте по id, здесь команды: они зависят от ноды (автообновления, контейнеры и сервисы,
которые не переживут перезагрузку, есть ли у дистрибутива платное продление).

Обновить ОС можно не всегда: старый сервер бэкапов, железо под софт, который на новой версии не
встанет. Тогда это решение человека "так и должно быть" (alert_mutes "osupd:<вид>"): пункт уходит
из "Требует действий", а в карточке виден приглушенным с кнопкой "вернуть". Не заглушенный алерт -
в "Заглушено" его нет, как и других решений.

Перезагрузку чаще откладывают, чем запрещают: "до нового" (alert_mutes "osupd:reboot@<отпечаток>",
ставится с главной галками по многим серверам или в карточке). Отпечаток - набор пакетов, ради которых
нужна перезагрузка, и время, когда о ней узнали (/run/reboot-required переписывается каждой такой
установкой). Новое ядро, новый пакет или перезагрузка и снова "нужна" - другой отпечаток, пункт
возвращается сам, а устаревшую заглушку сборщик убирает с записью в журнал (stale_mutes).

Так же "до нового" глушатся и остальные пункты (osupd:<вид>@<отпечаток>): конец поддержки - до смены
версии ОС или ее состояния (обычная поддержка кончилась, Ubuntu Pro подключен, кончается ESM), обновления
безопасности - до смены причины, почему они не ставятся (выключены, не установлены, не запускаются),
список пакетов - до следующего apt update. Ушел пункт - заглушку тоже снимает сборщик.

Обновлен только dbus - не "нужна перезагрузка", а "при случае" (soft): dbus на ходу не
перезапускают (от него зависят systemd-logind и сессии), Ubuntu поэтому и просит перезагрузку, а
работающий старый dbus сам по себе ничего не ломает. Такие серверы годами висели в одной группе с
непримененными ядрами и libc6 и прятали их.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone

from app import reboot_survive

MUTE_PREFIX = "osupd:"
# заглушка перезагрузки до нового: osupd:reboot@<отпечаток>; у других видов так же, osupd:<вид>@
REBOOT_NEW = MUTE_PREFIX + "reboot@"
# пакеты, после которых перезагрузка нужна "при случае": демон, который на ходу не перезапускают
_SOFT_RX = re.compile(r"^(dbus|libdbus)")
# пакеты ядра в reboot-required.pkgs: версия после префикса (linux-image-6.8.0-87-generic)
_KERNEL_RX = re.compile(r"^linux-(?:signed-)?(?:image|modules)(?:-extra)?(?:-unsigned)?-(\d\S*)$")
# служебные: перезагрузку просят вместе с ядром, сами по себе ничего не значат
_QUIET_PKGS = {"linux-base"}

# Конец поддержки: у Ubuntu LTS - обычной (дальше обновления безопасности только по подписке
# Ubuntu Pro), у Debian - вместе с LTS (Debian LTS бесплатный), у RHEL-семейства - полной.
_EOL: list[tuple[str, str, date, bool]] = [
    # (регулярка по PRETTY_NAME, название, дата, есть ли платное продление)
    (r"^Ubuntu 14\.04", "Ubuntu 14.04", date(2019, 4, 30), True),
    (r"^Ubuntu 16\.04", "Ubuntu 16.04", date(2021, 4, 30), True),
    (r"^Ubuntu 18\.04", "Ubuntu 18.04", date(2023, 5, 31), True),
    (r"^Ubuntu 20\.04", "Ubuntu 20.04", date(2025, 5, 31), True),
    (r"^Ubuntu 22\.04", "Ubuntu 22.04", date(2027, 6, 1), True),
    (r"^Ubuntu 24\.04", "Ubuntu 24.04", date(2029, 5, 31), True),
    (r"^Ubuntu 26\.04", "Ubuntu 26.04", date(2031, 5, 31), True),
    (r"^Ubuntu 23\.10", "Ubuntu 23.10", date(2024, 7, 11), False),
    (r"^Ubuntu 24\.10", "Ubuntu 24.10", date(2025, 7, 10), False),
    (r"^Ubuntu 25\.04", "Ubuntu 25.04", date(2026, 1, 15), False),
    (r"^Ubuntu 25\.10", "Ubuntu 25.10", date(2026, 7, 9), False),
    (r"^Debian GNU/Linux 9\b", "Debian 9", date(2022, 6, 30), False),
    (r"^Debian GNU/Linux 10\b", "Debian 10", date(2024, 6, 30), False),
    (r"^Debian GNU/Linux 11\b", "Debian 11", date(2026, 8, 31), False),
    (r"^Debian GNU/Linux 12\b", "Debian 12", date(2028, 6, 30), False),
    (r"^Debian GNU/Linux 13\b", "Debian 13", date(2030, 6, 30), False),
    (r"^CentOS Linux 7\b", "CentOS 7", date(2024, 6, 30), False),
    (r"^CentOS Linux 8\b", "CentOS 8", date(2021, 12, 31), False),
    (r"^CentOS Stream 8\b", "CentOS Stream 8", date(2024, 5, 31), False),
    (r"^CentOS Stream 9\b", "CentOS Stream 9", date(2027, 5, 31), False),
    (r"^(AlmaLinux|Rocky Linux|Red Hat Enterprise Linux)[^0-9]*8\.", "EL 8", date(2029, 5, 31), False),
    (r"^(AlmaLinux|Rocky Linux|Red Hat Enterprise Linux)[^0-9]*9\.", "EL 9", date(2032, 5, 31), False),
]
_EOL_RX = [(re.compile(rx), name, d, paid) for rx, name, d, paid in _EOL]
# Ubuntu Pro (ESM): до какого числа после обычной поддержки выходят исправления безопасности
_ESM = {"Ubuntu 16.04": date(2026, 4, 30), "Ubuntu 18.04": date(2028, 4, 30), "Ubuntu 20.04": date(2030, 4, 30),
        "Ubuntu 22.04": date(2032, 4, 30), "Ubuntu 24.04": date(2034, 4, 30), "Ubuntu 26.04": date(2036, 4, 30)}
EOL_WARN_DAYS = 180  # за полгода - пора планировать переезд
REBOOT_AFTER_DAYS = 7  # перезагрузку после обновления ядра откладывают на ночь-другую, не на месяц
REBOOT_SOFT_AFTER_DAYS = 30  # только dbus: срочности нет, месяц до ближайшего окна
UNATT_STALE_DAYS = 3
APT_STALE_DAYS = 14


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _days(n: int) -> str:
    return f"{n} {_plural(n, 'день', 'дня', 'дней')}"


def eol_of(os_name: str) -> tuple[str, date, bool] | None:
    """(название, конец поддержки, есть ли платное продление) или None, если ОС не знаем."""
    for rx, name, d, paid in _EOL_RX:
        if rx.search(os_name or ""):
            if name.startswith("EL "):
                name = (os_name.split(" (")[0] or name).strip()
            return name, d, paid
    return None


def advice(rep: dict, now: datetime, mutes: list[str] | None = None) -> list[dict]:
    """Что сделать с ОС ноды: [{"kind", "level", "text", "key", "muted", "fix"}], уровень 2 - уже
    дыра, 1 - пора; muted - человек решил, что так и должно быть (ключ key в alert_mutes); fix -
    шаги [{"id", "cmd"?, "arg"?, "alt"?}], alt - другой путь вместо шагов выше.
    new_key - заглушка до нового (osupd:<вид>@<отпечаток>), muted_new - заглушена ею, а не решением
    "обновить нельзя"; soft - обновлен только dbus, перезагрузить при случае."""
    out = _advice(rep, now)
    got = set(mutes or [])
    for x in out:
        x["key"] = MUTE_PREFIX + x["kind"]
        x["muted"] = x["key"] in got
        x["new_key"] = _new_key(x["kind"], rep, x.pop("sig", ""))
        x["muted_new"] = not x["muted"] and x["new_key"] in got
        x["muted"] = x["muted"] or x["muted_new"]
        x["fix"] = _fix(x["kind"], rep, now, got)
    return out


def _new_key(kind: str, rep: dict, sig: str) -> str:
    if kind == "reboot":
        return REBOOT_NEW + reboot_sig(rep)
    return f"{MUTE_PREFIX}{kind}@{hashlib.sha1(sig.encode()).hexdigest()[:10]}"


def _upd(rep: dict) -> dict:
    u = (rep or {}).get("os_upd")
    return u if isinstance(u, dict) else {}


def _pkgs(u: dict) -> list[str]:
    return [str(p) for p in u.get("reboot_pkgs") or [] if p]


def _ver(v: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", v)]


def _kshort(v: str) -> str:
    return re.sub(r"-generic$", "", v)


def _list(xs: list[str], n: int = 3) -> str:
    return ", ".join(xs[:n]) + (f" и еще {len(xs) - n}" if len(xs) > n else "")


def reboot_parts(u: dict) -> list[str]:
    """Зачем перезагрузка, по-человечески: ядро (что работает и что заработает), библиотеки
    (старые в памяти процессов), dbus (на ходу не перезапустить), остальное как есть. linux-base не
    показываем: служебный пакет, его ставят вместе с ядром"""
    pkgs = _pkgs(u)
    parts: list[str] = []
    kernels = [m.group(1) for p in pkgs if (m := _KERNEL_RX.match(p))]
    if u.get("kernel_new"):
        kernels.append(str(u["kernel_new"]))
    run = str(u.get("kernel") or "")
    if kernels:
        new = max(kernels, key=_ver)
        if not run:
            parts.append(f"установлено ядро {_kshort(new)} - заработает после перезагрузки")
        elif _ver(new) > _ver(run):
            parts.append(f"работает ядро {_kshort(run)}, установлено {_kshort(new)} - новое заработает после перезагрузки")
    rest = [p for p in pkgs if not _KERNEL_RX.match(p) and p not in _QUIET_PKGS]
    libs = [p for p in rest if p.startswith("lib") and not _SOFT_RX.match(p)]
    if libs:
        one = len(libs) == 1
        parts.append(f"{'обновлена' if one else 'обновлены'} {_list(libs)} - процессы держат "
                     f"{'старую' if one else 'старые'} в памяти, нужна перезагрузка или перезапуск служб")
    if any(_SOFT_RX.match(p) for p in rest):
        parts.append("обновлен dbus, перезапустить его без перезагрузки нельзя")
    other = [p for p in rest if p not in libs and not _SOFT_RX.match(p)]
    if other:
        parts.append(f"{'обновлен' if len(other) == 1 else 'обновлены'} {_list(other)} - нужна перезагрузка")
    return parts


def soft_reboot(u: dict) -> bool:
    """Перезагрузка нужна только ради dbus: при случае, а не срочно"""
    pkgs = _pkgs(u)
    return bool(u.get("reboot")) and bool(pkgs) and all(_SOFT_RX.match(p) for p in pkgs)


def reboot_sig(rep: dict) -> str:
    """Отпечаток того, ради чего нужна перезагрузка; "" - не нужна. Набор пакетов и время
    reboot-required: новая установка такого пакета переписывает файл, перезагрузка его стирает"""
    u = _upd(rep)
    if u.get("reboot"):
        try:
            ts = int(float(u.get("reboot_ts") or 0))
        except (TypeError, ValueError):
            ts = 0
        base = f"r{ts}:" + ",".join(sorted(set(_pkgs(u))))
    elif u.get("kernel_new"):
        base = f"k{u['kernel_new']}<{u.get('kernel') or ''}"
    else:
        return ""
    return hashlib.sha1(base.encode()).hexdigest()[:10]


_STALE_WHY = {"eol": "заглушка конца поддержки ОС снята", "security": "заглушка обновлений безопасности снята",
              "apt": "заглушка списка пакетов снята"}


def stale_mutes(rep: dict, mutes: list[str] | None, now: datetime | None = None) -> list[tuple[str, str]]:
    """Заглушки до нового, которые уже не про то, что на ноде: [(ключ, почему снята)]. Перезагрузка,
    обновления и apt без os_upd в отчете (старый агент, нет данных) не снимаются, конец поддержки - без
    имени ОС."""
    out = _stale_other(rep, mutes, now or datetime.now(timezone.utc))
    old = [str(m) for m in mutes or [] if str(m).startswith(REBOOT_NEW)]
    u = _upd(rep)
    if not old or not u:
        return out
    sig = reboot_sig(rep)
    if not sig:
        why = "перезагрузка больше не нужна"
    elif u.get("reboot"):
        pkgs = [p for p in _pkgs(u) if p not in _QUIET_PKGS][:4]
        why = "после заглушки снова нужна перезагрузка" + (f" (обновлены {', '.join(pkgs)})" if pkgs else "")
    else:
        why = f"в /boot новое ядро {u['kernel_new']}"
    return out + [(m, why) for m in old if m != REBOOT_NEW + sig]


def _stale_other(rep: dict, mutes: list[str] | None, now: datetime) -> list[tuple[str, str]]:
    old = [str(m) for m in mutes or [] if str(m).startswith(MUTE_PREFIX) and "@" in str(m)
           and not str(m).startswith(REBOOT_NEW)]
    if not old:
        return []
    cur = {x["kind"]: _new_key(x["kind"], rep, x.get("sig", "")) for x in _advice(rep, now)}
    out = []
    for m in old:
        kind = m[len(MUTE_PREFIX):].split("@", 1)[0]
        if kind not in _STALE_WHY or m == cur.get(kind):
            continue
        if kind == "eol" and not rep.get("os") or kind != "eol" and not _upd(rep):
            continue
        if kind == "eol":
            why = f"пункт ушел, ОС {rep['os']}" if kind not in cur else "у поддержки ОС новое состояние"
        elif kind == "security":
            why = "обновления безопасности установлены" if kind not in cur else "у обновлений безопасности новая причина"
        else:
            why = "список пакетов обновлен"
        out.append((m, f"{_STALE_WHY[kind]}: {why}"))
    return out


APT_UPDATE = "sudo apt update"
UNATT_INSTALL = "sudo apt install -y unattended-upgrades"
# две строки, а не одна: агент ищет Unattended-Upgrade в начале строки; повторный запуск дает тот же файл
UNATT_ENABLE = ("echo 'APT::Periodic::Update-Package-Lists \"1\";' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades\n"
                "echo 'APT::Periodic::Unattended-Upgrade \"1\";' | sudo tee -a /etc/apt/apt.conf.d/20auto-upgrades")
APT_DAILY = "echo 'APT::Periodic::Update-Package-Lists \"1\";' | sudo tee -a /etc/apt/apt.conf.d/20auto-upgrades"
UNATT_RUN = "sudo unattended-upgrade -v"
UNATT_CHECK = ("systemctl list-timers apt-daily.timer apt-daily-upgrade.timer\n"
               "sudo tail -n 30 /var/log/unattended-upgrades/unattended-upgrades.log")


def _fix(kind: str, rep: dict, now: datetime, mutes: set[str]) -> list[dict]:
    """Шаги для пункта kind: что выполнить на ноде, чтобы пункт ушел."""
    u = rep.get("os_upd") if isinstance(rep.get("os_upd"), dict) else {}
    unatt = str(u.get("unatt") or "")
    if kind == "apt":
        steps = [{"id": "apt_update", "cmd": APT_UPDATE}]
        if unatt != "on":
            # только список пакетов каждый день, без автоустановки; -a: не затирает включенное шагом
            # про безопасность
            steps.append({"id": "apt_daily", "cmd": APT_DAILY})
        return steps
    if kind == "security":
        if unatt == "on":
            return [{"id": "unatt_check", "cmd": UNATT_CHECK}, {"id": "unatt_run", "cmd": UNATT_RUN}]
        steps = [{"id": "apt_update", "cmd": APT_UPDATE}]
        if unatt != "off":
            steps.append({"id": "unatt_install", "cmd": UNATT_INSTALL})
        return steps + [{"id": "unatt_enable", "cmd": UNATT_ENABLE}, {"id": "unatt_run", "cmd": UNATT_RUN}]
    if kind == "reboot":
        # только dbus: почему не "перезапустить dbus", а перезагрузка при случае
        steps = [{"id": "reboot_soft"}] if soft_reboot(u) else []
        if reboot_survive.MUTE_ALL not in mutes:
            dc = [c["name"] for c in reboot_survive.containers(rep) if reboot_survive.MUTE_PREFIX + c["name"] not in mutes]
            un = [x["unit"] for x in reboot_survive.units(rep, now) if reboot_survive.MUTE_PREFIX + x["unit"] not in mutes]
            if dc:
                steps.append({"id": "noboot_docker", "cmd": "sudo docker update --restart unless-stopped " + " ".join(dc)})
            if un:
                steps.append({"id": "noboot_units", "cmd": "sudo systemctl enable " + " ".join(un)})
        return steps + [{"id": "reboot", "cmd": "sudo reboot"}]
    if kind == "eol":
        os_name = str(rep.get("os") or "")
        eol = eol_of(os_name)
        name = eol[0] if eol else ""
        today = now.astimezone(timezone.utc).date()
        esm = _ESM.get(name)
        if os_name.startswith("Ubuntu"):
            steps = []
            if eol and eol[2] and esm and esm >= today and not u.get("esm") and eol[1] < today:
                steps.append({"id": "pro_attach", "cmd": "sudo pro attach <token>", "arg": esm.strftime("%d.%m.%Y")})
                if unatt == "none":
                    steps.append({"id": "unatt_install", "cmd": UNATT_INSTALL})
                steps.append({"id": "pro_run", "cmd": APT_UPDATE + "\n" + UNATT_RUN})
            steps.append({"id": "release_upgrade", "cmd": "sudo do-release-upgrade", "alt": bool(steps)})
            return steps
        m = re.match(r"Debian (\d+)$", name)
        if m:
            return [{"id": "debian_upgrade", "arg": str(int(m.group(1)) + 1)}]
        return [{"id": "el_upgrade"}]
    return []


def _advice(rep: dict, now: datetime) -> list[dict]:
    out: list[dict] = []
    today = now.astimezone(timezone.utc).date()
    eol = eol_of(str(rep.get("os") or ""))
    u = rep.get("os_upd") if isinstance(rep.get("os_upd"), dict) else None
    if eol:
        name, end, paid = eol
        left = (end - today).days
        when = end.strftime("%d.%m.%Y")
        esm = _ESM.get(name)
        if paid and esm and esm >= today and (u or {}).get("esm"):
            # Ubuntu Pro подключен (агент 2.39 видит репозиторий ESM): срок - конец ESM
            left_e = (esm - today).days
            if left_e <= EOL_WARN_DAYS:
                out.append({"kind": "eol", "level": 1, "sig": f"{name}:esm-soon", "text": (
                    f"{name}: исправления Ubuntu Pro (ESM) кончатся {esm.strftime('%d.%m.%Y')}, через {_days(left_e)}")})
        elif left < 0 and paid and esm and esm >= today:
            # что делать, если переезд невозможен: ESM в Ubuntu Pro (pro attach <токен>)
            out.append({"kind": "eol", "level": 2, "sig": f"{name}:ended-esm", "text": (
                f"{name}: обычная поддержка кончилась {when}, дальше Ubuntu Pro (ESM до {esm.strftime('%d.%m.%Y')}) "
                f"или переезд на новую LTS")})
        elif left < 0:
            out.append({"kind": "eol", "level": 2, "sig": f"{name}:ended", "text": (
                f"{name}: поддержка кончилась {when}, ESM тоже ({esm.strftime('%d.%m.%Y')}), обновлений безопасности больше нет"
                if paid and esm else f"{name}: поддержка кончилась {when}, обновлений безопасности больше нет")})
        elif left <= EOL_WARN_DAYS:
            out.append({"kind": "eol", "level": 1, "sig": f"{name}:soon", "text": f"{name}: поддержка кончится {when}, через {_days(left)}"})
    if not u:
        return out
    ts_now = now.timestamp()

    def ago(ts) -> int | None:
        try:
            t = float(ts or 0)
        except (TypeError, ValueError):
            return None
        return int((ts_now - t) // 86400) if t > 0 else None

    if u.get("reboot"):
        d = ago(u.get("reboot_ts"))
        soft = soft_reboot(u)
        # reboot_ts - mtime /run/reboot-required: файл переписывает каждая установка такого пакета,
        # поэтому это последнее обновление, а не первое (первое могло быть раньше). /run живет до
        # перезагрузки, так что старше нее срок не бывает
        when = f" (последнее обновление {_days(d)} назад)" if d is not None else ""
        if soft and d is not None and d >= REBOOT_SOFT_AFTER_DAYS:
            out.append({"kind": "reboot", "level": 1, "soft": True, "pkgs": _pkgs(u), "text": (
                f"обновлен dbus, перезапустить его без перезагрузки нельзя - перезагрузить при случае{when}")})
        elif not soft and d is not None and d >= REBOOT_AFTER_DAYS:
            parts = reboot_parts(u) or ["нужна перезагрузка после обновления"]
            out.append({"kind": "reboot", "level": 1, "pkgs": _pkgs(u), "text": "; ".join(parts) + when})
    elif u.get("kernel_new"):
        parts = reboot_parts(u) or [f"установлено ядро {_kshort(str(u['kernel_new']))} - заработает после перезагрузки"]
        out.append({"kind": "reboot", "level": 1, "text": "; ".join(parts)})
    try:
        sec = int(u.get("security") or 0)
    except (TypeError, ValueError):
        sec = 0
    unatt = str(u.get("unatt") or "")
    ud = ago(u.get("unatt_ts"))
    if sec > 0 and (unatt != "on" or ud is None or ud >= UNATT_STALE_DAYS):
        why = {"off": "автообновления выключены", "none": "unattended-upgrades не установлен"}.get(unatt)
        if why is None:
            why = f"автообновления не запускались {_days(ud)}" if ud is not None else "автообновления не запускались"
        # причина, а не число: новые обновления безопасности выходят каждую неделю
        out.append({"kind": "security", "level": 1, "sig": unatt if unatt in ("off", "none") else "stale", "text": (
            f"не установлено {sec} {_plural(sec, 'обновление', 'обновления', 'обновлений')} безопасности: {why}")})
    ad = ago(u.get("apt_ts"))
    if ad is not None and ad >= APT_STALE_DAYS:
        out.append({"kind": "apt", "level": 1, "sig": str(u.get("apt_ts")),
                    "text": f"список пакетов не обновлялся {_days(ad)}: о новых обновлениях нода не знает"})
    return out


# Перезагрузка, которую ждали. Алерт "перезагружен" нужен на неожиданную: сбой, питание, reset у
# хостера. После обновлений ее ждали: агент до перезагрузки показывал reboot-required (apt,
# unattended-upgrades с Automatic-Reboot) или ядро в /boot новее работающего. Аптайм сбрасывается в
# приеме отчета, там же и видно, что было в прошлом отчете: причина ложится в отчет ("boot": {"at",
# "why"}) и переходит из отчета в отчет, пока не будет новой перезагрузки.

def reboot_why(prev: dict) -> str:
    """Почему перезагрузку ждали, по отчету до нее; "" - не ждали"""
    u = (prev or {}).get("os_upd")
    if not isinstance(u, dict):
        return ""
    if u.get("reboot"):
        pkgs = [str(p) for p in u.get("reboot_pkgs") or []][:3]
        return "после обновления ее ждали" + (f" ({', '.join(pkgs)})" if pkgs else "")
    if u.get("kernel_new"):
        return f"ждало новое ядро {u['kernel_new']}"
    return ""


def planned_reboot(rep: dict, rebooted_at: datetime | None) -> str:
    """Причина плановой перезагрузки rebooted_at из отчета; "" - неожиданная или не знаем"""
    b = (rep or {}).get("boot")
    if rebooted_at is None or not isinstance(b, dict) or not b.get("why"):
        return ""
    try:
        at = datetime.fromisoformat(str(b.get("at") or ""))
    except ValueError:
        return ""
    t = rebooted_at if rebooted_at.tzinfo else rebooted_at.replace(tzinfo=timezone.utc)
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return str(b["why"]) if abs((at - t).total_seconds()) < 2 else ""
