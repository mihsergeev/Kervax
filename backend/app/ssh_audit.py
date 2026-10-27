"""SSH: пускает ли по паролю порт, открытый в интернет, и не идет ли перебор.

Настройки снимает helper changes-setup с 0.3: sshd -T -C для соединения "откуда-то из
интернета" (адрес из документационного диапазона), так что блок Match, открывающий пароли для
офиса, не в счет. Открыт ли порт в интернет, знает внешняя проверка портов (open_ports): без
нее (или если смотрели с адреса самой панели) не судим - SSH за белым списком с паролем не дыра.
Кроме одного случая: перебор с внешних адресов сам доказывает, что SSH виден из интернета. На
одной ноде SSH был проброшен с другого адреса и номера порта (NAT 203.0.113.7:2224 -> нода:2221),
проверка портов этого не видела, и алерт дал отбой, хотя подбирали еще 7 часов.

Перебор помним (servers.changes["ssh_brute"], пишет changes.track по снимкам helper'а): боты
приходят волнами, и на GPU-ноде за "filtered" с адреса проверки неудачных входов за 10 минут было
то 31-35, то меньше 30. SSH считался то открытым, то нет, и "root по паролю" давал тревогу и отбой
каждые 20 минут. Теперь перебор с внешних адресов держит SSH видимым из интернета сутки
(BRUTE_HOLD), а сам перебор идет еще час после последних 10 минут выше порога (BRUTE_ON). Отбой
сразу - когда исправили настройку (root без пароля, пароли выключены, поставили fail2ban); если
закрыли порт - когда за час (или сутки для root) перебора больше не было.

Закрыли порт файрволом ноды (остальным отказ, на порт SSH только allow с конкретных сетей) - SSH
"по белому списку", и перебор, записанный до этого, больше не держит его открытым: GPU-нода после
ufw с белым списком еще сутки числилась "открыт в интернет" с тревогой root по паролю. Перебор,
пришедший уже при белом списке (в памяти "fw"), и перебор в текущем снимке держат открытым как
раньше: значит, файрвол пускает в обход того, что видит helper (или в списке есть боты).

Что делать:
- root входит по паролю на открытый SSH - проблема и алерт;
- вход по паролю включен, порт открыт, идет перебор, а fail2ban/sshguard/crowdsec нет -
  предупреждение и алерт: подбирают прямо сейчас и никто не банит;
- просто вход по паролю на открытый SSH - пункт в "Требует действий": оставить ключи.

У пункта готовые шаги (fix), как у советов по ОС: команды под ноду, после которых пункт уходит сам
со следующим снимком helper'а. Сначала - кому добавить ключ: учетка с паролем без ключей, в которую
входят, после выключения паролей не войдет. Пароль выключается отдельным файлом 00-...: в sshd
побеждает первое значение, Include sshd_config.d/*.conf стоит в начале sshd_config, файлы идут по
алфавиту, а cloud-init на Ubuntu кладет 50-cloud-init.conf с PasswordAuthentication yes. OpenSSH
старше 8.2 Include не знает - строка встает первой в sshd_config. Если пароль нужен - решение
"вход по паролю нужен" (alert_mutes "ssh:<вид>"), как решения по ОС: пункт уходит из "Требует
действий", в карточке приглушен с кнопкой "вернуть".
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app import open_ports
from app.models import Server

BRUTE_MIN = 30  # неудачных входов за 10 минут: это уже перебор, а не опечатка
BRUTE_ON = timedelta(hours=1)  # перебор "идет", пока был выше порога за последний час
BRUTE_HOLD = timedelta(days=1)  # перебор с внешних адресов доказывает, что SSH открыт, на сутки
BRUTE_WINDOW = timedelta(minutes=10)  # за сколько helper считает неудачные входы
MUTE_PREFIX = "ssh:"
DROPIN = "/etc/ssh/sshd_config.d/00-no-password.conf"
CONF = "/etc/ssh/sshd_config"
CONF_BAK = CONF + ".bak-nopass"
# имя учетки, которое можно вставить в команду как есть (с ~имя тоже)
NAME_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*\$?$")


def block(rep: dict) -> dict | None:
    ch = ((rep or {}).get("extras") or {}).get("changes")
    ssh = ch.get("ssh") if isinstance(ch, dict) else None
    return ssh if isinstance(ssh, dict) and ssh.get("ports") else None


def _password_path(pw: str, kbd: str, pam: str, methods: str) -> bool:
    """Можно ли войти одним паролем: PasswordAuthentication или keyboard-interactive через PAM, и
    AuthenticationMethods не требует ключ (publickey,password - пароль только вторым шагом)."""
    if pw != "yes" and not (kbd == "yes" and pam == "yes"):
        return False
    methods = (methods or "any").strip()
    if methods in ("", "any"):
        return True
    for alt in methods.split():
        steps = alt.split(",")
        if "publickey" not in steps and any(s.split(":")[0] in ("password", "keyboard-interactive") for s in steps):
            return True
    return False


def password_login(ssh: dict) -> bool:
    return _password_path(str(ssh.get("pass") or ""), str(ssh.get("kbd") or ""), str(ssh.get("pam") or ""),
                          str(ssh.get("methods") or ""))


def root_password(ssh: dict) -> bool:
    """root входит по паролю: PermitRootLogin yes (prohibit-password и without-password пускают
    только по ключу) и пароль в контексте root доступен"""
    return str(ssh.get("root") or "") == "yes" and _password_path(
        str(ssh.get("rootpass") or ""), str(ssh.get("rootkbd") or ""), str(ssh.get("pam") or ""),
        str(ssh.get("methods") or ""))


def internet_open(srv: Server, ssh: dict, now: datetime) -> bool | None:
    """Открыт ли порт SSH в интернет по свежей внешней проверке: True/False, None - не знаем
    (проверки не было, она старая или шла с адреса самой панели)."""
    ex = srv.exposure if isinstance(getattr(srv, "exposure", None), dict) else {}
    ts = open_ports.scanned_at(srv)
    if not ex or ts is None or now - ts > open_ports.FRESH or open_ports.from_panel(srv):
        return None
    ports = {int(p) for p in ssh.get("ports") or [] if str(p).isdigit()}
    alien = open_ports.foreign(srv)  # там отвечает другой сервер
    rows = [x for x in open_ports.marked_rows(srv, ex.get("ports") or []) if x.get("port") in ports
            and x.get("ip") not in alien]
    if not rows:
        return None
    # открыт только белому списку с адресом проверки - для интернета закрыт
    return any(x.get("state") == "open" and not x.get("allow") for x in rows)


def allowlisted(srv: Server, ssh: dict) -> bool:
    """Файрвол ноды пускает на все порты SSH только с конкретных сетей (или никого), остальным
    отказ - по свежему отчету helper'а (changes-setup 0.7, open_ports.allow_nets)."""
    ports = [int(p) for p in ssh.get("ports") or [] if str(p).isdigit()]
    return bool(ports) and all(open_ports.allow_nets(srv, p, fam) is not None
                               for p in ports for fam in open_ports.own_families(srv))


def brute_from_internet(ssh: dict) -> bool:
    """Идет перебор, и с внешних адресов (changes-setup 0.4 шлет, откуда больше всего попыток)."""
    if int(ssh.get("fail") or 0) < BRUTE_MIN:
        return False
    return any(open_ports.is_public(str(x.get("ip") or ""))
               for x in ssh.get("fail_top") or [] if isinstance(x, dict))


def note_brute(state: dict, snap: dict, stamp: str, srv: Server | None = None) -> None:
    """Запомнить перебор из снимка helper'а (changes.apply_snapshot): state["ssh_brute"] - когда
    последний раз было BRUTE_MIN+ с внешних адресов, сколько, откуда больше всех и шел ли уже при
    белом списке (fw). state["ssh_fw"] - с какого снимка SSH по белому списку (снимок - из
    srv.last_report): неудачные входы считаются за 10 минут, и в первом снимке после ufw они еще
    старые."""
    ssh = snap.get("ssh") if isinstance(snap.get("ssh"), dict) else {}
    if not ssh:
        return
    since = None
    if srv is not None and allowlisted(srv, ssh):
        since = state.get("ssh_fw") or stamp
        state["ssh_fw"] = since
    else:
        state.pop("ssh_fw", None)
    if brute_from_internet(ssh):
        top = [x for x in ssh.get("fail_top") or [] if isinstance(x, dict) and x.get("ip")][:1]
        old = since is not None and _ts(stamp) - _ts(since) >= BRUTE_WINDOW
        state["ssh_brute"] = {"ts": stamp, "n": int(ssh.get("fail") or 0), "top": top, "fw": old}


def _ts(v: str) -> datetime:
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _brute_mem(srv: Server) -> tuple[datetime, dict] | None:
    ch = srv.changes if isinstance(getattr(srv, "changes", None), dict) else {}
    b = ch.get("ssh_brute")
    if not isinstance(b, dict) or not b.get("ts"):
        return None
    try:
        return datetime.fromisoformat(str(b["ts"])), b
    except ValueError:
        return None


def brute_recent(srv: Server, ssh: dict, now: datetime, hold: timedelta) -> bool:
    """Перебор с внешних адресов в этом снимке или был за hold - кроме записанного до того, как
    SSH закрыли белым списком"""
    if brute_from_internet(ssh):
        return True
    mem = _brute_mem(srv)
    if mem is None or now - mem[0] > hold:
        return False
    return bool(mem[1].get("fw")) or not allowlisted(srv, ssh)


def reachable(srv: Server, ssh: dict, now: datetime) -> bool:
    """SSH виден из интернета: по внешней проверке портов или по перебору с внешних адресов за сутки."""
    return internet_open(srv, ssh, now) is True or brute_recent(srv, ssh, now, BRUTE_HOLD)


def _top(ssh: dict) -> str:
    """' (больше всех с 203.0.113.9: 120)' - откуда перебор (changes-setup 0.4)"""
    top = [x for x in ssh.get("fail_top") or [] if isinstance(x, dict) and x.get("ip")]
    return f" (больше всех с {top[0]['ip']}: {top[0].get('n')})" if top else ""


def _brute_text(srv: Server, ssh: dict) -> str:
    fail = int(ssh.get("fail") or 0)
    mem = _brute_mem(srv)
    if fail >= BRUTE_MIN or mem is None:
        return f"{fail} неудачных входов за 10 минут{_top(ssh)}"
    # сейчас тише порога, а за час подбирали: число и источник - того раза
    b = mem[1]
    return f"за последний час до {int(b.get('n') or 0)} неудачных входов за 10 минут{_top({'fail_top': b.get('top')})}"


def _ports(ssh: dict) -> str:
    return ", ".join(str(p) for p in ssh.get("ports") or [])


def problems(srv: Server, now: datetime) -> list[tuple[int, str]]:
    """(уровень, текст) для алерта ssh_risk и "Что сломано"."""
    ssh = block(srv.last_report or {})
    if ssh is None or not reachable(srv, ssh, now):
        return []
    out = []
    if root_password(ssh):
        out.append((2, f"root может войти по SSH паролем, а порт {_ports(ssh)} открыт в интернет"))
    # порт открыт уже доказан: перебор сейчас - с любых адресов, после - по памяти о внешнем
    brute = int(ssh.get("fail") or 0) >= BRUTE_MIN or brute_recent(srv, ssh, now, BRUTE_ON)
    if password_login(ssh) and not ssh.get("guard") and brute:
        out.append((1, f"идет перебор паролей SSH: {_brute_text(srv, ssh)}, вход по "
                       f"паролю включен, а fail2ban и подобного нет"))
    return out


_ROOT_PORTS = re.compile(r"^root может войти по SSH паролем, а порт (.+) открыт в интернет")
_BRUTE = "идет перебор паролей SSH"


def tg_short(items: list[tuple[int, str]]) -> tuple[str, list[str]] | None:
    """Алерт ssh_risk (вход - problems()): "Перебор паролей SSH", "вход по паролю открыт, fail2ban
    нет". root по паролю - главное, перебор тогда второй строкой. Без счетчиков попыток."""
    if not items:
        return None
    root = next((m for _l, t in items if (m := _ROOT_PORTS.match(t))), None)
    brute = any(t.startswith(_BRUTE) for _l, t in items)
    if root:
        ports = root.group(1)
        facts = [f"{'порты' if ',' in ports else 'порт'} {ports} открыт{'ы' if ',' in ports else ''} в интернет"]
        if brute:
            facts.append("Идет перебор паролей, fail2ban нет")
        return "SSH пускает root по паролю", facts
    if brute:
        return "Перебор паролей SSH", ["вход по паролю открыт, fail2ban нет"]
    return "Опасный вход SSH", []


def advice(srv: Server, now: datetime) -> list[dict]:
    """Пункт "Требует действий": вход по паролю на SSH, открытом в интернет. key - решение "вход по
    паролю нужен" в alert_mutes (muted), fix - шаги [{"id", "cmd"?, "arg"?, "on"?, "alt"?}]."""
    ssh = block(srv.last_report or {})
    if ssh is None or not password_login(ssh) or not reachable(srv, ssh, now):
        return []
    text = (f"SSH пускает по паролю, а порт {_ports(ssh)} открыт в интернет: оставьте вход по ключам "
            f"(PasswordAuthentication no)")
    if not ssh.get("guard"):
        text += ", от перебора не защищает ни fail2ban, ни sshguard"
    key = MUTE_PREFIX + "ssh_password"
    return [{"kind": "ssh_password", "level": 2 if root_password(ssh) else 1, "text": text, "key": key,
             "muted": key in (srv.alert_mutes or []), "fix": password_fix(srv, ssh, now)}]


def _day(v) -> str:
    """unix-секунды или ISO-время -> 18.10.2026"""
    d = datetime.fromtimestamp(v, timezone.utc) if isinstance(v, (int, float)) else datetime.fromisoformat(str(v))
    return d.strftime("%d.%m.%Y")


def _add_key(name: str) -> str | None:
    if not NAME_OK.match(name):
        return None
    # от root и chown: sudo -u другой учетки бывает запрещен и спрашивает пароль
    return (f"sudo mkdir -p -m 700 ~{name}/.ssh\n"
            f"echo '<public-key>' | sudo tee -a ~{name}/.ssh/authorized_keys\n"
            f"sudo chown -R {name}: ~{name}/.ssh")


def need_key(srv: Server, ssh: dict, now: datetime) -> list[dict]:
    """Кто не войдет, когда выключат пароли: пароль задан, ключей нет, а входили за 90 дней (как -
    helper не говорит, но без ключа это пароль), или входили по паролю (changes.ssh_pw), хотя ключ
    есть. root - только если его вообще пускают по паролю."""
    from app import access_audit  # access_audit берет отсюда password_login: на уровне модуля - цикл
    acc = access_audit.accounts(srv, now)
    if acc is None:
        return []
    ch = srv.changes if isinstance(getattr(srv, "changes", None), dict) else {}
    pw_seen = ch.get("ssh_pw") if isinstance(ch.get("ssh_pw"), dict) else {}
    cut = now.timestamp() - access_audit.STALE.total_seconds()
    out = []
    for a in acc[0]:
        if a["pw"] != "P" or (a["uid"] == 0 and not root_password(ssh)):
            continue
        name = a["name"]
        if a["keys"] == 0 and a["last"] >= cut:
            step = {"id": "ssh_key_add", "arg": name, "on": _day(a["last"])}
        elif pw_seen.get(name):
            step = {"id": "ssh_key_check", "arg": name, "on": _day(pw_seen[name])}
        else:
            continue
        cmd = _add_key(name)
        out.append(dict(step, cmd=cmd) if cmd else step)
    return out


def _tee(path: str, lines: list[str]) -> str:
    # построчно, как включение автообновлений: первая строка tee, остальные tee -a
    return "\n".join(f"echo '{x}' | sudo tee {'-a ' if i else ''}{path}" for i, x in enumerate(lines))


def password_fix(srv: Server, ssh: dict, now: datetime) -> list[dict]:
    """Шаги, после которых вход по паролю из интернета выключен и пункт уходит сам."""
    rep = srv.last_report or {}
    debian = bool(re.match(r"(Ubuntu|Debian)\b", str(rep.get("os") or "")))
    # Debian и Ubuntu: сервис ssh (на Ubuntu 22.10+ его поднимает ssh.socket, reload перечитывает
    # конфиг так же), у остальных sshd
    reload = f"sudo systemctl reload {'ssh' if debian else 'sshd'}"
    off = ["PasswordAuthentication no"]
    if str(ssh.get("kbd") or "") == "yes" and str(ssh.get("pam") or "") == "yes":
        off.append("KbdInteractiveAuthentication no")  # пароль через PAM
    lan = ["Match Address <cidr>", "    PasswordAuthentication yes"]
    m = re.match(r"OpenSSH_(\d+)\.(\d+)", str(ssh.get("banner") or ""))
    steps = need_key(srv, ssh, now)
    if m and (int(m.group(1)), int(m.group(2))) < (8, 2):
        # Include появился в 8.2: строка встает первой в sshd_config, Match - в конец
        first = f"sudo cp -an {CONF} {CONF_BAK}\n" + "\n".join(f"sudo sed -i '1i {x}' {CONF}" for x in reversed(off))
        steps.append({"id": "ssh_conf", "cmd": first})
        undo = f"sudo cp -a {CONF_BAK} {CONF}\n{reload}"
        match = (f"sudo cp -an {CONF} {CONF_BAK}\nsudo sed -i '1i PasswordAuthentication no' {CONF}\n"
                 + "\n".join(f"echo '{x}' | sudo tee -a {CONF}" for x in lan))
    else:
        steps.append({"id": "ssh_dropin", "cmd": _tee(DROPIN, off)})
        undo = f"sudo rm {DROPIN}\n{reload}"
        match = _tee(DROPIN, ["PasswordAuthentication no"] + lan)
    steps += [
        {"id": "ssh_check", "cmd": "sudo sshd -t && sudo sshd -T | grep -Ei '^(passwordauthentication|kbdinteractiveauthentication) '"},
        {"id": "ssh_reload", "cmd": reload},
        {"id": "ssh_undo", "undo": True, "cmd": undo},
        {"id": "ssh_match", "alt": True, "cmd": match},
    ]
    if not ssh.get("guard"):
        # fail2ban по умолчанию банит порт 22, а SSH бывает на другом; журнал - через systemd: на
        # свежих Ubuntu нет /var/log/auth.log
        ports = _ports(ssh).replace(" ", "") or "ssh"
        guard = {"id": "ssh_guard", "alt": True, "arg": ports}
        if debian:
            guard["cmd"] = ("sudo apt install -y fail2ban python3-systemd\n"
                            f"printf '[sshd]\\nenabled = true\\nport = {ports}\\nbackend = systemd\\n'"
                            " | sudo tee /etc/fail2ban/jail.d/sshd-port.local\n"
                            "sudo systemctl enable fail2ban && sudo systemctl restart fail2ban\n"
                            "sudo fail2ban-client status sshd")
        steps.append(guard)
    return steps


def view(srv: Server, now: datetime) -> dict | None:
    """Для карточки: порты, пароль, root, защита от перебора, неудачные входы, открыт ли порт (open)
    и пускает ли на него файрвол только белый список (allowlist)"""
    ssh = block(srv.last_report or {})
    if ssh is None:
        return None
    return {"ports": ssh.get("ports") or [], "password": password_login(ssh), "root": str(ssh.get("root") or ""),
            "root_password": root_password(ssh), "guard": str(ssh.get("guard") or ""),
            "fail": int(ssh.get("fail") or 0), "accepted_password": int(ssh.get("accp") or 0),
            "open": True if brute_recent(srv, ssh, now, BRUTE_HOLD) else internet_open(srv, ssh, now),
            "allowlist": allowlisted(srv, ssh),
            "fail_top": [x for x in ssh.get("fail_top") or [] if isinstance(x, dict)][:5]}
