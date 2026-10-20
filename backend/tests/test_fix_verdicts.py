"""Шаги "как исправить" сами говорят, вышло ли: sed, не нашедший строку, молчит, и дальше up -d
перезапускал прежний образ. Последняя строка правки и перезапуска - проверка с echo (compose_cmd.verdict).
Здесь: фразы проверок знает английская панель, и проверки на деле отличают удачу от неудачи (в sh с
заглушкой docker)."""
import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import compose_cmd
from tests.test_docker_risk import _c, _rep
from tests.test_docker_risk_fix import _blocks, _cm, _cmd
from tests.test_software import _grafana

ROOT = Path(__file__).resolve().parents[2]


def _phrases() -> set[str]:
    """Все фразы verdict(...) в коде бэкенда; у f"{n}: ..." - часть после имени контейнера."""
    out = set()
    for f in (ROOT / "backend" / "app").rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        # фраза в переменной (hand = "НЕ ИЗМЕНЕНО: ..." для Cmd.changed)
        names = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                 and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
                 for t in n.targets if isinstance(t, ast.Name)}
        for node in ast.walk(tree):
            fn = getattr(node, "func", None)
            name = getattr(fn, "id", getattr(fn, "attr", ""))
            if not isinstance(node, ast.Call) or name not in ("verdict", "changed"):
                continue
            for a in node.args[1:3] if name == "verdict" else node.args[1:2]:
                if isinstance(a, ast.Name) and a.id in names:
                    out.add(names[a.id])
                elif isinstance(a, ast.Constant) and isinstance(a.value, str):
                    out.add(a.value)
                elif isinstance(a, ast.JoinedStr):
                    out.add("".join(v.value for v in a.values if isinstance(v, ast.Constant)).removeprefix(": "))
    return out


def test_frontend_translates_every_verdict_phrase():
    src = ROOT / "frontend" / "src"
    if not src.is_dir():
        pytest.skip("нет исходников фронта")
    fix = (src / "FixSteps.tsx").read_text(encoding="utf-8")
    echo = set(re.findall(r"^  '([^']+)',$", re.search(r"const ECHO = \[\n(.*?)\n\]", fix, re.S).group(1), re.M))
    phrases = _phrases()
    assert len(phrases) >= 10
    assert phrases == echo
    i18n = (src / "i18n.tsx").read_text(encoding="utf-8")
    assert all(f"\n  '{p}':" in i18n for p in phrases)
    # фраза без кавычек, $ и !: она в двойных кавычках echo, а ! в bash - подстановка из истории
    assert not [p for p in phrases if re.search(r'["$`!\\]', p)]


def _sh() -> str | None:
    sh = shutil.which("sh")
    return sh if sh and shutil.which("md5sum") and shutil.which("grep") else None


STUB = """sudo() { "$@"; }
docker() {
  case "$1" in
    inspect)
      case "$3" in
        *Config.Image*) echo "$IMG" ;;
        *Privileged*) echo "$PRIV" ;;
        *Mounts*) printf '%s\\n' $MOUNTS ;;
      esac ;;
  esac
}
"""


def _run(tmp_path: Path, cmd: str, **env: str) -> list[str]:
    sh = _sh()
    if not sh:
        pytest.skip("нет sh")
    head = "".join(f"{k}='{v}'\n" for k, v in env.items())
    f = tmp_path / "step.sh"
    f.write_bytes((head + STUB + cmd + "\n").encode())
    out = subprocess.run([sh, str(f)], capture_output=True, timeout=30, cwd=tmp_path).stdout.decode()
    return out.strip().split("\n")


def test_restart_says_whether_the_container_is_on_the_new_image(monkeypatch, tmp_path):
    cmd = _grafana(monkeypatch)["c_up"].replace("cd /app/stack\n", "")
    assert _run(tmp_path, cmd, IMG="grafana/grafana:12.4")[-1] == "контейнер на новом образе"
    # compose не поменяли - up -d оставил прежний, просто Up
    assert _run(tmp_path, cmd, IMG="grafana/grafana:12.3.1")[-1] == (
        "НЕ ОБНОВЛЕН: контейнер на прежнем образе, смотрите вывод выше")


def test_image_change_says_not_replaced(monkeypatch, tmp_path):
    """Шаг замены целиком в sh: строка с комментарием меняется, вариант из переменной - нет."""
    if not _sh() or not shutil.which("sed"):
        pytest.skip("нет sh или sed")
    cmd = _grafana(monkeypatch)["img_set"].replace("cd /app/stack\n", "")
    y = tmp_path / "docker-compose.yml"
    y.write_bytes(b"services:\n  grafana:\n    #image: grafana/grafana:10.2.3\n"
                  b"    image: grafana/grafana:12.3.1 # https://hub.docker.com/r/grafana/grafana/tags\n")
    assert _run(tmp_path, cmd)[-1] == "образ заменен"
    assert b"    image: grafana/grafana:12.4 # https" in y.read_bytes() and b"#image: grafana/grafana:10.2.3" in y.read_bytes()
    y.write_bytes(b"services:\n  grafana:\n    image: ${IMG:-grafana/grafana:12.3.1}\n")
    assert _run(tmp_path, cmd)[-1] == "НЕ ЗАМЕНЕНО: строка image другая, поправьте ее руками"


def test_host_access_edit_and_restart_check(tmp_path):
    if not _sh() or not shutil.which("sed"):
        pytest.skip("нет sh или sed")
    rep = _rep(_c("web-a", "shop/web:1", priv=True, binds=["/var/run/docker.sock", "/srv/web"],
                  compose=_cm("web", d="/app/stack")))
    [b] = _blocks(rep)
    y = tmp_path / "docker-compose.yml"
    sock_rm = _cmd(b, "sock_rm").replace("cd /app/stack\n", "")
    priv = next(s["cmd"] for s in b["steps"] if s["id"].startswith("priv_")).replace("cd /app/stack\n", "")
    y.write_bytes(b"services:\n  web:\n    image: shop/web:1\n    privileged: true\n    volumes:\n"
                  b"      - /var/run/docker.sock:/var/run/docker.sock\n      - /srv/web:/srv/web\n")
    assert _run(tmp_path, priv)[-1] == "файл изменен"
    assert _run(tmp_path, sock_rm)[-1] == "файл изменен"
    assert b"privileged: false" in y.read_bytes() and b"docker.sock" not in y.read_bytes()
    # второй раз менять нечего: громко, а не молча
    assert _run(tmp_path, priv)[-1] == "НЕ ИЗМЕНЕНО: нужной строки в блоке сервиса нет, поправьте руками"
    up = _cmd(b, "up").replace("cd /app/stack\n", "")
    assert _run(tmp_path, up, PRIV="false", MOUNTS="/srv/web")[-2:] == [
        "web-a: privileged снят", "web-a: docker-сокета у контейнера нет"]
    assert _run(tmp_path, up, PRIV="true", MOUNTS="/var/run/docker.sock /srv/web")[-2:] == [
        "web-a: НЕ СНЯТ: контейнер все еще privileged", "web-a: НЕ УБРАН: docker-сокет все еще у контейнера"]


def test_ere_takes_the_image_literally():
    assert compose_cmd.ere("registry.example.com:5000/a/b:1.2+x") == r"registry\.example\.com:5000/a/b:1\.2\+x"
    assert compose_cmd.verdict("true", "a", "b") == 'true && echo "a" || echo "b"'
