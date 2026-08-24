#!/bin/sh
# Выкатка Kervax на прод: доставить исходники, пересобрать, СВЕРИТЬ результат.
#
#   ops/deploy.sh kervax-build            выкатить HEAD на kervax.acdev.pro
#   ops/deploy.sh fi-hz-ms2 v1.1.2        выкатить тег на kervax.msergeev.ru
#   ops/deploy.sh kervax-build --dry-run  показать, что произойдёт, и выйти
#
# Прод собирается из исходников (у него свой ключ подписи агента), а /app/kervax
# НЕ является git-репозиторием: файлы приезжают архивом. Отсюда две вещи, ради
# которых существует этот скрипт.
#
# 1. `git archive | tar x` РАСПАКОВЫВАЕТ поверх, но ничего не удаляет. Файл,
#    убранный из репозитория, остаётся на проде навсегда — и однажды ломает
#    сборку или, хуже, продолжает работать как живой код. Поэтому каждый деплой
#    оставляет манифест доставленного (.kervax-deployed), а следующий удаляет то,
#    что было в прошлом манифесте и пропало в новом. Ничего, кроме собственных
#    прошлых файлов, скрипт не трогает: .env, data/, agent-dist/, agent-signing/
#    в манифест не попадают, потому что их нет в архиве.
#
# 2. «Контейнер поднялся» ≠ «код выкачен». Проверяем не факт запуска, а версию
#    из ЖИВОГО health, номер миграции, сохранность данных и то, что scheduler
#    собран из той же версии, что и backend: алерты шлёт он, и забытый scheduler
#    молча оставляет их на старом коде.
set -eu

HOST=""; REF="HEAD"; DIR=/app/kervax; DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        --dir) DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        -*) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
        *) if [ -z "$HOST" ]; then HOST="$1"; else REF="$1"; fi; shift ;;
    esac
done
[ -n "$HOST" ] || { echo "Использование: ops/deploy.sh <ssh-хост> [ref] [--dry-run]" >&2; exit 2; }

say()  { printf '\n\033[1m→ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }

git rev-parse --git-dir >/dev/null 2>&1 || die "запускать из репозитория Kervax"
git rev-parse --verify --quiet "$REF" >/dev/null || die "нет такой ревизии: $REF"
# Выкатываем ровно то, что закоммичено: git archive не видит рабочее дерево, и
# «поправил, выкатил, не работает» — почти всегда именно это.
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    warn "в рабочем дереве есть незакоммиченные изменения — они НЕ поедут"
fi
# ^{commit}: у аннотированного тега rev-parse иначе отдаёт SHA самого тега
REV=$(git rev-parse --short "$REF^{commit}")
VERSION=$(git show "$REF:backend/app/config.py" | sed -n 's/^\s*version: str = "\([0-9.]*\)".*/\1/p' | head -1)
[ -n "$VERSION" ] || die "не смог прочитать версию из backend/app/config.py в $REF"

say "Выкатка $REF ($REV), версия $VERSION → $HOST:$DIR"

# ── Снимок «до» ──────────────────────────────────────────────────────────────
# Набор compose-файлов берём у самого прода (лейбл работающего контейнера), а не
# из головы: у установок он разный, а лишний/пропущенный -f пересоздаёт
# контейнеры без того, что описано в пропущенном файле.
BEFORE=$(ssh "$HOST" "cd '$DIR' 2>/dev/null || exit 7
    CF=\$(sudo docker inspect kervax-frontend-1 --format '{{index .Config.Labels \"com.docker.compose.project.config_files\"}}' 2>/dev/null)
    DBPW=\$(sudo grep '^KERVAX_DB_PASSWORD=' .env 2>/dev/null | cut -d= -f2-)
    VER=\$(sudo docker exec kervax-backend-1 python -c 'import urllib.request;print(urllib.request.urlopen(\"http://localhost:8000/api/health\").read().decode())' 2>/dev/null)
    MIG=\$(sudo docker exec -e PGPASSWORD=\"\$DBPW\" kervax-db-1 psql -U kervax -d kervax -tAc 'select version_num from alembic_version' 2>/dev/null | head -1)
    CNT=\$(sudo docker exec -e PGPASSWORD=\"\$DBPW\" kervax-db-1 psql -U kervax -d kervax -tAc 'select (select count(*) from servers), (select count(*) from checks), (select count(*) from users)' 2>/dev/null | head -1)
    printf '%s\n%s\n%s\n%s\n' \"\$CF\" \"\$VER\" \"\$MIG\" \"\$CNT\"") || die "не достучался до $HOST:$DIR"

CONFIG_FILES=$(printf '%s' "$BEFORE" | sed -n 1p)
VER_BEFORE=$(printf '%s' "$BEFORE" | sed -n 2p)
MIG_BEFORE=$(printf '%s' "$BEFORE" | sed -n 3p)
CNT_BEFORE=$(printf '%s' "$BEFORE" | sed -n 4p)
[ -n "$CONFIG_FILES" ] || die "не смог определить набор compose-файлов на проде (панель запущена?)"

FILES=$(printf '%s' "$CONFIG_FILES" | tr ',' '\n' | sed 's|.*/||' | sed 's/^/-f /' | tr '\n' ' ')
# scheduler пересобираем ТОЛЬКО если он есть в этом наборе (scale-режим)
case "$CONFIG_FILES" in
    *compose.scale.yml*) SERVICES="backend scheduler frontend" ;;
    *) SERVICES="backend frontend" ;;
esac

echo "  сейчас на проде: $VER_BEFORE"
echo "  миграция:        $MIG_BEFORE"
echo "  данные:          $CNT_BEFORE (серверы|мониторы|пользователи)"
echo "  compose:         $FILES"
echo "  пересобрать:     $SERVICES"

# ── Что доставим и что удалим ────────────────────────────────────────────────
NEW_LIST=$(git ls-tree -r --name-only "$REF")
OLD_LIST=$(ssh "$HOST" "cat '$DIR/.kervax-deployed' 2>/dev/null" || true)
if [ -z "$OLD_LIST" ]; then
    warn "манифеста прошлой выкатки нет — в этот раз ничего не удаляю,"
    warn "со следующего раза чистка заработает"
    STALE=""
else
    # то, что мы привозили раньше и чего в новой ревизии больше нет
    STALE=$(printf '%s\n' "$OLD_LIST" | sort > /tmp/.kv_old.$$
             printf '%s\n' "$NEW_LIST" | sort > /tmp/.kv_new.$$
             comm -23 /tmp/.kv_old.$$ /tmp/.kv_new.$$
             rm -f /tmp/.kv_old.$$ /tmp/.kv_new.$$)
fi

if [ -n "$STALE" ]; then
    say "Файлы, убранные из репозитория (удалю с прода)"
    printf '%s\n' "$STALE" | sed 's/^/  − /'
else
    echo "  лишних файлов нет"
fi

if [ "$DRY" = "1" ]; then
    printf '\n\033[33mПробный прогон: ничего не изменено.\033[0m\n'
    n_new=$(printf '%s\n' "$NEW_LIST" | grep -c . || true)
    n_stale=$(printf '%s\n' "$STALE" | grep -c . || true)
    printf 'Поехало бы: %s файлов, удалилось бы: %s\n' "$n_new" "$n_stale"
    exit 0
fi

# ── Доставка ─────────────────────────────────────────────────────────────────
say "Доставляю исходники"
git archive "$REF" | ssh "$HOST" "sudo tar x -C '$DIR'" || die "распаковка не удалась"

if [ -n "$STALE" ]; then
    say "Убираю лишнее"
    printf '%s\n' "$STALE" | ssh "$HOST" "cd '$DIR' && while IFS= read -r f; do
        [ -n \"\$f\" ] && sudo rm -f -- \"\$f\"
    done
    # каталоги, оставшиеся пустыми после удаления, тоже убираем — но только пустые
    sudo find . -type d -empty -not -path './data/*' -not -path './.git/*' -delete 2>/dev/null || true"
fi

# манифест пишем ПОСЛЕ успешной доставки — иначе прерванный деплой оставит
# список того, чего на проде нет, и следующая чистка промахнётся
printf '%s\n' "$NEW_LIST" | ssh "$HOST" "sudo tee '$DIR/.kervax-deployed' >/dev/null"

# ── Сборка ───────────────────────────────────────────────────────────────────
say "Собираю и поднимаю: $SERVICES"
ssh "$HOST" "cd '$DIR' && sudo docker compose $FILES up -d --build $SERVICES 2>&1 | tail -6" \
    || die "сборка не удалась — прод остался на прежних контейнерах"

# ── Сверка ───────────────────────────────────────────────────────────────────
say "Сверяю, что выкачено именно то"
sleep 12
AFTER=$(ssh "$HOST" "cd '$DIR'
    DBPW=\$(sudo grep '^KERVAX_DB_PASSWORD=' .env | cut -d= -f2-)
    VER=\$(sudo docker exec kervax-backend-1 python -c 'import urllib.request;print(urllib.request.urlopen(\"http://localhost:8000/api/health\").read().decode())' 2>/dev/null)
    MIG=\$(sudo docker exec -e PGPASSWORD=\"\$DBPW\" kervax-db-1 psql -U kervax -d kervax -tAc 'select version_num from alembic_version' 2>/dev/null | head -1)
    CNT=\$(sudo docker exec -e PGPASSWORD=\"\$DBPW\" kervax-db-1 psql -U kervax -d kervax -tAc 'select (select count(*) from servers), (select count(*) from checks), (select count(*) from users)' 2>/dev/null | head -1)
    SCH=\$(sudo docker exec kervax-scheduler-1 sed -n 's/^\s*version: str = \"\([0-9.]*\)\".*/\1/p' /srv/app/config.py 2>/dev/null | head -1)
    ERR=\$(sudo docker logs kervax-backend-1 --since 3m 2>&1 | grep -ciE 'traceback|critical' || true)
    printf '%s\n%s\n%s\n%s\n%s\n' \"\$VER\" \"\$MIG\" \"\$CNT\" \"\$SCH\" \"\$ERR\"")

VER_AFTER=$(printf '%s' "$AFTER" | sed -n 1p)
MIG_AFTER=$(printf '%s' "$AFTER" | sed -n 2p)
CNT_AFTER=$(printf '%s' "$AFTER" | sed -n 3p)
SCH_AFTER=$(printf '%s' "$AFTER" | sed -n 4p)
ERR_AFTER=$(printf '%s' "$AFTER" | sed -n 5p)

BAD=0
echo "  health:    $VER_AFTER"
case "$VER_AFTER" in
    *"\"version\":\"$VERSION\""*) ;;
    *) echo "    ✗ версия не $VERSION — код НЕ выкачен"; BAD=1 ;;
esac
case "$VER_AFTER" in
    *'"db":"ok"'*) ;;
    *) echo "    ✗ база недоступна панели"; BAD=1 ;;
esac
echo "  миграция:  $MIG_BEFORE → $MIG_AFTER"
echo "  данные:    $CNT_BEFORE → $CNT_AFTER"
[ "$CNT_BEFORE" = "$CNT_AFTER" ] || warn "счётчики изменились — проверьте, что это ожидаемо"
if [ "$SERVICES" != "${SERVICES#*scheduler}" ]; then
    echo "  scheduler: $SCH_AFTER"
    [ "$SCH_AFTER" = "$VERSION" ] || { echo "    ✗ scheduler не на $VERSION — алерты на старом коде"; BAD=1; }
fi
echo "  traceback в логах бэкенда за 3 мин: ${ERR_AFTER:-0}"
[ "${ERR_AFTER:-0}" = "0" ] || BAD=1

if [ "$BAD" = "0" ]; then
    printf '\n\033[32m✓ %s: %s выкачена и проверена\033[0m\n' "$HOST" "$VERSION"
else
    die "сверка не сошлась — смотрите вывод выше (docker compose $FILES logs --tail=50)"
fi
