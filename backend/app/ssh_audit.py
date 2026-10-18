"""SSH: пускает ли по паролю порт, открытый в интернет, и не идет ли перебор.

Настройки снимает helper changes-setup с 0.3: sshd -T -C для соединения "откуда-то из
интернета" (адрес из документационного диапазона), так что блок Match, открывающий пароли для
офиса, не в счет. Открыт ли порт в интернет, знает внешняя проверка портов (open_ports): без
нее (или если смотрели с адреса самой панели) не судим - SSH за белым списком с паролем не дыра.
Кроме одного случая: перебор с внешних адресов сам доказывает, что SSH виден из интернета. На
одной ноде SSH был проброшен с другого адреса и номера порта (NAT 203.0.113.7:2224 -> нода:2221),
проверка портов этого не видела, и алерт дал отбой, хотя подбирали еще 7 часов.

Что делать:
- root входит по паролю на открытый SSH - проблема и алерт;
- вход по паролю включен, порт открыт, идет перебор, а fail2ban/sshguard/crowdsec нет -
  предупреждение и алерт: подбирают прямо сейчас и никто не банит;
- просто вход по паролю на открытый SSH - пункт в "Требует действий": оставить ключи.
"""
from __future__ import annotations

from datetime import datetime

from app import open_ports
from app.models import Server

BRUTE_MIN = 30  # неудачных входов за 10 минут: это уже перебор, а не опечатка


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


def brute_from_internet(ssh: dict) -> bool:
    """Идет перебор, и с внешних адресов (changes-setup 0.4 шлет, откуда больше всего попыток)."""
    if int(ssh.get("fail") or 0) < BRUTE_MIN:
        return False
    return any(open_ports.is_public(str(x.get("ip") or ""))
               for x in ssh.get("fail_top") or [] if isinstance(x, dict))


def reachable(srv: Server, ssh: dict, now: datetime) -> bool:
    """SSH виден из интернета: по внешней проверке портов или по перебору с внешних адресов."""
    return internet_open(srv, ssh, now) is True or brute_from_internet(ssh)


def _top(ssh: dict) -> str:
    """' (больше всех с 203.0.113.9: 120)' - откуда перебор (changes-setup 0.4)"""
    top = [x for x in ssh.get("fail_top") or [] if isinstance(x, dict) and x.get("ip")]
    return f" (больше всех с {top[0]['ip']}: {top[0].get('n')})" if top else ""


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
    fail = int(ssh.get("fail") or 0)
    if password_login(ssh) and fail >= BRUTE_MIN and not ssh.get("guard"):
        out.append((1, f"идет перебор паролей SSH: {fail} неудачных входов за 10 минут{_top(ssh)}, вход по "
                       f"паролю включен, а fail2ban и подобного нет"))
    return out


def advice(srv: Server, now: datetime) -> list[dict]:
    """Пункт "Требует действий": вход по паролю на SSH, открытом в интернет."""
    ssh = block(srv.last_report or {})
    if ssh is None or not password_login(ssh) or not reachable(srv, ssh, now):
        return []
    text = (f"SSH пускает по паролю, а порт {_ports(ssh)} открыт в интернет: оставьте вход по ключам "
            f"(PasswordAuthentication no)")
    if not ssh.get("guard"):
        text += ", от перебора не защищает ни fail2ban, ни sshguard"
    return [{"kind": "ssh_password", "level": 2 if root_password(ssh) else 1, "text": text}]


def view(srv: Server, now: datetime) -> dict | None:
    """Для карточки: порты, пароль, root, защита от перебора, неудачные входы, открыт ли порт"""
    ssh = block(srv.last_report or {})
    if ssh is None:
        return None
    return {"ports": ssh.get("ports") or [], "password": password_login(ssh), "root": str(ssh.get("root") or ""),
            "root_password": root_password(ssh), "guard": str(ssh.get("guard") or ""),
            "fail": int(ssh.get("fail") or 0), "accepted_password": int(ssh.get("accp") or 0),
            "open": True if brute_from_internet(ssh) else internet_open(srv, ssh, now),
            "fail_top": [x for x in ssh.get("fail_top") or [] if isinstance(x, dict)][:5]}
