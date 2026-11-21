#!/usr/bin/env python3
"""Хеши lock-файлов бэкенда - настоящие файлы с PyPI (backend/lock.sh --check).

    python3 ops/lock-hashes.py backend/requirements.txt backend/requirements-dev.txt

pip с --require-hashes проверяет файл по хешу из lock, но не то, откуда хеш взялся: подмененный
или лишний хеш в PR выглядит как обычная строка. Здесь каждый --hash каждого пина должен быть
sha256 одного из файлов этой версии на PyPI (JSON API), и среди хешей пина должно быть колесо:
образ и CI ставят с --only-binary :all:, sdist туда не попадет. Новые колеса на PyPI проверку
не роняют: требуется lock ⊆ PyPI, а не равенство. Подходит ли колесо под платформу и Python,
проверяет сама установка с --only-binary в CI (3.12, 3.14) и в образе.
Только stdlib: запускается до установки чего-либо.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)\s*(?:;\s*(.*?))?\s*$")
HASH = re.compile(r"--hash=(\w+):(\S+)")


def parse_lock(text: str) -> list[dict]:
    """Записи lock: имя, версия, хеши. Строки-продолжения склеены, комментарии отброшены."""
    entries = []
    for line in text.replace("\\\n", " ").splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        spec = line.split("--hash=", 1)[0].strip()
        m = PIN.match(spec)
        if not m:
            raise ValueError(f"не пин name==version: {spec!r}")
        entries.append({"name": m.group(1).lower(), "version": m.group(2),
                        "hashes": HASH.findall(line)})
    return entries


def compare(entries: list[dict], pypi: dict[tuple[str, str], list[dict]]) -> list[str]:
    """Ошибки сверки. pypi[(name, version)] - urls из JSON API: digests.sha256, packagetype."""
    errors = []
    for e in entries:
        where = f"{e['name']}=={e['version']}"
        files = pypi.get((e["name"], e["version"]))
        if not files:
            errors.append(f"{where}: нет файлов на PyPI")
            continue
        known = {f["digests"]["sha256"]: f["packagetype"] for f in files}
        if not e["hashes"]:
            errors.append(f"{where}: нет хешей")
        for algo, digest in e["hashes"]:
            if algo != "sha256" or digest not in known:
                errors.append(f"{where}: хеш {algo}:{digest} не совпадает ни с одним файлом на PyPI")
        if not any(known.get(d) == "bdist_wheel" for a, d in e["hashes"] if a == "sha256"):
            errors.append(f"{where}: нет хеша ни одного колеса (--only-binary)")
    return errors


def fetch(name: str, version: str) -> list[dict]:
    url = f"https://pypi.org/pypi/{quote(name)}/{quote(version)}/json"
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=30) as r:
                return json.load(r)["urls"]
        except HTTPError as exc:
            if exc.code == 404:
                return []
            err = exc
        except (URLError, TimeoutError, ValueError) as exc:
            err = exc
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"PyPI недоступен для {name}=={version}: {err}")


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    lock = {p: parse_lock(open(p, encoding="utf-8").read()) for p in paths}
    keys = sorted({(e["name"], e["version"]) for entries in lock.values() for e in entries})
    try:
        with ThreadPoolExecutor(8) as pool:
            pypi = dict(zip(keys, pool.map(lambda k: fetch(*k), keys)))
    except RuntimeError as exc:
        print(f"lock-hashes: {exc}", file=sys.stderr)
        return 1
    rc = 0
    for path, entries in lock.items():
        errors = compare(entries, pypi)
        for err in errors:
            print(f"{path}: {err}", file=sys.stderr)
        rc |= bool(errors)
        print(f"{path}: {len(entries)} пинов, хеши {'не сходятся' if errors else 'совпадают с PyPI'}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
