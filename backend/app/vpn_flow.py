"""Клиенты VPN: кто, похоже, качает торрент и кто качает очень много.

VPN-сервер (AmneziaWG, WireGuard, OpenVPN) прячет клиентов за NAT: на хосте все потоки всех
клиентов выглядят одним адресом. Внутренний адрес клиента (10.8.x.y) знает только таблица
conntrack того netns, где живет VPN-интерфейс (у Amnezia - netns контейнера). Агент работает без
прав и туда не заглядывает. Helper vpnflow-setup раз в минуту находит netns с VPN-интерфейсом,
читает его conntrack через netlink и по внутреннему адресу считает: потоки, UDP, разные удаленные
адреса, разные адреса на высоких портах мимо обычных служб (hp), потоки на порты BitTorrent. extras
"vpnflow" - {"v": 1, "ts", "nets": [{"where", "ifaces", "flows", "clients": [{"ip", "flows", "udp",
"peers", "hp", "bt", "h"}]}], "skipped": [{"where", "why", "ifaces", "count"}]}; h - hp за каждую
минуту из последних 15, по ним видно, держится ли признак или это был всплеск.

Торрент - это сотни разных пиров на случайных высоких портах, в основном UDP (DHT, uTP). У
браузера, мессенджера, видеозвонка десятки адресов, почти все на 443: на живом сервере с 40
клиентами у обычных клиентов hp было 0-3. Признак - hp от TORRENT_HP хотя бы TORRENT_MIN минут из
последних 15; объявленный держится, пока таких минут не меньше TORRENT_HOLD.

Имя клиента, адрес подключения и трафик - из списка клиентов VPN (app/vpn.py, helper vpn-setup):
клиенты определяются в одном месте, здесь только присоединяются по внутреннему адресу. Трафик -
накопительные rx/tx пира, панель раз в SAMPLE_S запоминает их (alert_state["vpnctr"]) и считает,
сколько клиент прокачал за HEAVY_WINDOW. Много - от HEAVY_BYTES; отбой - когда меньше половины.
Счетчик сбросился (перезапуск контейнера) - история клиента начинается заново, а не "минус".

У Xray клиента по conntrack не видно: соединения открывает сам xray. Для него только трафик.

Заглушить клиента можно насовсем (alert_mutes "torrent:<ключ>", "vpnheavy:<ключ>") или на время
(alert_snoozes с тем же ключом); ключ - "<контейнер или host>|<внутренний адрес или имя>".
"""
from __future__ import annotations

from datetime import datetime, timezone

from app import tg_fmt
from app import vpn as vpn_mod
from app.models import Server

MUTE_T = "torrent:"
MUTE_H = "vpnheavy:"
STATE_KEY = "vpnctr"
MAX_AGE = 600  # блок старше 10 минут - helper встал, судить не по чему
TORRENT_HP = 80  # разных адресов на высоких портах за минуту: у обычных клиентов 0-3
TORRENT_MIN = 10  # минут из последних 15
TORRENT_HOLD = 3
HEAVY_WINDOW = 3 * 3600
HEAVY_BYTES = 30 * 1024 ** 3  # за 3 часа в среднем ~24 Мбит/с подряд: скачать фильм так не выйдет
SAMPLE_S = 300
KEEP_S = HEAVY_WINDOW + 3600


def _i(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def block(rep: dict) -> dict | None:
    b = ((rep or {}).get("extras") or {}).get("vpnflow")
    if not isinstance(b, dict) or b.get("v") != 1:
        return None
    ts = float(b.get("ts") or 0)
    ref = float((rep or {}).get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    if ts <= 0 or ref - ts > MAX_AGE:
        return None
    return b


def _src(where: str) -> str:
    """"container amnezia-awg3" -> "amnezia-awg3", "host" -> "host" """
    return where[len("container "):] if where.startswith("container ") else (where or "host")


def key(src: str, ident: str) -> str:
    return f"{src or 'host'}|{ident}"


def _ref(rep: dict) -> float:
    return float((rep or {}).get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()


def _clients(rep: dict) -> list[dict]:
    try:
        return vpn_mod.clients(rep or {}, _ref(rep))
    except Exception:  # noqa: BLE001 - чужой блок кривой: без имен, но торрент видно
        return []


def has_clients(rep: dict) -> bool:
    return bool(_clients(rep))


def _who(rep: dict) -> dict[str, dict]:
    """Клиенты VPN по внутреннему адресу: {"src|ip": клиент, "ip": клиент}"""
    out: dict[str, dict] = {}
    for c in _clients(rep):
        ip = str(c.get("ip") or "").split("/")[0]
        if not ip:
            continue
        out[key(str(c.get("src") or ""), ip)] = c
        out.setdefault(ip, c)
    return out


def _hot(h) -> int:
    return sum(1 for x in h or [] if isinstance(x, (int, float)) and x >= TORRENT_HP)


def flows(rep: dict) -> list[dict]:
    """Клиенты по conntrack плоским списком с именем и адресом подключения:
    {key, src, ip, name, ep, flows, udp, peers, hp, bt, hot, h}"""
    b = block(rep)
    if b is None:
        return []
    who = _who(rep)
    out = []
    for n in b.get("nets") or []:
        if not isinstance(n, dict):
            continue
        src = _src(str(n.get("where") or ""))
        for c in n.get("clients") or []:
            if not isinstance(c, dict) or not c.get("ip"):
                continue
            ip = str(c["ip"])[:45]
            cl = who.get(key(src, ip)) or who.get(ip) or {}
            h = [_i(x) for x in (c.get("h") or [])[-15:]]
            out.append({"key": key(src, ip), "src": src, "ip": ip, "name": str(cl.get("name") or "")[:64],
                        "ep": str(cl.get("ep") or "")[:45], "flows": _i(c.get("flows")), "udp": _i(c.get("udp")),
                        "peers": _i(c.get("peers")), "hp": _i(c.get("hp")), "bt": _i(c.get("bt")),
                        "hot": _hot(h), "h": h})
    return out


# ---- трафик клиентов по счетчикам ----

def _total(c: dict) -> int:
    return _i(c.get("rx")) + _i(c.get("tx"))


def _ckey(c: dict) -> str:
    ip = str(c.get("ip") or "").split("/")[0]
    return key(str(c.get("src") or ""), ip or str(c.get("name") or ""))


def update(state: dict | None, rep: dict, now: datetime) -> dict:
    """alert_state["vpnctr"]: {ключ клиента: [[ts, rx+tx], ...]} раз в SAMPLE_S, за KEEP_S"""
    st = dict(state or {})
    t = int(now.timestamp())
    seen = set()
    for c in _clients(rep):
        k = _ckey(c)
        if k.endswith("|") or k in seen:
            continue
        seen.add(k)
        tot = _total(c)
        h = [x for x in st.get(k) or [] if isinstance(x, list) and len(x) == 2 and t - _i(x[0]) <= KEEP_S]
        if h and tot < _i(h[-1][1]):
            h = []  # счетчик сбросился: контейнер перезапустили, клиента пересоздали
        if not h or t - _i(h[-1][0]) >= SAMPLE_S:
            h.append([t, tot])
        st[k] = h
    for k in list(st):
        h = [x for x in st[k] or [] if isinstance(x, list) and len(x) == 2 and t - _i(x[0]) <= KEEP_S]
        if h:
            st[k] = h
        else:
            st.pop(k)
    return st


def volume(state: dict | None, k: str, now: datetime, window: int = HEAVY_WINDOW) -> tuple[int, int]:
    """(байт за окно, за сколько секунд это видно): от последнего замера до самого раннего в окне"""
    h = [x for x in (state or {}).get(k) or [] if isinstance(x, list) and len(x) == 2]
    if len(h) < 2:
        return 0, 0
    t = int(now.timestamp())
    last = h[-1]
    first = next((x for x in h if t - _i(x[0]) <= window), last)
    return max(0, _i(last[1]) - _i(first[1])), max(0, _i(last[0]) - _i(first[0]))


# ---- заглушки ----

def _muted(s: Server, now: datetime, prefix: str) -> set[str]:
    keys = {m[len(prefix):] for m in (s.alert_mutes or []) if m.startswith(prefix)}
    for k, until in (getattr(s, "alert_snoozes", None) or {}).items():
        if not str(k).startswith(prefix):
            continue
        try:
            u = datetime.fromisoformat(str(until))
        except ValueError:
            continue
        if (u if u.tzinfo else u.replace(tzinfo=timezone.utc)) > now:
            keys.add(k[len(prefix):])
    return keys


def muted_torrent(s: Server, now: datetime) -> set[str]:
    return _muted(s, now, MUTE_T)


def muted_heavy(s: Server, now: datetime) -> set[str]:
    return _muted(s, now, MUTE_H)


def _held(s: Server, kind: str) -> set[str]:
    return {str(k) for k in ((s.alert_state or {}).get(f"{kind}_sig") or [])}


def who_text(x: dict) -> str:
    """"alice (10.8.3.9, с 203.0.113.5)", без имени - "10.8.3.9 (с 203.0.113.5)" """
    name, ip, ep = x.get("name") or "", x.get("ip") or "", x.get("ep") or ""
    ep = ep.rsplit(":", 1)[0].strip("[]") if ep.count(":") == 1 or ep.startswith("[") else ep
    if name:
        inner = ", ".join(v for v in (ip, f"с {ep}" if ep else "") if v)
        return f"{name} ({inner})" if inner else name
    return f"{ip} (с {ep})" if ep and ip else ip or name


def signs(x: dict) -> str:
    """"320 адресов на высоких портах, 60% UDP, 1800 потоков" """
    parts = [f"{x['hp']} адресов на высоких портах"]
    if x.get("flows"):
        parts.append(f"{round(100 * x['udp'] / x['flows'])}% UDP")
        parts.append(f"{tg_fmt.count(x['flows'])} потоков")
    if x.get("bt"):
        parts.append(f"{x['bt']} на портах BitTorrent")
    return ", ".join(parts)


def torrent_problems(s: Server, now: datetime) -> list[dict]:
    """Похоже на торрент, без заглушенных: {"key", "level", "text", "who", "signs", "vol", ...}"""
    rep = s.last_report or {}
    held, muted = _held(s, "vpn_torrent"), muted_torrent(s, now)
    ctr = (s.alert_state or {}).get(STATE_KEY) or {}
    out = []
    for x in flows(rep):
        if x["key"] in muted:
            continue
        need = TORRENT_HOLD if x["key"] in held else TORRENT_MIN
        if x["hot"] < need:
            continue
        vol, span = volume(ctr, x["key"], now)
        w = who_text(x)
        text = f"Похоже, качает торрент: {w} в {x['src']} - {signs(x)}"
        if vol:
            text += f", {tg_fmt.size(vol)} за {tg_fmt.dur(span)}"
        out.append({**x, "level": 1, "who": w, "signs": signs(x), "vol": vol, "span": span, "text": text})
    out.sort(key=lambda x: (-x["hp"], x["key"]))
    return out


def heavy_problems(s: Server, now: datetime) -> list[dict]:
    """Клиенты, прокачавшие за HEAVY_WINDOW от HEAVY_BYTES, без заглушенных"""
    rep = s.last_report or {}
    held, muted = _held(s, "vpn_heavy"), muted_heavy(s, now)
    ctr = (s.alert_state or {}).get(STATE_KEY) or {}
    fl = {x["key"]: x for x in flows(rep)}
    out = []
    for c in _clients(rep):
        k = _ckey(c)
        if k in muted or k.endswith("|"):
            continue
        vol, span = volume(ctr, k, now)
        # окно должно быть видно почти целиком: два замера за 10 минут - не "за 3 часа"
        if span < HEAVY_WINDOW * 0.8:
            continue
        lim = HEAVY_BYTES * (0.5 if k in held else 1)
        if vol < lim:
            continue
        x = {"key": k, "src": str(c.get("src") or "host"), "ip": str(c.get("ip") or "").split("/")[0],
             "name": str(c.get("name") or "")[:64], "ep": str(c.get("ep") or "")[:45]}
        w = who_text(x)
        text = f"Клиент VPN качает очень много: {w} в {x['src']} - {tg_fmt.size(vol)} за {tg_fmt.dur(span)}"
        f = fl.get(k)
        if f and f["hot"]:
            text += ", похоже на торрент"
        out.append({**x, "level": 1, "who": w, "vol": vol, "span": span, "torrent": bool(f and f["hot"]),
                    "text": text})
    out.sort(key=lambda x: (-x["vol"], x["key"]))
    return out


# ---- короткий алерт в Telegram (формат A, app/tg_fmt.py) ----

def tg_short_torrent(items: list[dict]) -> tuple[str, list[str]] | None:
    """"Похоже, качает торрент / Клиент: alice (10.8.3.9, с 203.0.113.5) / 320 адресов на высоких портах, ..." """
    if not items:
        return None
    x = items[0]
    facts = [tg_fmt.fact("Клиент", who_text(x))]
    sg = x["signs"] + (f", {tg_fmt.size(x['vol'])} за {tg_fmt.dur(x['span'])}" if x.get("vol") else "")
    if len(items) > 1:
        sg = tg_fmt.fact("Еще", tg_fmt.names([y.get("name") or y.get("ip") for y in items[1:]]))
    facts.append(sg)
    return "Похоже, качает торрент", facts


def tg_short_heavy(items: list[dict]) -> tuple[str, list[str]] | None:
    """"Клиент VPN качает очень много / Клиент: alice (...) / 42гб за 3 ч" """
    if not items:
        return None
    x = items[0]
    facts = [tg_fmt.fact("Клиент", who_text(x))]
    v = f"{tg_fmt.size(x['vol'])} за {tg_fmt.dur(x['span'])}" + (", похоже на торрент" if x.get("torrent") else "")
    if len(items) > 1:
        v += ", еще " + tg_fmt.names([y.get("name") or y.get("ip") for y in items[1:]])
    facts.append(v)
    return "Клиент VPN качает очень много", facts


# ---- карточка ----

def view(s: Server, now: datetime) -> dict | None:
    """Поле ServerOut.vpn_flow: {"ts", "clients": [...], "skipped": [...], "torrent_hp", "heavy_gb"}.
    У клиента потоки, адреса, UDP, признак торрента сейчас (hot - минут из 15), трафик за 3 часа,
    беда и мьюты. Клиенты без потоков, но с трафиком (Xray) - тоже, без потоков"""
    rep = s.last_report or {}
    b = block(rep)
    clients = _clients(rep)
    if b is None and not clients:
        return None
    ctr = (s.alert_state or {}).get(STATE_KEY) or {}
    tp = {x["key"] for x in torrent_problems(s, now)}
    hp = {x["key"] for x in heavy_problems(s, now)}
    mt, mh = muted_torrent(s, now), muted_heavy(s, now)
    perm = set(s.alert_mutes or [])
    snz = {str(k): str(v) for k, v in (getattr(s, "alert_snoozes", None) or {}).items()}

    def mute(prefix: str, k: str) -> dict:
        return {"perm": True} if prefix + k in perm else {"until": snz.get(prefix + k, "")}

    rows: dict[str, dict] = {}
    for x in flows(rep):
        rows[x["key"]] = {k: x[k] for k in ("key", "src", "ip", "name", "ep", "flows", "udp", "peers", "hp", "bt", "hot", "h")}
    for c in clients:
        k = _ckey(c)
        if k.endswith("|"):
            continue
        r = rows.setdefault(k, {"key": k, "src": str(c.get("src") or "host"),
                                "ip": str(c.get("ip") or "").split("/")[0], "name": str(c.get("name") or "")[:64],
                                "ep": str(c.get("ep") or "")[:45]})
        vol, span = volume(ctr, k, now)
        if span:
            r["vol"], r["span"] = vol, span
    out = []
    for k, r in rows.items():
        if k in tp:
            r["torrent"] = True
        if k in hp:
            r["heavy"] = True
        if k in mt:
            r["mute_t"] = mute(MUTE_T, k)
        if k in mh:
            r["mute_h"] = mute(MUTE_H, k)
        out.append(r)
    out.sort(key=lambda r: (not (r.get("torrent") or r.get("heavy")), -r.get("hot", 0), -r.get("vol", 0),
                            -r.get("flows", 0), r["key"]))
    sk = [{"where": _src(str(x.get("where") or "")), "why": str(x.get("why") or "")[:80],
           "count": _i(x.get("count"))} for x in (b or {}).get("skipped") or [] if isinstance(x, dict)]
    return {"ts": _i((b or {}).get("ts")) or None, "helper": b is not None, "clients": out[:300], "skipped": sk,
            "torrent_hp": TORRENT_HP, "torrent_min": TORRENT_MIN, "heavy_bytes": HEAVY_BYTES,
            "heavy_window": HEAVY_WINDOW}
