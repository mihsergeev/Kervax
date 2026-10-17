#!/usr/bin/env python3
"""Собирает офлайн-таблицу сроков поддержки ПО для "Версий без поддержки" (app/software.py).

Источник - endoflife.date (https://endoflife.date, данные под MIT): по каждому продукту циклы
версий (PHP 7.4, PostgreSQL 14, nginx 1.24) и дата конца поддержки, true - кончилась без даты,
false - поддерживается. Панель в рантайме никуда не ходит: работать ей надо и без интернета.

Результат - backend/app/data/eol.json: {"source", "built", "products": {код: {"name", "cycles":
[[цикл, срок], ...]}}}. Новые циклы появляются раз в несколько месяцев, сроки известных - заранее,
поэтому обновлять перед релизом раз в квартал:
    python ops/build-eol.py
"""

import datetime
import json
import os
import sys
import urllib.request

# код endoflife.date -> как называть людям
PRODUCTS = {
    "php": "PHP", "nodejs": "Node.js", "python": "Python", "ruby": "Ruby", "eclipse-temurin": "Java",
    "postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB", "redis": "Redis", "valkey": "Valkey",
    "mongodb": "MongoDB", "elasticsearch": "Elasticsearch", "clickhouse": "ClickHouse", "rabbitmq": "RabbitMQ",
    "zookeeper": "ZooKeeper", "nginx": "nginx", "traefik": "Traefik", "haproxy": "HAProxy", "caddy": "Caddy",
    "grafana": "Grafana", "alpine-linux": "Alpine", "ubuntu": "Ubuntu", "debian": "Debian", "centos": "CentOS",
}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend", "app", "data", "eol.json")


def fetch(code: str) -> list:
    req = urllib.request.Request(f"https://endoflife.date/api/{code}.json", headers={"User-Agent": "kervax-eol"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    products = {}
    for code, name in PRODUCTS.items():
        cycles = []
        for c in fetch(code):
            eol = c.get("eol")
            if not isinstance(eol, (str, bool)):
                continue
            if isinstance(eol, str):
                datetime.date.fromisoformat(eol)  # мусор в дате - упасть здесь, а не в панели
            cycles.append([str(c["cycle"]), eol])
        if not cycles:
            print(f"{code}: пусто", file=sys.stderr)
            return 1
        products[code] = {"name": name, "cycles": cycles}
        print(f"{code}: {len(cycles)}")
    data = {"source": "endoflife.date", "built": datetime.date.today().isoformat(), "products": products}
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"-> {os.path.normpath(OUT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
