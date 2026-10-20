"""Откуда запущен контейнер и как назвать его в командах compose: папка, файлы, сервис.

Метки compose (com.docker.compose.project.working_dir, config_files, project, service) агент 2.41
присылает в поле compose каждого контейнера; по ним шаги "как исправить" пишутся готовыми путями,
без поиска на самой ноде. Пустой объект - контейнер запущен не из compose (docker run), поля нет
вовсе - агент старый.
"""
from __future__ import annotations

import re
import shlex

# файлы, которые compose находит в папке сам: с ними -f в командах не нужен
DEFAULT_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
LABEL = "{{index .Config.Labels \"com.docker.compose.%s\"}}"


def project_name(d: str) -> str:
    """Имя проекта, которое compose берет по папке сам (маленькие буквы, цифры, - и _)."""
    return re.sub(r"[^a-z0-9_-]", "", d.rstrip("/").rsplit("/", 1)[-1].lower()).lstrip("_-")


def where(c: dict) -> dict | None:
    """{"dir", "files", "service", "project", "flags"}: files - имена относительно папки, flags - -p и
    -f, только если без них compose в этой папке проект не найдет. {} - не из compose, None - агент
    меток не присылает."""
    cp = c.get("compose")
    if not isinstance(cp, dict):
        return None
    d, svc, proj = (str(cp.get(k) or "").strip() for k in ("dir", "service", "project"))
    files = [f.strip() for f in str(cp.get("files") or "").split(",") if f.strip()]
    if not (d.startswith("/") and svc and files):
        return {}
    d = d.rstrip("/") or "/"
    names = [f[len(d) + 1:] if f.startswith(d + "/") else f for f in files]
    first = names[0]
    plain = first in DEFAULT_FILES and (len(names) == 1 or (
        len(names) == 2 and names[1] == first.rsplit(".", 1)[0] + ".override." + first.rsplit(".", 1)[1]))
    flags = ([f"-p {shlex.quote(proj)}"] if proj and proj != project_name(d) else []) + (
        [] if plain else [f"-f {shlex.quote(n)}" for n in names])
    return {"dir": d, "files": names, "service": svc, "project": proj, "flags": " ".join(flags)}


class Cmd:
    """Строки команд для одного compose-проекта: cd, docker compose с нужными -p/-f, копия файлов."""

    def __init__(self, w: dict):
        q = shlex.quote
        self.cd = f"cd {q(w['dir'])}"
        self.dc = " ".join(x for x in ("sudo docker compose", w["flags"]) if x)
        self.files = [q(n) for n in w["files"]]

    def backup(self, suffix: str) -> list[str]:
        return [f"sudo cp {f} {f}{suffix}" for f in self.files]

    def restore(self, suffix: str) -> list[str]:
        return [f"sudo cp {f}{suffix} {f}" for f in self.files]

    def diff(self, suffix: str) -> list[str]:
        return [f"diff {f}{suffix} {f}" for f in self.files]

    def changed(self, edit: list[str], bad: str) -> list[str]:
        """Правка файлов с проверкой: изменилось ли хоть что-то. sed, не нашедший строку, молчит, и
        дальше перезапускали прежний файл: здесь последняя строка громко скажет, что правки нет."""
        cat = f"sudo cat {' '.join(self.files)} | md5sum"
        return [f"H=$({cat})"] + edit + [verdict(f'[ "$({cat})" != "$H" ]', "файл изменен", bad)]

    def sed_in(self, svc: str, until: str, expr: str) -> str:
        """sed только в блоке сервиса: от строки "  svc:" до первой строки с until после нее.
        Так правка не заденет такую же строку у соседнего сервиса."""
        start = "^ *" + re.sub(r"[.]", r"\\.", svc) + ":"
        return f"sudo sed -i {shlex.quote(f'/{start}/,/{until}/ {expr}')} {' '.join(self.files)}"


def ere(s: str) -> str:
    """Строка буквально в регулярке grep -E / sed -E."""
    return re.sub(r"([.\[\]()*+?{}|^$\\])", r"\\\1", s)


def verdict(test: str, ok: str, bad: str) -> str:
    """Последняя строка команды: проверить результат и сказать словами, вышло или нет. test - команда,
    которой важен только код выхода. Фразы без переменных: английская панель переводит их прямо в
    тексте команды (frontend/src/FixSteps.tsx, ECHO), новую фразу дописать и туда."""
    return f'{test} && echo "{ok}" || echo "{bad}"'


def image_is(name: str, image: str) -> str:
    """Контейнер name сейчас на образе image (как он записан в compose)."""
    return f"sudo docker inspect -f '{{{{.Config.Image}}}}' {shlex.quote(name)} | grep -qxF -- {shlex.quote(image)}"


def find_cmd(names: list[str]) -> str:
    """Где compose контейнеров: одна строка на контейнер - имя, папка, файлы, сервис."""
    fmt = " ".join(["{{.Name}}"] + [LABEL % k for k in ("project.working_dir", "project.config_files", "service")])
    return f"sudo docker inspect -f {shlex.quote(fmt)} " + " ".join(shlex.quote(n) for n in names)
