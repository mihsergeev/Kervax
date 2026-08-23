"""Определение страны по IP по офлайн-таблице (собирает ops/build-geoip.py).

Никаких обращений наружу: панель не отправляет IP-адреса парка третьей стороне
ради флажка и работает без интернета.

Таблица сплошная и отсортированная по началу диапазона, поэтому конец хранить не
нужно — он равен началу следующего минус один, а bisect по началам даёт ответ за
один шаг. ZZ = страна неизвестна.

Память: адреса держим в array('Q') (8 байт на число), а не в списках Python —
700 тысяч int-объектов съели бы под сотню мегабайт. IPv6 не влезает в 64 бита,
поэтому разложен на две половины: старшая используется для поиска, младшая
уточняет границу.

Данные: DB-IP IP-to-Country Lite (CC BY 4.0, https://db-ip.com).
"""

import bisect
import gzip
import ipaddress
import os
import threading
from array import array

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "geoip.csv.gz")
_MASK64 = (1 << 64) - 1

_lock = threading.Lock()
_loaded = False
_v4 = array("Q")
_v4_cc = bytearray()
_v6_hi = array("Q")
_v6_lo = array("Q")
_v6_cc = bytearray()
_cache: dict[str, str] = {}


def _load() -> None:
    """Ленивая загрузка: разбор 700 тысяч строк не должен задерживать старт панели,
    а нужен он только когда в списке серверов реально есть адреса."""
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True  # даже при ошибке: без файла просто не показываем флаги
        try:
            with gzip.open(_DATA, "rt", encoding="utf-8") as f:
                for line in f:
                    if line[0] == "#":
                        continue
                    kind, start, cc = line.split()
                    if kind == "4":
                        _v4.append(int(start))
                        _v4_cc.extend(cc.encode("ascii"))
                    else:
                        n = int(start, 16)
                        _v6_hi.append(n >> 64)
                        _v6_lo.append(n & _MASK64)
                        _v6_cc.extend(cc.encode("ascii"))
        except (OSError, ValueError):
            return


def _cc_at(codes: bytearray, i: int) -> str:
    cc = codes[i * 2 : i * 2 + 2].decode("ascii")
    return "" if cc == "ZZ" else cc


def country_of(ip: str) -> str:
    """ISO-код страны в верхнем регистре или '' — если адрес приватный, битый,
    неизвестен таблице или таблица не собрана."""
    ip = (ip or "").strip()
    if not ip:
        return ""
    hit = _cache.get(ip)
    if hit is not None:
        return hit
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    # приватные/служебные адреса страны не имеют: у ноды за NAT вышел бы мусорный флаг
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
        _cache[ip] = ""
        return ""
    _load()
    out = ""
    if addr.version == 4:
        if _v4:
            i = bisect.bisect_right(_v4, int(addr)) - 1
            if i >= 0:
                out = _cc_at(_v4_cc, i)
    elif _v6_hi:
        n = int(addr)
        hi, lo = n >> 64, n & _MASK64
        # ищем по старшей половине, затем сдвигаемся назад, пока младшая меньше нужной:
        # в одной старшей половине может лежать несколько записей
        i = bisect.bisect_right(_v6_hi, hi) - 1
        while i >= 0 and _v6_hi[i] == hi and _v6_lo[i] > lo:
            i -= 1
        if i >= 0:
            out = _cc_at(_v6_cc, i)
    _cache[ip] = out
    return out
