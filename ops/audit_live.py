#!/usr/bin/env python3
"""Живой аудит доступа: проверяет РАБОТАЮЩУЮ панель, а не исходники.

ops/selfcheck.py читает код и ловит расхождения в реестрах. Здесь наоборот —
поднимаются временные учётки трёх ролей и проверяется, что API действительно
отвечает так, как задумано. Именно так были найдены две дыры: админские ручки,
защищённые только пунктом меню, и чужой монитор, открывавшийся по прямому id.

Запуск ВНУТРИ контейнера бэкенда (нужен доступ к БД и к 127.0.0.1:8000):

    docker exec -i kervax-backend-1 sh -c 'cd /srv && python - ' < ops/audit_live.py

Временные учётки (__audit_*) создаются и удаляются самим скриптом.
Код возврата 1 = найдены проблемы.
"""

import asyncio
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/srv")

from sqlalchemy import select  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import create_engine_and_factory  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Check, Location, Server, User  # noqa: E402
from app.security import hash_password  # noqa: E402

BASE = "http://127.0.0.1:8000"
PW = "Audit-Tmp-7Kd93x"
ROLES = [("__audit_admin__", "admin"), ("__audit_editor__", "editor"),
         ("__audit_viewer__", "viewer")]
SCOPED = "__audit_scoped__"  # учётка с нарезанными группами — для проверки на IDOR

# Ручки про саму панель (секреты, учётки, журнал, бэкап конфигурации) — только админ.
ADMIN_ONLY = ("/api/alerts", "/api/settings", "/api/users", "/api/vault",
              "/api/audit", "/api/backup")
SKIP = ("/api/agent",)  # у агента свой токен, эта проверка не про него

PROBLEMS: list[str] = []


def call(path: str, token: str, method: str = "GET") -> int:
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 — сеть/сокет: считаем «не ответил»
        return 0


def login(name: str) -> str:
    req = urllib.request.Request(BASE + "/api/auth/login", method="POST")
    req.add_header("Content-Type", "application/json")
    body = json.dumps({"username": name, "password": PW}).encode()
    with urllib.request.urlopen(req, body, timeout=20) as r:
        return json.loads(r.read())["access_token"]


async def _make_users(sf, scoped_site: str, scoped_srv: str) -> None:
    async with sf() as ses:
        for name, role in ROLES:
            old = await ses.scalar(select(User).where(User.username == name))
            if old:
                await ses.delete(old)
            ses.add(User(username=name, password_hash=hash_password(PW), role=role,
                         sections=[], server_groups=[], site_groups=[]))
        old = await ses.scalar(select(User).where(User.username == SCOPED))
        if old:
            await ses.delete(old)
        ses.add(User(username=SCOPED, password_hash=hash_password(PW), role="editor",
                     sections=[], server_groups=[scoped_srv], site_groups=[scoped_site]))
        await ses.commit()


async def _drop_users(sf) -> None:
    async with sf() as ses:
        for name in [n for n, _ in ROLES] + [SCOPED]:
            u = await ses.scalar(select(User).where(User.username == name))
            if u:
                await ses.delete(u)
        await ses.commit()


def sweep_roles(app, tokens: dict, ids: dict) -> None:
    """Каждый GET-маршрут под тремя ролями: админское закрыто, 5xx нет."""
    print("\n--- доступ по ролям ---")
    print(f"{'маршрут':<44}{'admin':>7}{'editor':>8}{'viewer':>8}")
    checked = 0
    for path, ops in sorted(app.openapi().get("paths", {}).items()):
        if "get" not in ops or not path.startswith("/api") or path.startswith(SKIP):
            continue
        real = path
        for k, v in ids.items():
            real = real.replace("{" + k + "}", str(v))
        if "{" in real:
            continue
        codes = {role: call(real, tokens[role]) for _, role in ROLES}
        checked += 1
        mark = ""
        if path.startswith(ADMIN_ONLY) and (codes["editor"] != 403 or codes["viewer"] != 403):
            mark = "  <-- должно быть только админу"
            PROBLEMS.append(f"{path}: editor={codes['editor']} viewer={codes['viewer']}")
        elif max(codes.values()) >= 500:
            mark = "  <-- 5xx"
            PROBLEMS.append(f"{path}: {codes}")
        print(f"{path:<44}{codes['admin']:>7}{codes['editor']:>8}{codes['viewer']:>8}{mark}")
    print(f"проверено маршрутов: {checked}")


def sweep_idor(app, token: str, mine: dict, alien: dict) -> None:
    """Подресурсы чужого объекта должны отвечать 403/404, а не отдавать данные."""
    print("\n--- чужие объекты по прямому id ---")
    print(f"{'маршрут':<44}{'свой':>7}{'чужой':>8}")
    for path, ops in sorted(app.openapi().get("paths", {}).items()):
        if "get" not in ops or not any(k in path for k in ("{check_id}", "{server_id}")):
            continue
        key = "check_id" if "{check_id}" in path else "server_id"
        mine_url = path.replace("{" + key + "}", str(mine[key]))
        alien_url = path.replace("{" + key + "}", str(alien[key]))
        if "{" in mine_url:
            continue
        a, b = call(mine_url, token), call(alien_url, token)
        bad = b not in (403, 404)
        if bad:
            PROBLEMS.append(f"{path}: чужой объект отдан с кодом {b}")
        print(f"{path:<44}{a:>7}{b:>8}{'  <-- чужое видно' if bad else ''}")


async def main() -> int:
    eng, sf = create_engine_and_factory(get_settings().db_url)
    async with sf() as ses:
        cgroups = sorted({g for g in await ses.scalars(select(Check.group_name).distinct()) if g})
        sgroups = sorted({g for g in await ses.scalars(select(Server.group_name).distinct()) if g})
        if len(cgroups) < 2 or len(sgroups) < 2:
            print("нужно минимум по две группы сайтов и серверов — проверка на IDOR пропущена")
            cgroups = cgroups or ["", ""]
            sgroups = sgroups or ["", ""]
        my_check = await ses.scalar(select(Check).where(Check.group_name == cgroups[0]))
        alien_check = await ses.scalar(select(Check).where(Check.group_name == cgroups[-1]))
        my_srv = await ses.scalar(select(Server).where(Server.group_name == sgroups[0]))
        alien_srv = await ses.scalar(select(Server).where(Server.group_name == sgroups[-1]))
        ids = {
            "check_id": my_check.id if my_check else 1,
            "server_id": my_srv.id if my_srv else 1,
            "location_id": (await ses.scalar(select(Location.id).limit(1))) or 1,
            "user_id": 1,
            "name": "kervax-backup-20260101-000000.json",
            "repo": "x",
            "arch": "amd64",
        }

    await _make_users(sf, cgroups[0], sgroups[0])
    tokens = {role: login(name) for name, role in ROLES}
    app = create_app()
    sweep_roles(app, tokens, ids)

    if my_check and alien_check and my_srv and alien_srv and cgroups[0] != cgroups[-1]:
        sweep_idor(
            app, login(SCOPED),
            {"check_id": my_check.id, "server_id": my_srv.id},
            {"check_id": alien_check.id, "server_id": alien_srv.id},
        )

    await _drop_users(sf)
    await eng.dispose()

    print("\n" + "=" * 60)
    if PROBLEMS:
        print(f"ПРОБЛЕМ: {len(PROBLEMS)}")
        for p in PROBLEMS:
            print("  *", p)
        return 1
    print("проблем не найдено")
    return 0


sys.exit(asyncio.run(main()))
