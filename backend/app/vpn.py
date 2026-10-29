"""VPN и прокси на сервере: бейдж в списке и в карточке, клиенты в разделе "VPN".

Что стоит, панель видит без нового агента: образы работающих контейнеров Docker (amnezia-awg2,
xtls/xray-core, mhsanaei/3x-ui, kylemanna/openvpn, nineseconds/mtg...) и сетевые интерфейсы хоста
(wg0, awg0, tailscale0). Интерфейс AmneziaWG в контейнере Amnezia живет в его netns, на хосте его
не видно, но контейнер виден по образу. Только образ, а не имя контейнера: рядом с 3x-ui стоят
3x-ui-caddy-1 и 3x-ui-docker-api-1, это не VPN.

Клиентов присылает root helper vpn-setup (extras["vpn"], раз в 2 минуты): wg/awg show all dump на
хосте и в контейнерах, имена из clientsTable Amnezia, комментариев wg*.conf и wg-easy, база 3x-ui,
конфиг Xray (email клиентов), статус-файл OpenVPN, узлы Headscale, tailscale status. Без ключей и
конфигов: имя, внутренний адрес, последний handshake, трафик и адрес, с которого клиент пришел.

Раздает ли сервер VPN или только сам в нем: клиент меша Tailscale на обычной ноде - это не
VPN-сервер, в списке он тише (пунктиром, как нода за ретранслятором). Exit node и subnet router
Tailscale по данным helper'а - уже раздает. Голый интерфейс wg0 без helper'а - туннель, роль
неизвестна: helper скажет по конфигу (пир без Endpoint подключается к нам - значит, мы сервер).

Клиентов отсюда же берет учет торрент-трафика: clients() - плоский список с внутренними адресами.
"""
from __future__ import annotations

import re

STALE = 900  # блок helper'а старше 15 минут: таймер не идет или docker exec завис
ONLINE_S = 180  # handshake WireGuard свежее 3 минут - клиент на связи (keepalive 25 с, rekey 2 мин)
MAX_CLIENTS = 500  # в одном сервисе на ответ: таблица в карточке, а не выгрузка базы

# Каталог по образу контейнера: (ключ, подпись, роль, образец). Первое совпадение. role: server -
# раздает VPN или прокси клиентам, mesh - сам участник меша. Панель Amnezia Control и ее фронт -
# не VPN: управляет нодами, сама ничего не раздает
_PANEL_RE = re.compile(r"amnezia-control|acontrol-(backend|frontend)")
CATALOG: list[tuple[str, str, str, re.Pattern]] = [
    (k, label, role, re.compile(rx)) for k, label, role, rx in (
        ("awg", "AmneziaWG", "server", r"amnezia-?awg|acontrol-awg|amneziawg"),
        ("xray", "Xray", "server", r"amnezia-xray"),
        ("openvpn", "OpenVPN", "server", r"amnezia-openvpn|openvpn"),
        ("3x-ui", "3x-ui", "server", r"3x-ui|(^|/)x-ui(:|$)"),
        ("marzban", "Marzban", "server", r"marzban|marzneshin"),
        ("remnawave", "Remnawave", "server", r"remnawave"),
        ("hiddify", "Hiddify", "server", r"hiddify"),
        ("xray", "Xray", "server", r"xray"),
        ("v2ray", "V2Ray", "server", r"v2ray|v2fly"),
        ("sing-box", "sing-box", "server", r"sing-?box"),
        ("wg", "WireGuard", "server", r"wg-easy|wireguard|wg-portal|wgportal"),
        ("outline", "Outline", "server", r"shadowbox|outline-ss-server"),
        ("ss", "Shadowsocks", "server", r"shadowsocks|(^|/)ss-server"),
        ("ipsec", "IPsec", "server", r"strongswan|ipsec-vpn|libreswan"),
        ("ocserv", "OpenConnect", "server", r"ocserv"),
        ("softether", "SoftEther", "server", r"softether"),
        ("pritunl", "Pritunl", "server", r"pritunl"),
        ("hysteria", "Hysteria", "server", r"hysteria"),
        ("trojan", "Trojan", "server", r"trojan"),
        ("naive", "NaiveProxy", "server", r"naiveproxy"),
        ("headscale", "Headscale", "server", r"headscale"),
        ("netbird", "NetBird", "server", r"netbirdio/(management|signal|relay)"),
        ("mtproto", "MTProto", "server", r"(^|/)mtg(:|@|$)|mtproto|telegrammessenger/proxy|telemt"),
        ("3proxy", "3proxy", "server", r"3proxy"),
        ("squid", "Squid", "server", r"squid"),
        ("dante", "Dante", "server", r"(^|/)dante|sockd"),
        ("gost", "GOST", "server", r"(^|/)gost(:|@|$)"),
        ("tailscale", "Tailscale", "mesh", r"tailscale/tailscale"),
    )
]
LABEL = {k: label for k, label, _r, _x in CATALOG} | {"netbird-client": "NetBird", "zerotier": "ZeroTier"}
# то же, что ищет условие установки helper'а на ноде (KERVAX_SETUP_WHEN в vpn-setup.sh): панель не
# просит helper там, где плейбук его не поставит. Тест сверяет, что каждое слово есть в условии
WHEN_WORDS = ("amnezia", "acontrol-awg", "xray", "v2ray", "v2fly", "sing-box", "singbox", "x-ui", "marzban", "marzneshin",
              "remnawave", "hiddify", "openvpn", "wireguard", "wg-easy", "wg-portal", "wgportal", "headscale",
              "netbirdio", "mtg", "mtproto", "telegrammessenger", "telemt", "shadowbox", "outline-ss", "shadowsocks", "ss-server",
              "strongswan", "ipsec", "libreswan", "ocserv", "softether", "pritunl", "hysteria", "trojan",
              "naiveproxy", "3proxy", "squid", "dante", "sockd", "gost")
# то же для helper'а vpnflow-setup (KERVAX_SETUP_WHEN в vpnflow-setup.sh): conntrack с внутренними адресами
# клиентов есть только там, где живет интерфейс туннеля. MTProto, Xray, Squid, 3proxy соединения открывают
# сами, условие на такой ноде ложно - панель не должна держать ее в kervax_outdated
FLOW_IF = ("wg", "awg", "tun", "tap")
FLOW_WORDS = ("amnezia", "wireguard", "wg-easy", "openvpn")

# Интерфейсы хоста: wg/awg - туннель, пока helper не скажет, что к нам подключаются клиенты.
# gre0/gretap0 ядро создает само при загрузке модуля ip_gre, amn0 - docker-сеть Amnezia
_IFACE: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^awg"), "awg"),
    (re.compile(r"^wg"), "wg"),
    (re.compile(r"^tailscale\d*$"), "tailscale"),
    (re.compile(r"^wt\d+$"), "netbird-client"),
    (re.compile(r"^zt[a-z0-9]{6,}$"), "zerotier"),
]
# что из интерфейсов само по себе - только участник меша
_MESH_IF = {"tailscale", "netbird-client", "zerotier"}
# протоколы Xray и родни, которые человек знает по имени: на бейдже вместо движка
_PROTO = {"vless": "VLESS", "vmess": "VMess", "trojan": "Trojan", "shadowsocks": "Shadowsocks",
          "hysteria": "Hysteria", "hysteria2": "Hysteria2", "tuic": "TUIC", "wireguard": "WireGuard",
          "socks": "SOCKS", "http": "HTTP"}
_XRAY_LIKE = {"xray", "3x-ui", "marzban", "remnawave", "hiddify", "v2ray", "sing-box"}


def kind_of_image(image: str) -> tuple[str, str] | None:
    """(ключ, роль) VPN-сервиса по образу контейнера или None"""
    img = (image or "").lower()
    if not img or _PANEL_RE.search(img):
        return None
    for k, _label, role, rx in CATALOG:
        if rx.search(img):
            return k, role
    return None


def _i(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _s(v, n: int = 64) -> str:
    return str(v or "")[:n] if isinstance(v, (str, int, float)) else ""


def block(rep: dict) -> dict | None:
    b = (rep.get("extras") or {}).get("vpn")
    return b if isinstance(b, dict) else None


def _from_report(rep: dict) -> dict[tuple[str, str], dict]:
    """Что видно по отчету агента: {(ключ, источник): сервис}"""
    out: dict[tuple[str, str], dict] = {}
    for c in (rep.get("docker") or {}).get("containers") or []:
        if not isinstance(c, dict) or c.get("state") != "running":
            continue
        kr = kind_of_image(str(c.get("image") or ""))
        if not kr:
            continue
        name = _s(c.get("name"), 80)
        ports = sorted({f"{_i(p.get('pub'))}/{p.get('proto') or 'tcp'}" for p in c.get("ports") or []
                        if isinstance(p, dict) and _i(p.get("pub"))})
        out[(kr[0], name)] = {"k": kr[0], "role": kr[1], "src": name, "ports": ports}
    for f in rep.get("net_ifaces") or []:
        name = str((f or {}).get("if") or "") if isinstance(f, dict) else ""
        for rx, k in _IFACE:
            if rx.search(name):
                # туннель wg/awg на хосте: роль скажет helper, без него - тише, как у меша
                out[(k, name)] = {"k": k, "role": "mesh", "src": name, "if": name, "ports": [], "guess": k not in _MESH_IF}
                break
    return out


def _client(c: dict, ts: int) -> dict | None:
    if not isinstance(c, dict):
        return None
    hs = _i(c.get("hs"))
    on = c.get("on")
    out = {
        "name": _s(c.get("name")),
        "ip": _s(c.get("ip"), 45),
        "hs": hs or None,
        "rx": _i(c.get("rx")),
        "tx": _i(c.get("tx")),
        "ep": _s(c.get("ep"), 45),
        # helper знает сам (OpenVPN, Headscale), иначе по свежести handshake на момент снимка
        "on": bool(on) if isinstance(on, bool) else bool(hs and ts and ts - hs <= ONLINE_S),
    }
    return out if out["name"] or out["ip"] or out["ep"] else None


def services(rep: dict, now_ts: float) -> list[dict]:
    """VPN и прокси на сервере для бейджа и раздела "VPN".

    [{k, label, short, role: server|mesh, src: [контейнеры и интерфейсы], ports, protos, n, on,
      clients: [{name, ip, hs, rx, tx, ep, on}]}], сначала то, что раздает. n - сколько клиентов
    заведено (None - не знаем), on - сколько на связи. У устаревшего блока helper'а клиенты остаются,
    но "на связи" никого: снимок мог быть сделан час назад."""
    seen = _from_report(rep)
    b = block(rep)
    ts = _i((b or {}).get("ts"))
    stale = bool(b) and (not ts or now_ts - ts > STALE)
    for sv in (b or {}).get("svc") or []:
        if not isinstance(sv, dict):
            continue
        k = _s(sv.get("k"), 24)
        if k not in LABEL:
            continue
        src = _s(sv.get("src"), 80) or "host"
        ifc = _s(sv.get("if"), 32)
        # интерфейс хоста helper называет по имени: wg0 из отчета и wg0 от helper'а - одно
        name = ifc or k if src == "host" else src
        cur = seen.setdefault((k, name), {"k": k, "src": name, "ports": []})
        if sv.get("role") in ("server", "mesh"):
            # у контейнера с несколькими интерфейсами хватает одного, который раздает
            cur["role"] = "server" if cur.get("helper_role") == "server" else sv["role"]
            cur["helper_role"] = cur["role"]
            cur.pop("guess", None)
        cur.setdefault("role", "server")
        if _i(sv.get("port")) and not cur["ports"]:
            cur["ports"] = [f"{_i(sv.get('port'))}/{_s(sv.get('proto'), 4) or 'udp'}"]
        protos = [p for p in (sv.get("protos") or []) if isinstance(p, str) and p in _PROTO]
        if protos:
            cur["protos"] = sorted(set(cur.get("protos", [])) | set(protos))
        routes = [_s(r, 45) for r in sv.get("routes") or [] if isinstance(r, str)][:8]
        if routes:
            cur["routes"] = routes
        if sv.get("exit") is True:
            cur["exit"] = True
        if _s(sv.get("err"), 200):
            cur["err"] = _s(sv.get("err"), 200)
        cl = [x for x in (_client(c, 0 if stale else ts) for c in (sv.get("clients") or [])[:MAX_CLIENTS]) if x]
        if "n" in sv or cl:
            # n не прислан (OpenVPN без списка выданных) - заведено неизвестно сколько, видно подключенных
            if "n" in sv:
                cur["n"] = (cur.get("n") or 0) + _i(sv.get("n"))
            cur.setdefault("n", None)
            cur["clients"] = cur.get("clients", []) + cl
            cur["on"] = cur.get("on", 0) + sum(1 for x in cl if x["on"])
    # одинаковые по ключу и роли - одной строкой: два контейнера OpenVPN, awg2 и awg3 на одной ноде
    merged: dict[tuple[str, str], dict] = {}
    for sv in seen.values():
        m = merged.setdefault((sv["k"], sv["role"]), {
            "k": sv["k"], "label": LABEL.get(sv["k"], sv["k"]), "role": sv["role"], "src": [], "ports": [],
            "protos": [], "n": None, "on": 0, "clients": [], "parts": [],
        })
        m["src"].append(sv["src"])
        m["ports"] += [p for p in sv["ports"] if p not in m["ports"]]
        m["protos"] = sorted(set(m["protos"]) | set(sv.get("protos") or []))
        if sv.get("guess"):
            m["guess"] = True
        if sv.get("routes"):
            m["routes"] = sv["routes"]
        if sv.get("exit"):
            m["exit"] = True
        if "clients" in sv:
            m["helper"] = True
            if sv.get("n") is not None:
                m["n"] = (m["n"] or 0) + sv["n"]
            m["on"] += sv.get("on", 0)
            m["clients"] += [{**c, "src": sv["src"]} for c in sv["clients"]]
        m["parts"].append({"src": sv["src"], "n": sv.get("n"), "on": sv.get("on", 0), "err": sv.get("err", ""),
                           "ports": sv["ports"]})
    out = []
    for m in merged.values():
        names = [_PROTO[p] for p in m["protos"]]
        # у Xray и его панелей человек знает протокол (VLESS), а не движок
        m["short"] = ", ".join(names) if names and m["k"] in _XRAY_LIKE else m["label"]
        m["clients"].sort(key=lambda x: (not x["on"], -(x["hs"] or 0), x["name"]))
        out.append(m)
    out.sort(key=lambda m: (m["role"] != "server", m["label"].lower()))
    return out


def view(rep: dict, now_ts: float) -> dict | None:
    """Поле ServerOut.vpn: сервисы и состояние helper'а (None - VPN на сервере нет)"""
    svc = services(rep, now_ts)
    if not svc:
        return None
    b = block(rep)
    ts = _i((b or {}).get("ts"))
    return {
        "svc": svc,
        "helper": bool(b),
        "ts": ts or None,
        "stale": bool(b) and (not ts or now_ts - ts > STALE),
    }


def needs_helper(rep: dict) -> bool:
    """Нужен ли helper vpn-setup: на сервере что-то раздает VPN, туннель wg с неизвестной ролью или
    helper уже присылает блок. Один клиент меша Tailscale - не повод"""
    if block(rep):
        return True
    return any(m["role"] == "server" or m.get("guess") for m in services(rep, 0))


def needs_flow_helper(rep: dict) -> bool:
    """Нужен ли helper vpnflow-setup: интерфейс wg/awg/tun/tap на хосте или работающий контейнер,
    у которого в имени или образе слово из FLOW_WORDS (условие на ноде ищет их во всей строке docker ps:
    у Amnezia Control образ acontrol-awg3, а контейнер amnezia-awg3), или helper уже присылает блок"""
    if "vpnflow" in (rep.get("extras") or {}):
        return True
    for f in rep.get("net_ifaces") or []:
        if isinstance(f, dict) and str(f.get("if") or "").startswith(FLOW_IF):
            return True
    for c in (rep.get("docker") or {}).get("containers") or []:
        if not isinstance(c, dict) or c.get("state") != "running":
            continue
        s = f"{c.get('name') or ''} {c.get('image') or ''}".lower()
        if any(w in s for w in FLOW_WORDS):
            return True
    return False


def clients(rep: dict, now_ts: float) -> list[dict]:
    """Все клиенты VPN сервера плоским списком: {k, src, name, ip, hs, rx, tx, ep, on}.

    ip - внутренний адрес клиента в туннеле (у Xray его нет, пусто), по нему трафик клиента
    находится в conntrack и счетчиках. Устаревший блок - клиентов нет: адреса могли уйти другим."""
    b = block(rep)
    ts = _i((b or {}).get("ts"))
    if not b or not ts or now_ts - ts > STALE:
        return []
    out = []
    for m in services(rep, now_ts):
        if m["role"] != "server":
            continue
        out += [{"k": m["k"], **c} for c in m["clients"]]
    return out

