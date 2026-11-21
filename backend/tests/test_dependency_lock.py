"""Lock-файлы бэкенда: requirements*.in повторяют pyproject.toml, lock закрепляет версии и хеши.

Образ и CI ставят пакеты только из lock (--require-hashes --no-deps). Зависимость, добавленная
в pyproject.toml мимо lock, в образ не попала бы, а поднятая нижняя граница - не сработала бы.
Свежесть lock относительно .in проверяет CI (sh lock.sh --check), здесь - без сети.
"""
import importlib.util
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

BACKEND = Path(__file__).resolve().parents[1]
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def _pyproject():
    return tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))


def _key(req: Requirement):
    return canonicalize_name(req.name), frozenset(req.extras), str(req.specifier), str(req.marker or "")


def _in_file(name):
    """Требования и опции (-r/-c) из requirements*.in."""
    reqs, options = [], []
    for line in (BACKEND / name).read_text(encoding="utf-8").splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        (options if line.startswith("-") else reqs).append(line)
    return [Requirement(r) for r in reqs], options


def _lock(name):
    """Записи lock: [(Requirement, хеши)], строки-продолжения склеены."""
    text = (BACKEND / name).read_text(encoding="utf-8").replace("\\\n", " ")
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        spec = line.split("--hash=", 1)[0].strip()
        entries.append((Requirement(spec), HASH.findall(line)))
    return entries


def test_runtime_in_repeats_pyproject():
    data = _pyproject()
    want = data["project"]["dependencies"] + data["build-system"]["requires"]
    reqs, options = _in_file("requirements.in")
    assert options == []
    assert sorted(map(_key, reqs)) == sorted(_key(Requirement(r)) for r in want), \
        "requirements.in и pyproject.toml ([project].dependencies + [build-system].requires) разошлись"


def test_dev_in_repeats_pyproject_extra():
    dev = [Requirement(r) for r in _pyproject()["project"]["optional-dependencies"]["dev"]]
    reqs, options = _in_file("requirements-dev.in")
    assert options == ["-r requirements.in", "-c requirements.txt"]
    keys = set(map(_key, reqs))
    missing = [str(r) for r in dev if _key(r) not in keys]
    assert not missing, f"нет в requirements-dev.in: {missing}"
    # генератор lock закреплен точно: от его версии зависит вид файла
    assert any(canonicalize_name(r.name) == "uv" and str(r.specifier).startswith("==") for r in reqs)


def test_locks_pin_exact_versions_with_hashes():
    for name in ("requirements.txt", "requirements-dev.txt"):
        head = (BACKEND / name).read_text(encoding="utf-8")[:600]
        # один файл на Python 3.12 (CI) и 3.14 (образ): универсальный lock от минимальной версии
        assert "uv pip compile" in head and "--universal" in head and "--python-version 3.12" in head, name
        assert "--generate-hashes" in head, name
        entries = _lock(name)
        assert entries, name
        for req, hashes in entries:
            assert len(req.specifier) == 1 and next(iter(req.specifier)).operator == "==", f"{name}: {req}"
            assert not req.url and hashes, f"{name}: {req} без хеша"


def test_lock_satisfies_pyproject_and_dev_lock_contains_runtime():
    runtime, dev = _lock("requirements.txt"), _lock("requirements-dev.txt")
    pins = {}
    for req, _ in runtime:
        pins.setdefault(canonicalize_name(req.name), []).append(next(iter(req.specifier)).version)
    data = _pyproject()
    for raw in data["project"]["dependencies"] + data["build-system"]["requires"]:
        req = Requirement(raw)
        versions = pins.get(canonicalize_name(req.name))
        assert versions, f"{req.name} нет в requirements.txt"
        assert all(req.specifier.contains(v, prereleases=True) for v in versions), \
            f"requirements.txt: {req.name} {versions} не подходит под {req.specifier}"
    dev_entries = {(canonicalize_name(r.name), str(r.specifier), str(r.marker or "")) for r, _ in dev}
    for req, _ in runtime:
        assert (canonicalize_name(req.name), str(req.specifier), str(req.marker or "")) in dev_entries, \
            f"requirements-dev.txt ставит не ту версию {req.name}, что образ"


# --- ops/lock-hashes.py: хеши lock сверяются с файлами на PyPI (sh lock.sh --check, с сетью).
# Здесь без сети - разбор lock и сравнение на синтетических данных.
_spec = importlib.util.spec_from_file_location("lock_hashes", BACKEND.parent / "ops/lock-hashes.py")
lock_hashes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lock_hashes)

WHL, WHL2, SDIST = "a" * 64, "b" * 64, "c" * 64
PYPI = {("samplepkg", "1.2.3"): [
    {"digests": {"sha256": WHL}, "packagetype": "bdist_wheel"},
    {"digests": {"sha256": WHL2}, "packagetype": "bdist_wheel"},  # колесо, которого нет в lock
    {"digests": {"sha256": SDIST}, "packagetype": "sdist"},
]}


def _sample(*hashes, marker=""):
    lines = [f"samplepkg==1.2.3{marker} \\"] + [f"    --hash=sha256:{h} \\" for h in hashes]
    return "# header\n" + "\n".join(lines).rstrip(" \\") + "\n    # via -r requirements.in\n"


def test_lock_hashes_accepts_a_subset_of_pypi_files():
    entries = lock_hashes.parse_lock(_sample(WHL, SDIST, marker=" ; sys_platform != 'win32'"))
    assert entries == [{"name": "samplepkg", "version": "1.2.3", "hashes": [("sha256", WHL), ("sha256", SDIST)]}]
    assert lock_hashes.compare(entries, PYPI) == []


def test_lock_hashes_rejects_substituted_extra_or_sdist_only_hashes():
    bad = {
        "substituted": _sample("0" * 64),
        "extra": _sample(WHL, "d" * 64),
        "sdist only": _sample(SDIST),
        "no hashes": "samplepkg==1.2.3\n",
    }
    for case, text in bad.items():
        assert lock_hashes.compare(lock_hashes.parse_lock(text), PYPI), case
    assert lock_hashes.compare(lock_hashes.parse_lock(_sample(WHL).replace("1.2.3", "9.9.9")), PYPI)


def test_lock_hashes_parses_the_real_locks():
    for name in ("requirements.txt", "requirements-dev.txt"):
        entries = lock_hashes.parse_lock((BACKEND / name).read_text(encoding="utf-8"))
        assert len(entries) == len(_lock(name)) and all(e["hashes"] for e in entries), name
