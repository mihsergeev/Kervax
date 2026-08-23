#!/usr/bin/env python3
"""Собирает офлайн-таблицу IP → страна.

Зачем не внешний сервис в рантайме: панель не должна отправлять IP-адреса всего
парка третьей стороне ради флажка, да и работать это обязано без интернета.

Источник — DB-IP IP-to-Country Lite (CC BY 4.0, https://db-ip.com). Он даёт
ГЕОЛОКАЦИЮ, а не владельца диапазона, и это принципиально: делегированные файлы
RIR на диапазоны Hetzner отвечают DE (компания немецкая), хотя железо может
стоять в Хельсинки и правильный ответ FI. Проверено на живом парке.

Резерв — delegated-extended файлы пяти RIR: если DB-IP недоступен, лучше менее
точная таблица, чем никакой. В шапке готового файла помечено, чем собрано.

Результат — backend/app/data/geoip.csv.gz: строки «<4|6> start CC», IPv4 числом,
IPv6 в hex. Таблица сплошная и отсортированная: конец диапазона равен началу
следующего минус один, поэтому его не храним, а ZZ означает «страна неизвестна».

Запуск (нужен интернет, обновлять раз в несколько месяцев):
    python ops/build-geoip.py
"""

import datetime
import gzip
import ipaddress
import os
import sys
import urllib.request

DBIP_URL = "https://download.db-ip.com/free/dbip-country-lite-{ym}.csv.gz"
RIRS = {
    "arin": "https://ftp.arin.net/pub/stats/arin/delegated-arin-extended-latest",
    "ripencc": "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-extended-latest",
    "apnic": "https://ftp.apnic.net/stats/apnic/delegated-apnic-extended-latest",
    "lacnic": "https://ftp.lacnic.net/pub/stats/lacnic/delegated-lacnic-extended-latest",
    "afrinic": "https://ftp.afrinic.net/stats/afrinic/delegated-afrinic-extended-latest",
}

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "backend", "app", "data", "geoip.csv.gz")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "kervax-geoip-build"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def from_dbip(v4: list, v6: list) -> str:
    """Пробуем текущий месяц, затем два предыдущих: свежий срез выкладывают не 1-го числа."""
    today = datetime.date.today()
    for back in range(3):
        y, m = today.year, today.month - back
        while m <= 0:
            y, m = y - 1, m + 12
        url = DBIP_URL.format(ym=f"{y}-{m:02d}")
        try:
            raw = fetch(url)
        except Exception:  # noqa: BLE001 — просто нет такого среза, пробуем предыдущий
            continue
        for line in gzip.decompress(raw).decode("utf-8", "replace").splitlines():
            parts = line.split(",")
            if len(parts) != 3:
                continue
            a, b, cc = parts
            # ZZ (страна неизвестна) НЕ выбрасываем: он держит таблицу сплошной,
            # а сплошная таблица позволяет не хранить конец каждого диапазона
            if len(cc) != 2 or not cc.isalpha():
                continue
            try:
                lo, hi = ipaddress.ip_address(a), ipaddress.ip_address(b)
            except ValueError:
                continue
            (v4 if lo.version == 4 else v6).append((int(lo), int(hi), cc.upper()))
        return f"DB-IP IP-to-Country Lite {y}-{m:02d} (CC BY 4.0, https://db-ip.com)"
    return ""


def from_rirs(v4: list, v6: list) -> str:
    for name, url in RIRS.items():
        print(f"RIR {name}...", flush=True)
        try:
            text = fetch(url).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 — один недоступный RIR не валит сборку
            print(f"  !! {name}: {e}", file=sys.stderr)
            continue
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            f = line.split("|")
            # registry|cc|type|start|value|date|status[|extensions]
            if len(f) < 7 or f[6] not in ("allocated", "assigned"):
                continue
            cc, typ, start, value = f[1], f[2], f[3], f[4]
            if len(cc) != 2 or not cc.isalpha():
                continue
            try:
                if typ == "ipv4":
                    a = int(ipaddress.IPv4Address(start))
                    v4.append((a, a + int(value) - 1, cc.upper()))
                elif typ == "ipv6":
                    net = ipaddress.IPv6Network(f"{start}/{value}", strict=False)
                    v6.append((int(net.network_address), int(net.broadcast_address), cc.upper()))
            except (ValueError, ipaddress.AddressValueError):
                continue
    return "delegated-extended RIR (менее точно: страна ВЛАДЕЛЬЦА диапазона)" if v4 else ""


def solidify(rows: list, space: int) -> list:
    """Делает таблицу СПЛОШНОЙ: дыры между диапазонами закрывает записями ZZ.

    Это позволяет хранить только начало диапазона — конец всегда равен началу
    следующего минус один. Файл и память ужимаются вдвое, а поиск остаётся тем же
    bisect'ом по стартам."""
    out: list = []
    pos = 0
    for start, end, cc in rows:
        if start > pos:
            out.append((pos, "ZZ"))
        out.append((start, cc))
        pos = end + 1
    if pos < space:
        out.append((pos, "ZZ"))
    # после закрытия дыр соседи с одной страной снова могли слипнуться
    dedup: list = []
    for start, cc in out:
        if dedup and dedup[-1][1] == cc:
            continue
        dedup.append((start, cc))
    return dedup


def merge(rows: list) -> list:
    """Склеивает соседние диапазоны одной страны: таблица короче, поиск быстрее."""
    rows.sort()
    out: list = []
    for start, end, cc in rows:
        if out and out[-1][2] == cc and start <= out[-1][1] + 1:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end, cc)
            continue
        out.append((start, end, cc))
    return out


def main() -> int:
    v4: list = []
    v6: list = []
    print("istochnik: DB-IP...", flush=True)
    src = from_dbip(v4, v6)
    if not src:
        print("DB-IP nedostupen, perehozhu na RIR", file=sys.stderr)
        src = from_rirs(v4, v6)
    if not v4:
        print("ne sobrano ni odnogo diapazona - tablicu ne trogayu", file=sys.stderr)
        return 1
    v4, v6 = solidify(merge(v4), 2 ** 32), solidify(merge(v6), 2 ** 128)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # utf-8, а не ascii: в шапке кириллица, на ней сборка падала UnicodeEncodeError
    with gzip.open(OUT, "wt", encoding="utf-8", newline="\n") as f:
        f.write(f"# IP->страна, собрано ops/build-geoip.py\n# источник: {src}\n")
        for start, cc in v4:
            f.write(f"4 {start} {cc}\n")
        for start, cc in v6:
            f.write(f"6 {start:x} {cc}\n")
    size = os.path.getsize(OUT)
    # без стрелок и кириллицы в выводе: консоль Windows (cp1251) на них падает
    print(f"gotovo: IPv4 {len(v4)} + IPv6 {len(v6)} -> {OUT} ({size // 1024} KB)")
    print(f"source: {src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
