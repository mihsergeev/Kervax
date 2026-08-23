#!/usr/bin/env python3
"""Самопроверка панели: ищет места, где Kervax может ПРОМОЛЧАТЬ.

Каждая проверка здесь появилась из реального дефекта, а не из общих соображений.
Общая беда у них одна: рядом с кодом живёт список, написанный руками, и когда
код уходит вперёд — панель не ругается, а тихо перестаёт что-то показывать.
Молчание мониторинга не видно вообще, поэтому такие расхождения ловим машинно.

Запуск из корня репозитория:  python ops/selfcheck.py
Код возврата 1 = есть расхождения (годится для CI).
"""

from __future__ import annotations

import io
import os
import re
import sys

BS = chr(92)
PROBLEMS: list[str] = []


def fail(area: str, msg: str) -> None:
    PROBLEMS.append(f"[{area}] {msg}")


def read(path: str) -> str:
    return io.open(path, encoding="utf-8").read()


def block(src: str, head: str) -> str:
    """Тело литерала-словаря/кортежа, объявленного строкой head.

    Скобки считаем вручную: литерал бывает и многострочным, и в одну строку
    (ALL_SECTIONS), а «до строки с закрывающей скобкой» ловит только первый вид."""
    i = src.find(head)
    if i < 0:
        return ""
    depth = 0
    for k in range(i + len(head) - 1, len(src)):
        if src[k] in "({[":
            depth += 1
        elif src[k] in ")}]":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    return ""


# ---------------------------------------------------------------- 1. helper'ы
def check_helpers() -> None:
    """Каждый раздаваемый helper должен иметь человекочитаемую подпись."""
    srv = read("backend/app/api/servers.py")
    labels = set(re.findall(r'^\s*"([a-z-]+)":', block(srv, "_SETUP_LABEL = {"), re.M))
    files = {f[:-3] for f in os.listdir("agent") if f.endswith(".sh") and f != "install.sh"}
    for name in sorted(files - labels):
        fail("helper", f"«{name}» без подписи в _SETUP_LABEL — в UI покажется техимя")
    for name in sorted(labels - files):
        fail("helper", f"подпись для «{name}», а файла agent/{name}.sh нет")
    print(f"helper-скриптов: {len(files)}, с подписями: {len(labels & files)}")


# ---------------------------------------------------------------- 2. алерты
def check_alerts() -> None:
    """Виды алертов, разделы адресации и иконки.

    Отсутствие вида в реестре раньше означало, что алерт не уйдёт НИКОГДА
    (см. _fallback_rule в collector.py — теперь он спасает, но расхождение
    всё равно надо видеть)."""
    col = read("backend/app/collector.py")
    store = read("backend/app/settings_store.py")
    deps = read("backend/app/deps.py")

    kinds = set(re.findall(
        r'^\s+"([a-z_]+)":', block(store, "SERVER_ALERT_KINDS: dict[str, tuple[str, str]] = {"), re.M))
    site_kinds = set(re.findall(
        r'^\s+"([a-z_]+)":', block(store, "SITE_ALERT_KINDS: dict[str, tuple[str, str]] = {"), re.M))

    cond = re.search(r"def _server_conditions.*?\n(?=def )", col, re.S).group(0)
    used = {x for tup in re.findall(r'out\["([a-z_]+)"\]', cond) for x in [tup]}
    used |= set(re.findall(r'srv_fire\(\s*s,\s*"([a-z_]+)"', col))
    used |= set(re.findall(r'rules\.get\("([a-z_]+)"\)', col))
    for k in sorted(used - kinds):
        fail("алерты", f"вид «{k}» шлётся кодом, но его нет в SERVER_ALERT_KINDS "
                       "(правила и текст не настраиваются)")

    rule_map = dict(re.findall(r'"([a-z_]+)":\s*"([a-z_]+)"',
                               block(col, "_SITE_RULE_KIND = {")))
    for pend, rk in rule_map.items():
        if rk not in site_kinds:
            fail("алерты", f"сайтовый вид «{rk}» (из «{pend}») отсутствует в SITE_ALERT_KINDS")

    sections = set(re.findall(r'"([a-z]+)"', block(deps, "ALL_SECTIONS = (")))
    for k, v in re.findall(r'"([a-z_]+)":\s*"([a-z]+)"', block(col, "_ALERT_SECTION = {")):
        if v not in sections:
            fail("алерты", f"_ALERT_SECTION[{k}] ведёт в несуществующий раздел «{v}»")
    print(f"видов серверных алертов: {len(kinds)}, сайтовых: {len(site_kinds)}")


# ---------------------------------------------------------------- 3. доступ
def check_rbac() -> None:
    """Админские ручки должны требовать админа НА БЭКЕНДЕ.

    Прятать пункт меню недостаточно: с токеном в руках сотрудник ходит в API
    напрямую. Так /api/alerts отдавал telegram_token любому, кто вошёл."""
    # backup — это бэкап ПАНЕЛИ (экспорт app_settings с токеном бота + restore,
    # который переписывает конфигурацию), а не бэкапы нод
    admin_only = {"alerts", "settings", "users", "vault", "audit", "backup"}
    for name in sorted(admin_only):
        src = read(f"backend/app/api/{name}.py")
        if "CurrentUser" in src:
            fail("доступ", f"api/{name}.py принимает CurrentUser — ручка должна быть "
                           "админской (проверка только в UI не считается)")

    deps = read("backend/app/deps.py")
    back = set(re.findall(r'"([a-z]+)"', block(deps, "ALL_SECTIONS = (")))
    app_tsx = read("frontend/src/App.tsx")
    front = set(re.findall(r"'([a-z]+)'", re.search(r"export type Section = (.*)", app_tsx).group(1)))
    front.discard("home")
    if back != front:
        fail("доступ", f"разделы разошлись: бэкенд {sorted(back)} vs фронт {sorted(front)}")
    print(f"разделов: {len(back)}, админских модулей API: {len(admin_only)}")


# ---------------------------------------------------------------- 3b. секреты
def check_secrets() -> None:
    """Поля-секреты в ответах API должны быть осознанно закрыты.

    GET /api/checks отдавал auth_pass (пароль от закрытого раздела сайта) и
    http_headers («Authorization: Bearer …») всем подряд, а GET /api/locations —
    прокси с user:pass в URL. Каждое такое поле обязано либо гаситься по роли,
    либо быть перечислено здесь как сознательно открытое."""
    masked = {  # поле → файл, где оно гасится или выдаётся под гейтом
        "auth_pass": "backend/app/api/checks.py",     # _hide_secrets по роли
        "http_headers": "backend/app/api/checks.py",  # там же
        "telegram_token": "backend/app/api/alerts.py",  # весь роутер — AdminUser
        "repopass": "backend/app/api/servers.py",     # ключ бэкапа — AdminUser
    }
    # осознанно открытые: own_token — флаг «свой бот», а не сам токен; secret —
    # TOTP при настройке 2FA СВОЕЙ учётки; token — разовый токен агента, который
    # тот же человек только что и завёл
    public = {"auth_method", "auth_user", "own_token", "secret", "token"}
    schemas = read("backend/app/schemas.py")
    secret_re = re.compile(r"pass|token|secret|creds")
    suspicious: set[str] = set()
    for m in re.finditer(r"class (\w*Out)\(BaseModel\):(.*?)(?=\nclass |\Z)", schemas, re.S):
        for field in re.findall(r"^\s{4}([a-z_]+):", m.group(2), re.M):
            if field not in public and secret_re.search(field):
                suspicious.add(field)
    for field in sorted(suspicious):
        where = masked.get(field)
        if where is None:
            fail("секреты", f"поле «{field}» уезжает в API-ответе без гейта по роли — "
                            "добавьте маскировку или внесите в список осознанных")
        elif field not in read(where):
            fail("секреты", f"маскировка поля «{field}» пропала из {where}")
    print(f"полей-секретов в Out-схемах: {len(suspicious)}, все с маскировкой")


# ---------------------------------------------------------------- 4. схема
def check_migrations() -> None:
    """Таблица/колонка есть в модели, но её не создаёт ни одна миграция → 500 на проде."""
    models = read("backend/app/models.py")
    mig_dir = "backend/alembic/versions"
    migs = "\n".join(
        read(os.path.join(mig_dir, f)) for f in os.listdir(mig_dir) if f.endswith(".py")
    )
    tables = re.findall(r'__tablename__ = "([a-z_]+)"', models)
    for t in tables:
        if f'"{t}"' not in migs:
            fail("миграции", f"таблица «{t}» есть в моделях, но не создаётся миграцией")
    for cls in ("User", "Server", "Check"):
        m = re.search(rf"class {cls}\(Base\):(.*?)\n\nclass ", models, re.S)
        if not m:
            continue
        for col in re.findall(r"^\s{4}([a-z_]+):\s*Mapped", m.group(1), re.M):
            if col != "id" and f'"{col}"' not in migs:
                fail("миграции", f"колонка {cls}.{col} есть в модели, но не создаётся миграцией")
    print(f"таблиц в моделях: {len(tables)}, файлов миграций: "
          f"{len([f for f in os.listdir(mig_dir) if f.endswith('.py')])}")


# ---------------------------------------------------------------- 5. переводы
def check_i18n() -> None:
    """Строка без английского перевода молча покажется по-русски в EN-локали."""
    d = "frontend/src"
    key_re = re.compile(r"^\s+'((?:[^'" + BS + BS + "]|" + BS + BS + r".)+)'\s*:", re.M)
    use_re = re.compile(r"\bt\(\s*'((?:[^'" + BS + BS + "]|" + BS + BS + r".)+)'")
    keys = set(key_re.findall(read(f"{d}/i18n.tsx")))
    used: dict[str, str] = {}
    for f in sorted(os.listdir(d)):
        if f.endswith((".tsx", ".ts")) and f != "i18n.tsx":
            for s in use_re.findall(read(f"{d}/{f}")):
                used.setdefault(s, f)
    cyr = lambda s: any("а" <= c.lower() <= "я" or c == "ё" for c in s)  # noqa: E731
    missing = sorted((s, f) for s, f in used.items() if cyr(s) and s not in keys)
    for s, f in missing:
        fail("i18n", f"{f}: нет перевода — «{s[:60]}»")
    print(f"строк через t(): {len(used)}, переведено: {len(used) - len(missing)}")


def check_compose_overlays() -> None:
    """Оверлей не должен ВВОДИТЬ сервис, которого нет в базовом compose.

    Объявление сервиса в оверлее его создаёт, а не дополняет. compose.ghcr.yml
    упоминал scheduler «на случай scale» — и в обычном режиме поднимался пустой
    контейнер без переменных и томов, падавший на первой же попытке открыть базу.
    Установка при этом выглядела успешной, а рядом висел Exited (1).
    """
    def services(path: str) -> set[str]:
        out, inside = set(), False
        for line in read(path).split("\n"):
            if re.match(r"^services:\s*$", line):
                inside = True
                continue
            if inside:
                # комментарий и пустая строка блок НЕ закрывают: в YAML они
                # прозрачны, и дописанный после комментария сервис Compose
                # прекрасно видит — а наивный парсер его пропускал
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if re.match(r"^\S", line):          # вышли из блока services
                    inside = False
                elif (m := re.match(r"^  ([a-z0-9_-]+):\s*$", line)):
                    out.add(m.group(1))
        return out

    base = services("compose.yml")
    # Оверлей → сервисы, которые ему разрешено называть. compose.scale.yml
    # заводит scheduler целиком, а ghcr-scale только подменяет ему образ и
    # применяется исключительно вместе со scale — там сервис уже существует.
    allowed = {"compose.scale.yml": {"scheduler"},
               "compose.ghcr-scale.yml": {"scheduler"}}
    for f in sorted(f for f in os.listdir(".") if re.match(r"^compose\..+\.yml$", f)):
        new = services(f) - base - allowed.get(f, set())
        for name in sorted(new):
            fail("compose", f"{f} вводит сервис «{name}», которого нет в compose.yml — "
                            "он поднимется пустым, без окружения и томов")
    print(f"сервисов в compose.yml: {len(base)}, оверлеев проверено: "
          f"{len([f for f in os.listdir('.') if re.match(r'^compose[.].+[.]yml$', f)])}")


def check_version() -> None:
    """Номер версии объявлен в трёх файлах — они должны совпадать.

    Панель показывает версию из /api/health (config.py), образы тегируются по
    pyproject, npm — по package.json. Разъедутся — и «какая версия у вас стоит»
    станет вопросом без ответа. CHANGELOG обязан знать текущую версию."""
    got = {
        "backend/pyproject.toml": re.search(r'^version = "([\d.]+)"', read("backend/pyproject.toml"), re.M),
        "backend/app/config.py": re.search(r'^\s+version: str = "([\d.]+)"', read("backend/app/config.py"), re.M),
        "frontend/package.json": re.search(r'"version":\s*"([\d.]+)"', read("frontend/package.json")),
    }
    vers = {f: (m.group(1) if m else None) for f, m in got.items()}
    if None in vers.values():
        fail("версия", f"не нашёл номер версии в {[f for f, v in vers.items() if v is None]}")
        return
    if len(set(vers.values())) > 1:
        fail("версия", f"номера разошлись: {vers}")
        return
    v = next(iter(vers.values()))
    if f"## [{v}]" not in read("CHANGELOG.md"):
        fail("версия", f"версия {v} не описана в CHANGELOG.md")
    print(f"версия: {v}, во всех трёх файлах и в CHANGELOG")


def check_password_len() -> None:
    """Минимальная длина пароля записана в двух местах — они должны совпадать.

    Разъедутся — форма либо отправит то, что API отвергнет (422 «на ровном
    месте»), либо не даст ввести пароль, который бэкенд принял бы."""
    back = re.search(r"^MIN_PASSWORD_LEN\s*=\s*(\d+)", read("backend/app/security.py"), re.M)
    front = re.search(r"^export const MIN_PASSWORD_LEN\s*=\s*(\d+)", read("frontend/src/api.ts"), re.M)
    if not back or not front:
        fail("password", "не нашёл MIN_PASSWORD_LEN в backend/app/security.py или frontend/src/api.ts")
        return
    if back.group(1) != front.group(1):
        fail("password", f"минимум пароля разошёлся: бэкенд {back.group(1)}, фронт {front.group(1)}")
    # число, вписанное мимо константы, — та же беда в профиль
    hard = re.findall(r"(?:password|пароль)[^\n]{0,40}length\s*[<>]=?\s*(\d+)",
                      read("frontend/src/PasswordModal.tsx") + read("frontend/src/UsersModal.tsx"),
                      re.I)
    for n in hard:
        fail("password", f"длина пароля вписана числом ({n}) вместо MIN_PASSWORD_LEN")
    print(f"минимум пароля: {back.group(1)} символов, бэкенд и фронт согласны")


def check_i18n_literals() -> None:
    """Русский текст, который вообще не проходит через переводчик.

    check_i18n смотрит только на t('…'). Мимо неё прошли шаблонные литералы и
    таблицы единиц: английская панель показывала «41 ГБ», «2ч 30м» и выдавала
    bash-скрипты с русскими комментариями. Признак пробела — строки нет в
    словаре, а рядом в строке кода нет вызова переводчика.
    """
    d = "frontend/src"
    lit = re.compile(r"['\"`]([^'\"`" + BS + BS + r"\n]*[А-Яа-яЁё][^'\"`" + BS + BS + r"\n]*)['\"`]")
    key_re = re.compile(r"^\s+'((?:[^'" + BS + BS + "]|" + BS + BS + r".)+)'\s*:", re.M)
    keys = set(key_re.findall(read(f"{d}/i18n.tsx")))
    bad: list[tuple[str, int, str]] = []
    for f in sorted(os.listdir(d)):
        if not f.endswith((".ts", ".tsx")) or f == "i18n.tsx":
            continue
        for n, line in enumerate(read(f"{d}/{f}").split("\n"), 1):
            code = line.split("//")[0]
            if line.strip().startswith(("//", "*", "/*")):
                continue
            # в строке уже зовут переводчик — значит текст локализован, а куски
            # с ${…} регулярка просто рвёт на части
            if re.search(r"\b(?:t|tr)\(", code) or re.search(r"\ben\b\s*[:?]|'en'", code):
                continue
            for s in lit.findall(code):
                if len(s.strip()) > 2 and s not in keys:
                    bad.append((f, n, s))
    for f, n, s in bad:
        fail("i18n", f"{f}:{n}: текст мимо переводчика — «{s[:56]}»")
    print(f"кириллических литералов вне словаря: {len(bad)}")


def main() -> int:
    if not os.path.isdir("backend/app"):
        print("запускать из корня репозитория", file=sys.stderr)
        return 2
    for fn in (check_helpers, check_alerts, check_rbac, check_secrets,
               check_migrations, check_i18n, check_i18n_literals,
               check_password_len, check_version, check_compose_overlays):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — упавшая проверка тоже проблема
            fail(fn.__name__, f"проверка не отработала: {exc}")
    print("-" * 58)
    if PROBLEMS:
        print(f"РАСХОЖДЕНИЙ: {len(PROBLEMS)}")
        for p in PROBLEMS:
            print("  *", p)
        return 1
    print("расхождений нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
