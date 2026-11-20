"""Адрес того, кто пришел в панель, за своими прокси.

Панель стоит за своими прокси (caddy, за ним nginx фронтенда) в docker-сети, и адрес
соединения - это прокси: все входы в журнале выглядели одним частным адресом, а блокировка
после неудач запирала всех сразу, а не того, кто подбирает пароль. Каждый прокси дописывает в
X-Forwarded-For, от кого пришел запрос: справа налево снимаем свои прокси, первый чужой адрес -
клиент. Нужен входу (api/auth.py), счетчику левых запросов (request_watch.py) и адресу агента.

Заголовку верим, только когда соединение от nginx фронтенда (бэкенд наружу не опубликован, к
нему ходит только фронтенд), и снимаем справа ровно доверенные хопы. Раньше доверенным считался
любой частный адрес, а при цепочке из одних частных брался самый левый: в офисной установке
(KERVAX_BIND=0.0.0.0) или из соседнего контейнера в сети caddy клиент подставлял любой адрес -
адрес из белого списка входа, новый адрес на каждую попытку. Самый левый адрес клиент пишет сам,
в ключ он не идет никогда.

Кто свой, задает KERVAX_TRUSTED_PROXIES (через пробел или запятую, слова и сети складываются):
  gateway - шлюз docker-сети бэкенда: так в nginx приходит прокси на самом хосте (nginx/traefik
            хоста на 127.0.0.1:8080 по compose.yml). Умолчание compose.yml. Действует, только
            когда KERVAX_BIND на loopback: при 0.0.0.0 или голом порте со шлюза приходят и не
            прокси (IPv6 через docker-proxy), тогда gateway = none и предупреждение админу
  private - один хоп справа из частной сети: caddy в общей docker-сети (compose.caddy.yml, там
            это умолчание). Порт фронтенда тогда не опубликован, в nginx приходят только из
            docker-сетей. Если хостовый снимок agent_proxy_peers.json свежий (agent_auth.py),
            вместо любого частного адреса снимаются ровно адреса caddy из него
  адрес или сеть - свой прокси, заданный явно
  none или пусто - своих прокси перед nginx нет: клиент - тот, кто пришел в nginx (офис)

Когда клиента не определить (Client.known=False), ключ - адрес инфраструктуры: фронтенда,
шлюза docker или чужого прокси, который сам пишет X-Forwarded-For. Все клиенты за ним входят
этим адресом и делят счет неудач и блокировку (api/auth.py), белый список входа для него не
действует, адрес агента не перезаписывается, админ видит подсказку в "Ошибках панели".
Client.exact - клиент определен через точно известные прокси (без догадки private): только
такой адрес проходит по белому списку входа.
"""

import functools
import ipaddress
import logging
import os
import re
import time
from dataclasses import dataclass, replace

from app.config import get_settings

log = logging.getLogger(__name__)

# частные сети: соединение с бэкендом от фронтенда в docker-сети и хоп caddy в режиме private
PROXY_NETS = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")
]
# хопов в X-Forwarded-For разбираем не больше: дальше левее - то, что клиент написал сам
_MAX_HOPS = 64
# снимок доверенных прокси перечитываем не чаще (запрос в счетчике request_watch - каждый)
_SNAP_TTL = 5.0
_snap: list = [None]
_WORDS = ("gateway", "private", "none")
# подсказка админу про неопределенный адрес - не чаще раза в столько секунд на процесс
_HINT_EVERY = 6 * 3600
_hint_at: list[float] = [0.0]
_PORT = re.compile(r"\d{1,5}")


@dataclass(frozen=True)
class Client:
    ip: str
    known: bool = True
    exact: bool = True
    # фронтенд только что пересоздан и еще не попал в снимок прокси: адрес не точный, но
    # подсказки админу ("адрес не определен", "белый список не действует") - ложная тревога
    quiet: bool = False


def is_proxy(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in n for n in PROXY_NETS)


def _ip(value) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not isinstance(value, str) or "%" in value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def _hop(part: str):
    """Хоп X-Forwarded-For: адрес, адрес:порт или [IPv6]:порт (так пишут часть прокси)."""
    s = part.strip()
    if s.startswith("["):
        inner, sep, rest = s[1:].partition("]")
        if not sep or (rest and not (rest.startswith(":") and _PORT.fullmatch(rest[1:]))):
            return None
        ip = _ip(inner)
        return ip if ip is not None and ":" in inner else None
    if s.count(":") == 1:
        host, port = s.split(":")
        return _ip(host) if _PORT.fullmatch(port) and "." in host else None
    return _ip(s)


@functools.lru_cache(maxsize=1)
def _gateways() -> frozenset:
    """Шлюзы маршрута по умолчанию в контейнере бэкенда: адрес хоста в docker-сети."""
    return read_gateways("/proc/net/route", "/proc/net/ipv6_route")


def read_gateways(route4: str, route6: str) -> frozenset:
    out = set()
    try:
        with open(route4, encoding="ascii") as f:
            for line in f.read().splitlines()[1:]:
                p = line.split()
                try:
                    if len(p) > 3 and p[1] == "00000000" and int(p[3], 16) & 2:
                        out.add(ipaddress.IPv4Address(bytes.fromhex(p[2])[::-1]))
                except ValueError:
                    continue
    except OSError:
        pass
    try:
        with open(route6, encoding="ascii") as f:
            for line in f.read().splitlines():
                p = line.split()
                try:
                    if len(p) > 4 and p[0] == "0" * 32 and p[1] == "00" and p[4] != "0" * 32:
                        out.add(ipaddress.IPv6Address(bytes.fromhex(p[4])))
                except ValueError:
                    continue
    except OSError:
        pass
    return frozenset(out)


def bind_local(bind: str) -> bool:
    """KERVAX_BIND публикует фронтенд только на loopback (или вовсе не публикует: caddy)."""
    b = (bind or "").strip()
    if b == "unpublished":
        return True
    if not b:
        return False  # запуск не через наш compose: где опубликован фронтенд, неизвестно
    host = b.rpartition(":")[0] if ":" in b else ""
    host = host.strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # голый порт, 0.0.0.0, имя: опубликован наружу


@functools.lru_cache(maxsize=8)
def edge_gateways(raw: str) -> frozenset:
    """KERVAX_PROXY_GATEWAY (адреса через пробел, их пишет quickstart) -> набор адресов."""
    out = set()
    for word in raw.replace(",", " ").split():
        ip = _ip(word)
        if ip is not None:
            out.add(ip)
    return frozenset(out)


@functools.lru_cache(maxsize=8)
def _parse(raw: str) -> tuple[tuple, bool, bool]:
    """KERVAX_TRUSTED_PROXIES -> (сети, gateway, private). Непонятное слово не доверяет ничему,
    о нем говорит config_problems."""
    nets, gateway, private = [], False, False
    for word in raw.replace(",", " ").split():
        w = word.strip().lower()
        if w == "gateway":
            gateway = True
        elif w == "private":
            private = True
        elif w != "none":
            try:
                nets.append(ipaddress.ip_network(w, strict=False))
            except ValueError:
                continue
    return tuple(nets), gateway, private


def config_problems(raw: str) -> list[str]:
    """Слова KERVAX_TRUSTED_PROXIES, которые не слово и не адрес/сеть (опечатка)."""
    bad = []
    for word in (raw or "").replace(",", " ").split():
        if word.lower() in _WORDS:
            continue
        try:
            ipaddress.ip_network(word, strict=False)
        except ValueError:
            bad.append(word)
    return bad


async def check_config(session_factory) -> list[str]:
    """При старте: опечатка в KERVAX_TRUSTED_PROXIES или gateway при публикации не на loopback -
    в лог и в "Ошибки панели". Возвращает, что не так (непонятые слова, "gateway")."""
    from app import panel_errors

    settings = get_settings()
    raw = getattr(settings, "trusted_proxies", "") or ""
    bind = getattr(settings, "bind", "") or ""
    out = config_problems(raw)
    notes = []
    if out:
        notes.append(("config", f"KERVAX_TRUSTED_PROXIES: не понял {', '.join(out)[:200]} - это не gateway, "
                      "private, none и не адрес или сеть. Этим словам панель не доверяет, адреса клиентов за "
                      "таким прокси не определяются. Исправьте значение в .env (см. .env.example)"))
    if _parse(raw)[1] and not bind_local(bind):
        out.append("gateway")
        where = (f"панель опубликована на KERVAX_BIND={bind[:64]}, не на 127.0.0.1" if bind else
                 "KERVAX_BIND не задан (панель запущена не через ее compose), где она опубликована - неизвестно")
        notes.append(("bind", f"KERVAX_TRUSTED_PROXIES=gateway, а {where}: со шлюза docker тогда могут прийти и "
                      "клиенты (IPv6 через docker-proxy), "
                      "поэтому шлюзу панель не доверяет (как none). Если перед панелью свой прокси, впишите его "
                      "адрес в KERVAX_TRUSTED_PROXIES; если прокси нет - задайте none"))
    if _parse(raw)[2] and bind != "unpublished" and not bind_local(bind):
        out.append("private")
        notes.append(("private", f"KERVAX_TRUSTED_PROXIES=private, а фронтенд опубликован на KERVAX_BIND={bind[:64] or '?'}: "
                      "private рассчитан на caddy в docker-сети (compose.caddy.yml), а с опубликованного порта "
                      "клиент из LAN сам станет этим хопом и подставит любой адрес. Впишите точный адрес своего "
                      "прокси или задайте none"))
    for fp, msg in notes:
        log.warning(msg)
        await panel_errors.record(session_factory, kind="server", action="KERVAX_TRUSTED_PROXIES",
                                  message=msg, fp_key=f"client_ip|{fp}")
    return out


def _snapshot(data_dir: str):
    """Свежий снимок своих прокси от хостового sync (agent_auth.py) или None."""
    from app.agent_auth import PROXY_FILE, proxy_peers

    try:
        st = os.stat(os.path.join(data_dir, PROXY_FILE))
    except OSError:
        return None
    stamp = (data_dir, st.st_mtime_ns, st.st_size)
    now = time.monotonic()
    hit = _snap[0]
    if hit and hit[0] == stamp and now - hit[1] < _SNAP_TTL:
        return hit[2]
    value = proxy_peers(data_dir)
    _snap[0] = (stamp, now, value)
    return value


# когда панель впервые увидела адрес соединения, которого нет в снимке прокси
_peer_first: dict[str, float] = {}


def _fresh_peer(peer: str) -> bool:
    """Адрес соединения появился недавно (не дольше двух сроков жизни снимка): снимок обновится
    сам, тревожить админа рано."""
    from app.agent_auth import PROXY_MAX_AGE

    now = time.monotonic()
    if len(_peer_first) > 256:
        _peer_first.clear()
    first = _peer_first.setdefault(peer, now)
    return now - first < 2 * PROXY_MAX_AGE


def resolve(host: str | None, xff: str) -> Client:
    """host - адрес соединения, xff - заголовок X-Forwarded-For как пришел."""
    who = _resolve(host, xff)
    if who.known and who.exact:
        return who
    peer = _ip(host)
    if peer is None or not is_proxy(peer):
        return who
    from app.agent_auth import PROXY_FILE

    data_dir = get_settings().data_dir
    if not os.path.exists(os.path.join(data_dir, PROXY_FILE)):
        return who
    snap = _snapshot(data_dir)
    if (snap is None or str(peer) not in snap[0]) and _fresh_peer(str(peer)):
        return replace(who, quiet=True)
    return who


def _resolve(host: str | None, xff: str) -> Client:
    host = host or "unknown"
    peer = _ip(host)
    if peer is None:
        return Client(host, known=False, exact=False)
    settings = get_settings()
    nets, gateway, private = _parse(getattr(settings, "trusted_proxies", "") or "")
    gateway = gateway and bind_local(getattr(settings, "bind", "") or "")
    # своих прокси нет совсем (none): частный адрес с X-Forwarded-For - клиент из LAN, а не прокси
    expect_proxies = bool(nets or gateway or private)
    snap = _snapshot(settings.data_dir)
    if snap is not None:
        # точная топология известна: XFF только от адресов фронтенда, снимаем ровно caddy
        frontends, forwarded = snap
        expect_proxies = True
        if str(peer) not in frontends:
            # не фронтенд снимка: прямой клиент или фронтенд после пересоздания, до нового снимка
            known = not is_proxy(peer)
            return Client(str(peer), known=known, exact=known)
        exact = {ipaddress.ip_address(a) for a in forwarded}
        private = False
    else:
        if not is_proxy(peer):
            return Client(str(peer))
        exact = set()
    gws = _gateways()
    if gateway:
        exact |= gws
    # шлюз сети caddy (KERVAX_PROXY_GATEWAY): IPv6-клиент в IPv4-сети caddy приходит через
    # docker-proxy с этого адреса, кто за ним - не видно. Своим прокси он не бывает
    gws = gws | edge_gateways(getattr(settings, "proxy_gateway", "") or "")
    parts = (xff or "").rsplit(",", _MAX_HOPS)
    hops = list(reversed(parts))
    guessed = False
    for i, part in enumerate(hops):
        ip = _hop(part)
        if ip is None:
            break  # мусор там, где ждали адрес от своего прокси
        if ip in exact or any(ip in n for n in nets):
            continue
        if ip in gws:
            # шлюз docker: прокси хоста или IPv6 через docker-proxy - кто за ним, не видно
            return Client(str(ip), known=False, exact=False)
        if private and i == 0 and is_proxy(ip):
            guessed = True
            continue  # caddy в общей docker-сети: один хоп, левее него - клиент, даже из LAN
        if expect_proxies and is_proxy(ip) and i + 1 < len(hops) and hops[i + 1].strip():
            # частный адрес, который сам дописал X-Forwarded-For: чужой прокси, не клиент
            return Client(str(ip), known=False, exact=False)
        return Client(str(ip), exact=not guessed)
    if peer.is_loopback and not (xff or "").strip():
        return Client(str(peer))  # процесс на той же машине без прокси (healthcheck, разработка)
    # цепочка из одних своих прокси или мусор: ключ - адрес соединения
    return Client(str(peer), known=not is_proxy(peer), exact=not is_proxy(peer))


def client_key(host: str | None, xff: str) -> str:
    return resolve(host, xff).ip


def _hint(session_factory, who: Client) -> None:
    """Раз в _HINT_EVERY: адрес клиента не определен - в лог и в "Ошибки панели"."""
    now = time.monotonic()
    if _hint_at[0] and now - _hint_at[0] < _HINT_EVERY:
        return
    _hint_at[0] = now
    msg = (f"Адрес клиента не определен: запрос пришел от {who.ip} (прокси, шлюз docker или "
           "фронтенд), кто за ним - не видно. Все клиенты за ним входят этим адресом и делят "
           "блокировку после неудач (один подбор пароля запрет всех), белый список не действует. "
           "Если перед панелью "
           "свой прокси, впишите его адрес или подсеть в KERVAX_TRUSTED_PROXIES (.env.example) и "
           "проверьте свой адрес на странице Пользователи - Защита входа. Адреса серверов из таких "
           "отчетов агента не обновляются и в белый список автобана не попадают")
    log.warning(msg)
    if session_factory is None:
        return
    from app import panel_errors

    panel_errors.record_soon(session_factory, kind="server", action="адрес клиента", message=msg,
                             fp_key="client_ip|unknown")


def request_client(request) -> Client:
    """resolve для запроса FastAPI: несколько заголовков XFF склеиваются по порядку."""
    who = resolve(request.client.host if request.client else None,
                  ", ".join(request.headers.getlist("x-forwarded-for")))
    if not who.known and not who.quiet:
        state = getattr(getattr(request, "app", None), "state", None)
        _hint(getattr(state, "session_factory", None), who)
    return who


def request_key(request) -> str:
    return request_client(request).ip


_allow_note_at: list[float] = [0.0]


def note_allowlist_inactive(session_factory, who: Client) -> None:
    """Белый список входа задан, а адрес клиента не точный: список молча не действовал бы.
    Раз в _HINT_EVERY - в лог и в "Ошибки панели"."""
    now = time.monotonic()
    if _allow_note_at[0] and now - _allow_note_at[0] < _HINT_EVERY:
        return
    _allow_note_at[0] = now
    why = "адрес клиента не определен" if not who.known else "прокси caddy задан догадкой (private)"
    msg = (f"Белый список входа не действует: {why}, адрес {who.ip} мог подставить не клиент. Задайте "
           "точный адрес своего прокси в KERVAX_TRUSTED_PROXIES или включите снимок прокси "
           "(docs/agent-source-guard.md), затем проверьте свой адрес на странице Пользователи - Защита входа")
    log.warning(msg)
    if session_factory is None:
        return
    from app import panel_errors

    panel_errors.record_soon(session_factory, kind="server", action="белый список входа", message=msg,
                             fp_key="client_ip|allowlist")
