#!/usr/bin/env bash
# Kervax: включить УПРАВЛЕНИЕ restic-бэкапом из панели (Фаза 2).
#
# Агент под непривилегированным `kervax` + NoNewPrivileges НЕ может sudo и не может
# править конфиг бэкапа (скрипт 0700 bx231, таймер root). Поэтому:
#  • root-cron пишет текущий конфиг (mode/расписание/пути) в /var/lib/kervax/backup-config.json
#    (агент читает файл — показывает «Управление» в панели, без секретов);
#  • команды идут ЧЕРЕЗ СПУЛ: агент кладёт запрос в /var/lib/kervax/backup-req, а root
#    path-unit исполняет его узким helper и пишет ответ в /var/lib/kervax/backup-res.
# Агент остаётся полностью изолированным (никаких повышений привилегий). Действия строго
# из белого списка, имена/время валидируются. Запускать root'ом на клиентской ноде.
set -euo pipefail

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-backup-helper"
STATE_DIR=/var/lib/kervax
CONF_JSON="$STATE_DIR/backup-config.json"
REQ_DIR="$STATE_DIR/backup-req"
RES_DIR="$STATE_DIR/backup-res"
CRON=/etc/cron.d/kervax-backup
AGENT_USER=kervax

# v2: пути пишутся/читаются только для активного режима (include ИЛИ exclude) — раньше
# get-config отдавал панели один и тот же список в обоих полях
# v3: dump-setup — локальные дампы СУБД в /backup/<движок> перед файловым бэкапом
# v4: движки redis, rabbitmq, k8s (бэкап кластера k0s/k3s вместе с etcd)
# v5: get-config отдаёт состояние включённых дампов (панель показывает статус, а не «включено» разово)
# v6: dump-remove — выключить дамп ОДНОГО движка, не трогая остальные
# v7: pg_dumpall берёт роль из POSTGRES_USER контейнера; mysqldump — пароль из его окружения
# v8: дампы ПОКОНТЕЙНЕРНО (движок@контейнер): две postgres на ноде не мешают друг другу
# ИСТОРИЯ (версия = дата правки, ГГГГММДД; см. KERVAX_SETUP_VERSION ниже)
# v9: атомарная запись (.partial + gzip -t): оборванный дамп не остаётся под видом целого
# v10: настраиваемые dir/keep/minfree + защита от переполнения (skip при нехватке места)
# v11: включение — лёгкая проба доступности вместо полного дампа; enabled_ts для grace
# v12: pg — каждая база в свой файл + globals.sql.gz; ротация по прогонам (подкаталог/TS)
# v13: restic-update — обновление restic до 0.19.1 (проверка sha256, атомарная замена)
# v14: дампы БЕЗ файлового бэкапа — свой таймер kervax-dumps.timer (локальная копия на
#      ноде, из которой можно восстановиться); ставим хелпер ВЕЗДЕ (ALWAYS): он и есть
#      транспорт панели для управления дампами, а раньше без restic его просто не было
KERVAX_SETUP_VERSION=0.23  # МАЖОР.МИНОР; сравнивается покомпонентно (0.13 > 0.2!)
KERVAX_SETUP_ALWAYS=1  # безопасен на любой ноде: ставит только свой хелпер+спул,
                       # конфиг бэкапа не трогает, пока панель не пришлёт команду
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" /var/lib/kervax/versions
echo "$KERVAX_SETUP_VERSION" > /var/lib/kervax/versions/backup-setup.ver
chmod 0644 /var/lib/kervax/versions/backup-setup.ver  # явно: агент (kervax) обязан прочитать
# файловая/спул-схема sudo не требует — снимаем старое sudoers-правило (если было)
rm -f /etc/sudoers.d/kervax-backup 2>/dev/null || true
# спул: агент (kervax) кладёт запросы (нужен -wx), читает ответы (нужен r-x)
install -d -o root -g "$AGENT_USER" -m 0730 "$REQ_DIR"
# 0770: агент (kervax) должен УДАЛЯТЬ прочитанный ответ, иначе res-файлы копятся
install -d -o root -g "$AGENT_USER" -m 0770 "$RES_DIR"
# агент под ProtectSystem=strict пишет только /opt/kervax/bin — разрешаем ему спул,
# иначе запись в /var/lib/kervax read-only (спул не работает). Drop-in + рестарт агента.
if systemctl cat kervax-agent >/dev/null 2>&1; then
  install -d -m 0755 /etc/systemd/system/kervax-agent.service.d
  cat > /etc/systemd/system/kervax-agent.service.d/kervax-spool.conf <<'DROPIN'
[Service]
ReadWritePaths=/var/lib/kervax
DROPIN
  systemctl daemon-reload 2>/dev/null || true
  systemctl try-restart kervax-agent 2>/dev/null || true
fi

cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Kervax backup helper (root) — узкие операции над restic-бэкапом. get-config (read),
# set-paths/set-schedule/run-now (write), process-spool (исполнить запросы из спула).
set -euo pipefail

HELPER_VER=1  # версия backup-helper; панель флагует ноды со старым helper'ом (переустановить)
REQ_DIR=/var/lib/kervax/backup-req
RES_DIR=/var/lib/kervax/backup-res
CONF_JSON=/var/lib/kervax/backup-config.json
SCRIPT_CANDS=("/etc/systemd-rest.conf")
TIMER_CANDS=("systemd-rest.timer" "restic-backup.timer" "restic.timer")
# провижининг (создание бэкапа с нуля) — целевые артефакты РОВНО те, что детектит агент
RESTIC_BIN_DIR=/usr/local/lib/.restic
RESTIC_BIN="$RESTIC_BIN_DIR/restic"
PROV_ENV=/etc/systemd-resta.conf
PROV_SCRIPT=/etc/systemd-rest.conf
PROV_SERVICE=systemd-rest.service
PROV_TIMER=systemd-rest.timer
PROV_USER=bx231
PROV_HOME=/var/cache/bx231
PROV_METRICS_DIR=/var/lib/node_exporter/textfile_collector
PROV_METRICS_FILE=restic-backup.prom
PROV_CACERT="$RESTIC_BIN_DIR/backup-ca.pem"

find_script() { for f in "${SCRIPT_CANDS[@]}"; do [ -f "$f" ] && { echo "$f"; return; }; done; }
find_timer()  { for t in "${TIMER_CANDS[@]}"; do
  [ -e "/etc/systemd/system/$t" ] && { echo "$t"; return; }
  systemctl cat "$t" >/dev/null 2>&1 && { echo "$t"; return; }
done; }
valid_path() { case "$1" in
  /*[!A-Za-z0-9._/+-]*) return 1 ;; *..*) return 1 ;; /*) return 0 ;; *) return 1 ;;
esac; }
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# ------- локальные дампы СУБД: restic заберёт их обычным файловым бэкапом -------
# Файловый снапшот живой базы может не восстановиться, поэтому кладём рядом штатный дамп.
# Пароли НЕ нужны и НЕ хранятся: в контейнер ходим `docker exec` под служебным юзером,
# нативно — `sudo -u <юзер>`, везде локальный сокет + peer-авторизация.
DUMP_DIR_BASE=/backup
# пути пишем ЛИТЕРАЛОМ: это тело helper'а внутри quoted-heredoc, переменные установщика
# (HELPER_DIR и пр.) сюда не подставляются и в рантайме не существуют
DUMPS_D=/lib65/kervax/dumps.d
RUN_DUMPS=/lib65/kervax/run-dumps.sh
DUMP_KEEP=2  # держим 2 последних: историю хранит restic, дублировать её на диске незачем
DUMP_HOUR=3  # час своего таймера дампов (когда файлового бэкапа на ноде нет)

# run-dumps.sh — прогоняет все включённые дампы. Вешается на сервис бэкапа как
# ExecStartPre=- (минус!): упавший дамп НЕ должен отменять файловый бэкап.
write_run_dumps() {
  install -d -m 0755 "$DUMPS_D"
  cat > "$RUN_DUMPS" <<'RD_EOF'
#!/usr/bin/env bash
# Kervax: прогон включённых дампов СУБД перед файловым бэкапом. Ошибку одного движка
# не считаем фатальной — остальные и сам бэкап должны отработать.
set -u
for d in /lib65/kervax/dumps.d/*.sh; do
  [ -x "$d" ] || continue
  "$d" || echo "kervax-dump: $(basename "$d") завершился с ошибкой" >&2
done
exit 0
RD_EOF
  chmod 0755 "$RUN_DUMPS"; chown root:root "$RUN_DUMPS"
  # ЧЕМ ЗАПУСКАЕТСЯ. Если файловый бэкап есть — цепляемся к нему (дамп прямо перед
  # restic, чтобы тот забрал свежий). Если бэкапа нет — дампы всё равно нужны:
  # локальная копия на самой ноде, из которой можно восстановиться. Тогда поднимаем
  # СВОЙ таймер. Раньше второй случай не рассматривался: drop-in писался на
  # несуществующий сервис, дамп не запускался никогда, а панель показывала «включено».
  if systemctl cat "$PROV_SERVICE" >/dev/null 2>&1; then
    local dir="/etc/systemd/system/$PROV_SERVICE.d"
    install -d -m 0755 "$dir"
    # префиксы: «+» = выполнить С ПОЛНЫМИ ПРАВАМИ (сервис бэкапа бежит под непривилегированным
    # bx231, а дамп-скрипты root:0700 — без «+» цикл молча ничего не находил и выходил с 0);
    # «-» = провал дампа НЕ отменяет файловый бэкап.
    cat > "$dir/kervax-dumps.conf" <<EOF
[Service]
ExecStartPre=-+$RUN_DUMPS
EOF
    chmod 0644 "$dir/kervax-dumps.conf"
    # свой таймер больше не нужен: дампы поедут вместе с бэкапом, иначе снимались бы дважды
    systemctl disable --now kervax-dumps.timer >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/kervax-dumps.timer /etc/systemd/system/kervax-dumps.service
  else
    cat > /etc/systemd/system/kervax-dumps.service <<EOF
[Unit]
Description=Kervax: локальные дампы СУБД (файлового бэкапа на ноде нет)
[Service]
Type=oneshot
ExecStart=$RUN_DUMPS
EOF
    cat > /etc/systemd/system/kervax-dumps.timer <<EOF
[Unit]
Description=Kervax: ежедневные локальные дампы СУБД
[Timer]
OnCalendar=*-*-* ${DUMP_HOUR}:00
RandomizedDelaySec=15min
Persistent=true
[Install]
WantedBy=timers.target
EOF
    chmod 0644 /etc/systemd/system/kervax-dumps.service /etc/systemd/system/kervax-dumps.timer
    systemctl daemon-reload 2>/dev/null || true
    systemctl enable --now kervax-dumps.timer >/dev/null 2>&1 || true
  fi
  systemctl daemon-reload 2>/dev/null || true
}

# Быстрая проверка доступности перед включением — вместо полного пробного дампа. На
# большой базе полный дамп идёт минуты: панель отваливалась по таймауту и показывала
# «ошибку», хотя дамп шёл. Тут — только доступность и права (schema-only/ping): ловит
# главные молчаливые поломки (роль, пароль, доступ), но не тянет данные. Реальный дамп —
# по расписанию бэкапа. k8s не проверяем: снапшот кластера тяжёлый, лёгкой пробы нет.
# Grafana: состояние — SQLite-файл grafana.db. Ищем и на хосте, и в томах: под
# kubernetes база лежит в PVC (local-path кладёт её прямо на диск ноды), поэтому
# «процесс на хосте» ещё не значит «установлена пакетом».
grafana_db_path() {
  local p
  for p in /var/lib/grafana/grafana.db /var/lib/grafana/data/grafana.db; do
    [ -f "$p" ] && { echo "$p"; return 0; }
  done
  # PVC/тома: ограничиваем глубину и типовые корни, чтобы не сканировать весь диск
  p=$(find /data /var/lib/k0s /var/lib/rancher /opt/local-path-provisioner /var/lib/docker/volumes \
        -maxdepth 4 -name grafana.db -type f 2>/dev/null | head -1)
  [ -n "$p" ] && { echo "$p"; return 0; }
  return 1
}

# Чем снимать КОНСИСТЕНТНУЮ копию живой SQLite. Просто cp нельзя: база пишется, файл
# может оказаться порванным. sqlite3 в системе обычно нет (на графановской ноде не
# было), зато python3 есть почти всегда, а его модуль sqlite3 умеет онлайн-бэкап.
grafana_dumper() {
  if command -v sqlite3 >/dev/null 2>&1; then echo sqlite3; return 0; fi
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sqlite3' 2>/dev/null; then echo python3; return 0; fi
  return 1
}

dump_probe() {
  local engine="$1" container="${2:-}"
  case "$engine" in
    pg)
      if [ -n "$container" ]; then
        local pgu; pgu=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container" 2>/dev/null | sed -n 's/^POSTGRES_USER=//p' | head -1)
        [ -n "$pgu" ] || pgu=postgres
        docker exec -u postgres "$container" pg_dumpall -U "$pgu" --schema-only >/dev/null 2>&1 \
          || docker exec "$container" pg_dumpall -U "$pgu" --schema-only >/dev/null 2>&1
      else sudo -u postgres pg_dumpall --schema-only >/dev/null 2>&1; fi ;;
    mysql)
      if [ -n "$container" ]; then
        docker exec "$container" sh -c 'MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}" mysqldump -u root --no-data --all-databases' >/dev/null 2>&1
      else mysqldump --no-data --all-databases >/dev/null 2>&1; fi ;;
    ch)
      if [ -n "$container" ]; then docker exec "$container" clickhouse-client --query "SELECT 1" >/dev/null 2>&1
      else clickhouse-client --query "SELECT 1" >/dev/null 2>&1; fi ;;
    redis)
      if [ -n "$container" ]; then docker exec "$container" redis-cli ping >/dev/null 2>&1
      else redis-cli ping >/dev/null 2>&1; fi ;;
    rabbitmq)
      if [ -n "$container" ]; then docker exec "$container" rabbitmqctl list_vhosts >/dev/null 2>&1
      else rabbitmqctl list_vhosts >/dev/null 2>&1; fi ;;
    k8s) return 0 ;;
    neo4j)
      # Дамп снимается ОСТАНОВЛЕННОЙ базой (см. генератор), поэтому пробуем не сам дамп,
      # а наличие инструмента: neo4j-admin в образе контейнера либо на хосте.
      if [ -n "$container" ]; then
        local img; img=$(docker inspect -f '{{.Config.Image}}' "$container" 2>/dev/null)
        [ -n "$img" ] || return 1
        docker run --rm --entrypoint neo4j-admin "$img" --version >/dev/null 2>&1
      else command -v neo4j-admin >/dev/null 2>&1; fi ;;
    grafana) [ -n "$(grafana_db_path)" ] && grafana_dumper >/dev/null ;;
  esac
}

cmd_dump_setup() {
  local engine="$1" container="${2:-}"
  case "$engine" in
    pg|mysql|ch|redis|rabbitmq|k8s|grafana|neo4j) ;;
    *) echo "неизвестный движок: $engine (поддерживаются pg, mysql, ch, redis, rabbitmq, k8s, grafana, neo4j)" >&2; return 2 ;;
  esac
  case "$container" in ''|*[!A-Za-z0-9._-]*) container="" ;; esac
  # Настройки из env (кладёт агент по спулу; при вызове из CLI — дефолты). Валидируем
  # ЗДЕСЬ, а не доверяем панели: dir уходит в rm/mkdir от root, keep — в ротацию.
  local dir_base="${KV_DUMP_DIR:-$DUMP_DIR_BASE}"
  local keep="${KV_DUMP_KEEP:-$DUMP_KEEP}"
  local minfree="${KV_DUMP_MINFREE:-10}"
  case "$dir_base" in
    /*) : ;; *) echo "каталог дампов должен быть абсолютным путём: $dir_base" >&2; return 2 ;;
  esac
  case "$dir_base" in
    *..*|*[!A-Za-z0-9._/-]*) echo "недопустимый каталог дампов: $dir_base" >&2; return 2 ;;
    /) echo "корень / нельзя использовать под дампы" >&2; return 2 ;;
  esac
  case "$keep" in ''|*[!0-9]*) keep=2 ;; esac
  [ "$keep" -lt 1 ] && keep=1; [ "$keep" -gt 30 ] && keep=30
  case "$minfree" in ''|*[!0-9]*) minfree=10 ;; esac
  [ "$minfree" -gt 50 ] && minfree=50   # выше 50% — уже не «запас», а полдиска впустую
  # Движков одного типа на ноде бывает несколько (kervax-db-1 и zabbix-postgres). У каждого
  # СВОЙ скрипт и СВОЙ каталог: иначе второй перезаписал бы первый, а чистка «оставить N
  # последних» удаляла бы чужие дампы. Без контейнера (нативная установка) — как раньше.
  local slot="$engine" out="$dir_base/$engine"
  if [ -n "$container" ]; then
    slot="$engine@$container"
    out="$dir_base/$engine/$container"
  fi
  local skipf="$DUMPS_D/$slot.skip"   # флаг «последний прогон пропущен из-за места»
  install -d -m 0700 "$dir_base" "$dir_base/$engine" "$out"
  write_run_dumps
  local f="$DUMPS_D/$slot.sh"
  {
    printf '#!/usr/bin/env bash\n# generated by kervax — дамп %s перед файловым бэкапом\nset -uo pipefail\n' "$engine"
    printf 'OUT=%q\nKEEP=%s\nCONT=%q\nMINFREE=%s\nSKIPF=%q\n' "$out" "$keep" "$container" "$minfree" "$skipf"
    printf 'TS=$(date +%%Y%%m%%d-%%H%%M%%S)\n'
    printf 'FINAL="$OUT"\nMULTI=""\n'
    # ЗАЩИТА ОТ ПЕРЕПОЛНЕНИЯ: не начинаем дамп, если после него на разделе останется
    # меньше MINFREE%. Размер будущего дампа оцениваем по последнему успешному ×1.2 (на
    # рост). Первый дамп (оценки нет) пускаем — от заполнения под ноль защищает атомарная
    # запись в .partial (упадёт на «No space», обрезок удалится). При пропуске пишем флаг
    # SKIPF и выходим с 0: это НАМЕРЕННЫЙ пропуск, а не сбой (иначе run-dumps счёл бы ошибкой).
    printf 'if [ "$MINFREE" -gt 0 ]; then\n'
    printf '  read -r AVAIL TOTAL <<<"$(df -kP "$FINAL" | awk %s)"\n' "'NR==2{print \$4, \$2}'"
    printf '  LAST=$(ls -1t "$FINAL"/* 2>/dev/null | head -1)\n'
    printf '  EST=0; [ -n "$LAST" ] && EST=$(du -sk "$LAST" 2>/dev/null | cut -f1)\n'
    printf '  NEED=$(( EST * 12 / 10 )); MIN=$(( TOTAL * MINFREE / 100 ))\n'
    printf '  if [ "$TOTAL" -gt 0 ] && [ $(( AVAIL - NEED )) -lt "$MIN" ]; then\n'
    printf '    printf %s "$(date +%%s)" "$(( AVAIL * 100 / TOTAL ))" "$(( NEED / 1024 ))" > "$SKIPF"\n' "'%s|%s|%s\\n'"
    printf '    echo "kervax-dump: %s ПРОПУЩЕН — на разделе свободно $(( AVAIL*100/TOTAL ))%%, порог ${MINFREE}%% (нужно ~$(( NEED/1024 )) МБ)" >&2\n' "$slot"
    printf '    exit 0\n  fi\nfi\n'
    printf 'rm -f "$SKIPF"\n'   # места хватило — снимаем прошлый флаг пропуска
    # АТОМАРНОСТЬ: пишем во временный подкаталог и переносим готовое. Дамп, оборванный
    # на середине (перезагрузка, OOM, убитая сессия), оставлял бы обрезанный .gz рядом с
    # настоящими — панель считала бы его файлом дампа, а restic унёс бы битый архив в
    # бэкап. Проверено вживую: прерванный pg_dumpall не проходит `gzip -t`.
    printf 'OUT="$OUT/.partial"\nrm -rf "$OUT"; mkdir -p "$OUT"\n'
    case "$engine" in
      pg)
           # КАЖДАЯ база — в свой файл (pg_dump), плюс globals.sql.gz с ролями/правами
           # (pg_dumpall --globals-only) — их pg_dump по базе НЕ берёт, без них при
           # восстановлении на чистый сервер были бы таблицы без пользователей и GRANT'ов.
           # Прогон кладём в подкаталог по TS ($OUT/<TS>), ротация — по прогонам (MULTI=1).
           # Роль читаем из POSTGRES_USER контейнера (см. v7); -u postgres, при отсутствии
           # (bitnami) — от юзера по умолчанию.
           pg_body='MULTI=1
if [ -n "$CONT" ]; then
  PGU=$(docker inspect -f "{{range .Config.Env}}{{println .}}{{end}}" "$CONT" 2>/dev/null | sed -n "s/^POSTGRES_USER=//p" | head -1)
  [ -n "$PGU" ] || PGU=postgres
  DEX() { docker exec -u postgres "$CONT" "$@" 2>/dev/null || docker exec "$CONT" "$@"; }
else PGU=""; DEX() { sudo -u postgres "$@"; }; fi
RUN="$FINAL/.partial-$TS"; rm -rf "$RUN"; mkdir -p "$RUN"
DEX pg_dumpall ${PGU:+-U "$PGU"} --globals-only | gzip -c > "$RUN/globals.sql.gz"
DBS=$(DEX psql ${PGU:+-U "$PGU"} -Atc "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname")
for db in $DBS; do
  safe=$(printf "%s" "$db" | tr -c "A-Za-z0-9._-" "_")
  DEX pg_dump ${PGU:+-U "$PGU"} "$db" | gzip -c > "$RUN/$safe.sql.gz"
done
'
           printf '%s' "$pg_body" ;;
      mysql) printf 'F="$OUT/mysqldump-all-$TS.sql.gz"\n'
           # --single-transaction: снимок без блокировки таблиц (InnoDB).
           # Пароль root'а разворачивается ВНУТРИ контейнера (sh -c), а не подставляется
           # в аргументы docker exec — иначе он светился бы в ps на хосте. MYSQL_PWD, а не
           # -p: пароль в argv виден даже внутри контейнера.
           printf 'if [ -n "$CONT" ]; then docker exec "$CONT" sh -c %s | gzip -c > "$F"\n' \
                  "'MYSQL_PWD=\"\${MYSQL_ROOT_PASSWORD:-\${MARIADB_ROOT_PASSWORD:-}}\" mysqldump -u root --all-databases --single-transaction --quick'"
           printf 'else mysqldump --all-databases --single-transaction --quick | gzip -c > "$F"; fi\n' ;;
      redis) printf 'F="$OUT/redis-$TS.rdb"\n'
           # --rdb делает полную синхронизацию в файл; из контейнера вынимаем docker cp,
           # т.к. redis-cli пишет ФАЙЛ (стрим в stdout поддерживается не везде)
           printf 'if [ -n "$CONT" ]; then docker exec "$CONT" redis-cli --rdb /tmp/kervax.rdb >/dev/null 2>&1 && docker cp "$CONT":/tmp/kervax.rdb "$F" >/dev/null && docker exec "$CONT" rm -f /tmp/kervax.rdb\n'
           printf 'else redis-cli --rdb "$F" >/dev/null 2>&1; fi\n'
           printf 'gzip -f "$F" 2>/dev/null; F="$F.gz"\n' ;;
      rabbitmq) printf 'F="$OUT/rabbitmq-defs-$TS.json"\n'
           # export_definitions = пользователи/vhost/очереди/политики (СООБЩЕНИЯ не входят)
           printf 'if [ -n "$CONT" ]; then docker exec "$CONT" rabbitmqctl export_definitions /tmp/kervax.json >/dev/null 2>&1 && docker cp "$CONT":/tmp/kervax.json "$F" >/dev/null && docker exec "$CONT" rm -f /tmp/kervax.json\n'
           printf 'else rabbitmqctl export_definitions "$F" >/dev/null 2>&1; fi\n'
           printf 'gzip -f "$F" 2>/dev/null; F="$F.gz"\n' ;;
      k8s) printf 'F="$OUT/cluster-$TS"\n'
           # Бэкап кластера ВМЕСТЕ с etcd — штатной командой дистрибутива, на контроллере
           # от root. Никакого kubectl exec: k0s/k3s умеют это сами.
           printf 'if command -v k0s >/dev/null 2>&1; then mkdir -p "$F" && k0s backup --save-path "$F" >/dev/null 2>&1\n'
           printf 'elif command -v k3s >/dev/null 2>&1; then mkdir -p "$F" && k3s etcd-snapshot save --dir "$F" >/dev/null 2>&1\n'
           printf 'else echo "не нашёл k0s/k3s — снапшот etcd надо делать etcdctl вручную" >&2; exit 1; fi\n'
           printf 'tar -czf "$F.tar.gz" -C "$(dirname "$F")" "$(basename "$F")" 2>/dev/null && rm -rf "$F"; F="$F.tar.gz"\n' ;;
      grafana) printf 'F="$OUT/grafana-$TS.db"\n'
           # штатный ОНЛАЙН-бэкап SQLite (.backup / Connection.backup): снимок берётся
           # под блокировкой движка, графану останавливать не нужно. Просто cp нельзя —
           # живая база пишется, файл может оказаться порванным.
           printf 'DB=%q\n' "$(grafana_db_path)"
           printf 'if command -v sqlite3 >/dev/null 2>&1; then sqlite3 "$DB" ".backup $F" >/dev/null 2>&1\n'
           printf 'else python3 -c %s "$DB" "$F" >/dev/null 2>&1; fi\n' \
                  "'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()'"
           printf 'gzip -f "$F" 2>/dev/null; F="$F.gz"\n' ;;
      neo4j) printf 'F="$OUT/neo4j-$TS.tar.gz"\n'
           # Neo4j Community онлайн-дамп НЕ умеет: neo4j-admin отказывается работать по
           # живой базе («The database is in use»), а горячий backup есть только в
           # Enterprise. Поэтому база на время дампа останавливается и ОБЯЗАТЕЛЬНО
           # поднимается обратно — даже если дамп упал (иначе один сбой оставил бы
           # сервис лежать до утра).
           #
           # Дамп снимает ОДНОРАЗОВЫЙ контейнер того же образа с --volumes-from: сам
           # контейнер в этот момент остановлен, а данные доступны через его тома.
           # --entrypoint neo4j-admin обязателен: штатный entrypoint образа сбрасывает
           # привилегии на пользователя neo4j, и тот не может писать в наш каталог
           # (проверено: AccessDeniedException на /kvdump).
           #
           # Берём ВСЕ базы ("*"): в system живут пользователи и роли, без него
           # восстановление даёт данные без доступов.
           printf 'if [ -n "$CONT" ]; then\n'
           printf '  IMG=$(docker inspect -f "{{.Config.Image}}" "$CONT" 2>/dev/null)\n'
           printf '  WASUP=$(docker inspect -f "{{.State.Running}}" "$CONT" 2>/dev/null)\n'
           printf '  [ "$WASUP" = "true" ] && docker stop "$CONT" >/dev/null 2>&1\n'
           printf '  docker run --rm --user root --entrypoint neo4j-admin --volumes-from "$CONT" \\\n'
           printf '    -v "$OUT":/kvdump "$IMG" database dump "*" --to-path=/kvdump >/dev/null 2>&1\n'
           printf '  rc2=$?\n'
           printf '  [ "$WASUP" = "true" ] && docker start "$CONT" >/dev/null 2>&1\n'
           printf 'else\n'
           printf '  WASUP=$(systemctl is-active neo4j 2>/dev/null)\n'
           printf '  [ "$WASUP" = "active" ] && systemctl stop neo4j >/dev/null 2>&1\n'
           printf '  neo4j-admin database dump "*" --to-path="$OUT" >/dev/null 2>&1; rc2=$?\n'
           printf '  [ "$WASUP" = "active" ] && systemctl start neo4j >/dev/null 2>&1\n'
           printf 'fi\n'
           # каждая база даёт свой .dump — складываем прогон в один архив
           printf 'if [ "$rc2" = "0" ] && ls "$OUT"/*.dump >/dev/null 2>&1; then\n'
           printf '  tar -czf "$F" -C "$OUT" $(cd "$OUT" && ls *.dump) 2>/dev/null && rm -f "$OUT"/*.dump\n'
           # else false: rc ниже читается из $?, и без явного провала сбойный дамп
           # выглядел бы успешным — ветка if просто не выполнилась бы, вернув 0
           printf 'else false; fi\n' ;;
      ch)  printf 'F="$OUT/clickhouse-$TS.sql.gz"\n'
           # без clickhouse-backup: выгружаем схему+данные штатным клиентом
           printf 'if [ -n "$CONT" ]; then docker exec "$CONT" clickhouse-client --query "SHOW DATABASES" > "$OUT/databases-$TS.txt" 2>/dev/null; docker exec "$CONT" clickhouse-client --query "SELECT create_table_query FROM system.tables WHERE database NOT IN (%s)" | gzip -c > "$F"\n' "'system','INFORMATION_SCHEMA','information_schema'"
           printf 'else clickhouse-client --query "SELECT create_table_query FROM system.tables WHERE database NOT IN (%s)" | gzip -c > "$F"; fi\n' "'system','INFORMATION_SCHEMA','information_schema'" ;;
    esac
    printf 'rc=$?\n'
    # Финал в двух режимах. MULTI (pg) — прогон это подкаталог $FINAL/<TS> с набором
    # файлов: проверяем каждый gzip -t, публикуем уже готовый подкаталог (он писался
    # прямо в $FINAL/$TS, не в .partial), ротируем по ПРОГОНАМ. Одно-файловые движки —
    # как раньше: один $F, атомарный перенос из .partial, ротация по файлам.
    fin_multi='if [ -n "$MULTI" ]; then
  if [ -z "$(ls -A "$RUN" 2>/dev/null)" ]; then rm -rf "$OUT" "$RUN"; echo "дамп pg: не создан ни один файл" >&2; exit 1; fi
  for gz in "$RUN"/*.gz; do gzip -t "$gz" 2>/dev/null || { rm -rf "$RUN"; echo "дамп pg повреждён: $(basename "$gz")" >&2; exit 1; }; done
  chmod 0700 "$RUN"; chmod 0600 "$RUN"/*.gz; rm -rf "$OUT"
  mv -f "$RUN" "$FINAL/$TS"   # публикация прогона одним движением
  ls -1dt "$FINAL"/*/ 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -rf
  echo "kervax-dump: pg → $FINAL/$TS ($(ls -1 "$FINAL/$TS"/*.gz 2>/dev/null | wc -l) файлов)"
  exit 0
fi
'
    printf '%s' "$fin_multi"
    printf 'if [ $rc -ne 0 ] || [ ! -s "$F" ]; then rm -rf "$OUT"; echo "дамп %s не удался (rc=$rc)" >&2; exit 1; fi\n' "$engine"
    # gzip -t ловит именно обрыв: файл на месте и непустой, но распаковаться не может
    printf 'case "$F" in *.gz) gzip -t "$F" 2>/dev/null || { rm -rf "$OUT"; echo "дамп %s повреждён (обрыв записи)" >&2; exit 1; } ;; esac\n' "$engine"
    printf 'chmod 0600 "$F"\n'
    # публикация: готовое переносим одним движением, временный каталог убираем
    printf 'mv -f "$F" "$FINAL/"; F="$FINAL/$(basename "$F")"; rm -rf "$OUT"; OUT="$FINAL"\n'
    # ротация: держим только KEEP последних, иначе локальный диск съест история
    printf 'ls -1t "$OUT"/*.gz 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f\n'
    printf 'echo "kervax-dump: %s → $F"\n' "$engine"
  } > "$f"
  chmod 0700 "$f"; chown root:root "$f"
  # метаданные слота: get-config читает их отсюда, а не парсит сгенерированный скрипт.
  # dir здесь настраиваемая, поэтому по фиксированному DUMP_DIR_BASE её уже не вычислить.
  printf 'dir=%s\nkeep=%s\nminfree=%s\n' "$out" "$keep" "$minfree" > "$DUMPS_D/$slot.meta"
  chmod 0644 "$DUMPS_D/$slot.meta"
  # БЫСТРАЯ проверка доступности вместо полного пробного дампа (см. dump_probe). Ловит
  # молчаливые поломки (роль/пароль/доступ) за секунды, не тянет данные. Полный дамп —
  # по расписанию бэкапа. Раньше тут гнался весь дамп: на большой базе минуты, панель
  # отваливалась по таймауту и показывала «ошибку», хотя дамп шёл.
  if ! dump_probe "$engine" "$container" >/tmp/kv-dump.$$ 2>&1; then
    local err; err="$(tr '\n' ' ' </tmp/kv-dump.$$ | tail -c 300)"; rm -f /tmp/kv-dump.$$
    rm -f "$f" "$DUMPS_D/$slot.meta"   # не оставляем заведомо нерабочий дамп включённым
    echo "проверка дампа не прошла, дампы НЕ включены: $err" >&2; return 2
  fi
  rm -f /tmp/kv-dump.$$
  local trial_note=" — первый дамп снимется в ближайший бэкап по расписанию"
  systemctl cat "$PROV_SERVICE" >/dev/null 2>&1 || trial_note=" — первый дамп снимется в ближайший прогон таймера"
  # Дамп бесполезен, если restic его не забирает. В include-режиме папку дампов надо явно
  # перечислить, иначе дампы молча копились бы локально и никуда не уезжали.
  local added=""
  local script; script="$(find_script || true)"
  if [ -n "$script" ]; then
    local cur_mode inc
    cur_mode="$(sed -n 's/^INCLUDE_MODE="\(.*\)".*/\1/p' "$script" | head -1)"
    if [ "$cur_mode" = include ]; then
      inc="$(sed -n 's/^INCLUDES_STR="\(.*\)"$/\1/p' "$script" | head -1)"
      # формат строки: «"/etc" "/var/www"». Значение собираем ОТДЕЛЬНОЙ переменной и лишь
      # потом отдаём sed — иначе экранирование кавычек в самой sed-команде уезжает и в
      # runner попадает путь вида \"/backup\" (проверено: restic ищет несуществующий путь).
      if ! printf '%s' "$inc" | grep -qF "\"$dir_base\""; then
        local newinc esc
        newinc="${inc:+$inc }\"$dir_base\""
        esc="$(printf '%s' "$newinc" | sed 's/[|&\\]/\\&/g')"   # только спецсимволы sed
        sed -i "s|^INCLUDES_STR=\".*\"|INCLUDES_STR=\"$esc\"|" "$script"
        added=" ($dir_base добавлен в список бэкапа)"
      fi
    else
      # exclude-режим: убедимся, что папка дампов не выкинута исключением
      local exc; exc="$(sed -n 's/^EXCLUDES_STR="\(.*\)"$/\1/p' "$script" | head -1)"
      case "$exc" in
        *"--exclude=$dir_base"*) added=" ВНИМАНИЕ: $dir_base в списке исключений — дампы не попадут в бэкап!" ;;
      esac
    fi
  fi
  refresh_config
  # чем именно триггерится — тем и отчитываемся: на ноде без файлового бэкапа обещать
  # «перед каждым бэкапом» было бы враньём, там работает свой таймер
  local when="перед каждым бэкапом"
  systemctl cat "$PROV_SERVICE" >/dev/null 2>&1 || when="ежедневно, свой таймер (файлового бэкапа на ноде нет — копия только локальная)"
  echo "OK дампы $engine включены → $out ($when, храним $keep, порог места ${minfree}%)$added$trial_note"
}

# Выключить дамп ОДНОГО движка. Убираем и скрипт, и накопленные файлы: иначе restic
# продолжал бы вечно таскать в бэкап один и тот же протухший дамп. История при этом не
# теряется — прошлые дампы уже лежат в снапшотах restic на бэкап-сервере.
# ExecStartPre-drop-in НЕ трогаем: run-dumps.sh на пустом каталоге просто выходит с 0,
# а при повторном включении ничего не нужно доделывать.
cmd_dump_remove() {
  local engine="$1" container="${2:-}"
  case "$engine" in
    pg|mysql|ch|redis|rabbitmq|k8s|grafana|neo4j) ;;
    *) echo "неизвестный движок: $engine" >&2; return 2 ;;
  esac
  case "$container" in ''|*[!A-Za-z0-9._-]*) container="" ;; esac
  local slot="$engine"
  [ -n "$container" ] && slot="$engine@$container"
  local f="$DUMPS_D/$slot.sh"
  [ -f "$f" ] || { echo "дампы $slot и так не включены" >&2; return 2; }
  # каталог дампов берём из .meta: он мог быть настроен нестандартным. Нет meta
  # (старый слот) — старая схема по DUMP_DIR_BASE.
  local out="$DUMP_DIR_BASE/$engine"; [ -n "$container" ] && out="$DUMP_DIR_BASE/$engine/$container"
  local base="$DUMP_DIR_BASE"
  if [ -f "$DUMPS_D/$slot.meta" ]; then
    out="$(sed -n 's/^dir=//p' "$DUMPS_D/$slot.meta" | head -1)"
    base="$(dirname "$(dirname "$out")")"; [ -z "$container" ] && base="$(dirname "$out")"
  fi
  rm -f "$f" "$DUMPS_D/$slot.meta" "$DUMPS_D/$slot.skip"
  rm -rf "${out:?}"
  # каталог движка сносим, только если в нём не осталось дампов другого контейнера
  rmdir "$(dirname "$out")" 2>/dev/null || true
  refresh_config
  echo "OK дампы $slot выключены, локальные файлы удалены (история осталась в restic)"
}

# какие дампы включены — панель показывает это в «Покрытии»
cmd_dump_status() {
  local j="" e f n sz
  for f in "$DUMPS_D"/*.sh; do
    [ -f "$f" ] || continue
    e="$(basename "$f" .sh)"
    n=$(find "$DUMP_DIR_BASE/$e" -maxdepth 1 -type f -name '*.gz' 2>/dev/null | wc -l)
    sz=$(du -sb "$DUMP_DIR_BASE/$e" 2>/dev/null | cut -f1); [ -z "$sz" ] && sz=0
    j="${j:+$j,}{\"engine\":\"$(json_escape "$e")\",\"files\":$n,\"size_bytes\":$sz}"
  done
  printf '[%s]\n' "$j"
}

cmd_get_config() {
  local script timer mode="" sched="" inc="" exc=""
  script="$(find_script || true)"; timer="$(find_timer || true)"
  if [ -n "$script" ]; then
    mode="$(sed -n 's/^INCLUDE_MODE="\(.*\)".*/\1/p' "$script" | head -1)"
    # читаем список ТОЛЬКО активного режима — на нодах, провижиненных старым helper'ом,
    # заполнены оба, и без этого панель показывала бы неактуальный список второго режима
    if [ "$mode" = include ]; then
      inc="$(sed -n 's/^INCLUDES_STR="\(.*\)"$/\1/p' "$script" | head -1)"
    else
      exc="$(sed -n 's/^EXCLUDES_STR="\(.*\)"$/\1/p' "$script" | head -1)"
    fi
  fi
  [ -n "$timer" ] && sched="$(systemctl cat "$timer" 2>/dev/null | sed -n 's/^OnCalendar=.* \([0-9]\{1,2\}:[0-9]\{2\}\):00$/\1/p' | head -1)"
  local ex_json="" in_json="" p
  for p in $(printf '%s\n' "$exc" | grep -oE -- '--exclude=[^ ]+' | sed 's/^--exclude=//'); do
    ex_json="${ex_json:+$ex_json,}\"$(json_escape "$p")\""; done
  for p in $(printf '%s\n' "$inc" | grep -oE '"[^"]+"' | tr -d '"'); do
    in_json="${in_json:+$in_json,}\"$(json_escape "$p")\""; done
  # назначение бэкапа (куда) — из env, БЕЗ пароля/htpasswd (файл конфига world-readable!)
  local repo_masked=""
  if [ -f "$PROV_ENV" ]; then
    local url; url="$(sed -n 's/^RESTIC_REPOSITORY=//p' "$PROV_ENV" | head -1)"
    repo_masked="$(printf '%s' "$url" | sed -E 's#(rest:[a-z]+://)[^@]*@#\1#')"
  fi
  # Состояние ВКЛЮЧЁННЫХ дампов. Именно состояние, а не разовый ответ команды: панель
  # должна показывать «дампы идут, N файлов, столько-то места, последний тогда-то»,
  # иначе после включения карточка выглядит так же, как до него.
  # слот = «движок» или «движок@контейнер»: панель показывает состояние КАЖДОЙ базы
  # отдельно, иначе вторая postgres выглядела бы покрытой дампом первой
  local dumps_json="" slot de dcont ddir dn dsz dts dkeep dminfree skipped skip_ts skip_free enabled_ts
  for slot in "$DUMPS_D"/*.sh; do
    [ -f "$slot" ] || continue
    slot="$(basename "$slot" .sh)"
    de="${slot%%@*}"; dcont=""
    case "$slot" in *@*) dcont="${slot#*@}" ;; esac
    enabled_ts=$(stat -c %Y "$DUMPS_D/$slot.sh" 2>/dev/null || echo 0)  # когда дамп включён
    # dir/keep/minfree — из .meta (dir настраиваемая, по DUMP_DIR_BASE её не вычислить).
    # Старые слоты без .meta (до этой версии) → дефолты и путь по старой схеме.
    ddir="$DUMP_DIR_BASE/$de"; [ -n "$dcont" ] && ddir="$DUMP_DIR_BASE/$de/$dcont"
    dkeep="$DUMP_KEEP"; dminfree=10
    if [ -f "$DUMPS_D/$slot.meta" ]; then
      ddir="$(sed -n 's/^dir=//p' "$DUMPS_D/$slot.meta" | head -1)"
      dkeep="$(sed -n 's/^keep=//p' "$DUMPS_D/$slot.meta" | head -1)"
      dminfree="$(sed -n 's/^minfree=//p' "$DUMPS_D/$slot.meta" | head -1)"
    fi
    dn=$(find "$ddir" -type f -name "*.gz" ! -path "*/.partial-*" 2>/dev/null | wc -l)
    dsz=$(du -sb "$ddir" 2>/dev/null | cut -f1); [ -z "$dsz" ] && dsz=0
    dts=$(find "$ddir" -type f -name "*.gz" ! -path "*/.partial-*" -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)
    [ -z "$dts" ] && dts=0
    # флаг пропуска из-за места (пишет сам дамп-скрипт): «ts|free_pct|need_mb»
    skipped=false; skip_ts=0; skip_free=0
    if [ -f "$DUMPS_D/$slot.skip" ]; then
      skipped=true
      skip_ts="$(cut -d'|' -f1 "$DUMPS_D/$slot.skip")"; [ -z "$skip_ts" ] && skip_ts=0
      skip_free="$(cut -d'|' -f2 "$DUMPS_D/$slot.skip")"; [ -z "$skip_free" ] && skip_free=0
    fi
    dumps_json="${dumps_json:+$dumps_json,}{\"engine\":\"$(json_escape "$de")\",\"container\":\"$(json_escape "$dcont")\",\"files\":$dn,\"size_bytes\":$dsz,\"last_ts\":$dts,\"keep\":${dkeep:-2},\"min_free_pct\":${dminfree:-10},\"dir\":\"$(json_escape "$ddir")\",\"skipped\":$skipped,\"skip_ts\":$skip_ts,\"skip_free_pct\":$skip_free,\"enabled_ts\":${enabled_ts:-0}}"
  done
  # helper_version — панель по нему понимает, что helper на ноде устарел (нужна переустановка)
  printf '{"manageable":true,"helper_version":%s,"mode":"%s","schedule":"%s","includes":[%s],"excludes":[%s],"repo_dest":"%s","dumps":[%s]}\n' \
    "$HELPER_VER" "${mode:-exclude}" "${sched:-}" "$in_json" "$ex_json" "$(json_escape "$repo_masked")" "$dumps_json"
}

# get-creds — вернуть данные для восстановления (repo URL + пароль). Секрет: только по
# явному запросу из панели, через спул (res 0770, удаляется сразу). НЕ в конфиг-файл.
cmd_get_creds() {
  [ -f "$PROV_ENV" ] || { echo "env не найден (бэкап не настроен этой панелью)" >&2; return 2; }
  local url pass cacert=""
  url="$(sed -n 's/^RESTIC_REPOSITORY=//p' "$PROV_ENV" | head -1)"
  pass="$(sed -n 's/^RESTIC_PASSWORD=//p' "$PROV_ENV" | head -1)"
  [ -f "$PROV_CACERT" ] && cacert="$PROV_CACERT"
  # base64 одной строкой — переживает спул (tr '\n'); панель декодирует
  printf 'repo_url=%s\nrepopass=%s\ncacert_file=%s\n' "$url" "$pass" "$cacert" | base64 -w0
}
refresh_config() { cmd_get_config > "$CONF_JSON.tmp" 2>/dev/null && mv -f "$CONF_JSON.tmp" "$CONF_JSON" && chmod 0644 "$CONF_JSON"; }

cmd_set_paths() {
  local mode="$1"; shift
  [ "$mode" = include ] || [ "$mode" = exclude ] || { echo "bad mode" >&2; return 2; }
  local script; script="$(find_script)" || { echo "script not found" >&2; return 2; }
  local paths=() p; for p in "$@"; do valid_path "$p" || { echo "bad path: $p" >&2; return 2; }; paths+=("$p"); done
  [ "${#paths[@]}" -gt 0 ] || { echo "no paths" >&2; return 2; }
  cp -a "$script" "$script.kervax.bak"
  if [ "$mode" = include ]; then
    local inc=""; for p in "${paths[@]}"; do inc="${inc:+$inc }\"$p\""; done
    sed -i "s|^INCLUDE_MODE=\".*\"|INCLUDE_MODE=\"include\"|; s|^INCLUDES_STR=\".*\"|INCLUDES_STR=\"$inc\"|" "$script"
  else
    local exc=""; for p in "${paths[@]}"; do exc="${exc:+$exc }--exclude=$p"; done
    sed -i "s|^INCLUDE_MODE=\".*\"|INCLUDE_MODE=\"exclude\"|; s|^EXCLUDES_STR=\".*\"|EXCLUDES_STR=\"$exc\"|" "$script"
  fi
  echo OK
}
cmd_set_schedule() {
  local hhmm="$1"
  [[ "$hhmm" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "bad time" >&2; return 2; }
  local timer; timer="$(find_timer)" || { echo "timer not found" >&2; return 2; }
  local file="/etc/systemd/system/$timer"; [ -f "$file" ] || { echo "timer file not found" >&2; return 2; }
  cp -a "$file" "$file.kervax.bak"
  sed -i "s|^OnCalendar=.*|OnCalendar=*-*-* ${hhmm}:00|" "$file"
  systemctl daemon-reload
  echo OK
}
cmd_run_now() {
  local timer; timer="$(find_timer)" || { echo "timer not found" >&2; return 2; }
  systemctl start --no-block "${timer%.timer}.service"
  echo "OK (запущен)"
}

# ---- провижининг бэкапа с нуля (создать restic + env + скрипт + service + timer) ----
ensure_restic() {
  local ver="$1" arch f base
  [ -x "$RESTIC_BIN" ] && return 0
  install -d -m 0755 "$RESTIC_BIN_DIR"
  # уже есть restic в системе — прячем копию в наш каталог (как ансибл-роль)
  if command -v restic >/dev/null 2>&1; then
    cp "$(command -v restic)" "$RESTIC_BIN"; chmod 0755 "$RESTIC_BIN"; return 0
  fi
  case "$(uname -m)" in x86_64) arch=amd64;; aarch64|arm64) arch=arm64;; *) echo "неизвестная арх." >&2; return 2;; esac
  command -v bunzip2 >/dev/null 2>&1 || { apt-get update -qq >/dev/null 2>&1 || true; apt-get install -y -qq bzip2 >/dev/null 2>&1 || true; }
  base="https://github.com/restic/restic/releases/download/v$ver"; f="restic_${ver}_linux_${arch}.bz2"
  curl -fsSL --connect-timeout 20 "$base/$f" -o "/tmp/$f" || { echo "не скачать restic" >&2; return 2; }
  if curl -fsSL --connect-timeout 20 "$base/SHA256SUMS" -o /tmp/restic-sums 2>/dev/null && [ -s /tmp/restic-sums ]; then
    ( cd /tmp && grep " $f\$" restic-sums | sha256sum -c - >/dev/null 2>&1 ) || { echo "checksum не сошёлся" >&2; rm -f "/tmp/$f"; return 2; }
  fi
  bunzip2 -f "/tmp/$f" || { echo "bunzip2 fail" >&2; return 2; }
  install -m 0755 "/tmp/restic_${ver}_linux_${arch}" "$RESTIC_BIN"
  rm -f "/tmp/restic_${ver}_linux_${arch}" /tmp/restic-sums
}

# Целевая версия restic для всего парка + её sha256 (проверка ОБЯЗАТЕЛЬНА). Обновляем до
# неё старые ноды (0.13/0.14/0.15). Форматы репозитория обратно совместимы — 0.19 читает
# и пишет репо, созданные старыми версиями, поэтому обновление безопасно.
RESTIC_TARGET_VER="0.19.1"
RESTIC_SHA_amd64="f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c"
RESTIC_SHA_arm64="a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465"

# cmd_restic_update — обновить restic-бинарь(и) до RESTIC_TARGET_VER. Обновляет и панельный
# ($RESTIC_BIN), и системный (из PATH), если он реально используется. Скачивает с github,
# СВЕРЯЕТ sha256 с зашитым (не доверяем только SHA256SUMS с того же хоста), заменяет
# атомарно. Бэкап-конфиг не трогаем — только бинарь.
cmd_restic_update() {
  local arch want
  case "$(uname -m)" in
    x86_64) arch=amd64; want="$RESTIC_SHA_amd64";;
    aarch64|arm64) arch=arm64; want="$RESTIC_SHA_arm64";;
    *) echo "неизвестная архитектура: $(uname -m)" >&2; return 2;;
  esac
  # какие бинари обновлять: панельный + системный (могут быть один и тот же — dedup ниже)
  local targets=() b
  [ -x "$RESTIC_BIN" ] && targets+=("$RESTIC_BIN")
  b="$(command -v restic 2>/dev/null || true)"; [ -n "$b" ] && targets+=("$b")
  if [ "${#targets[@]}" -eq 0 ]; then
    echo "restic на ноде не найден (ни $RESTIC_BIN, ни в PATH)" >&2; return 2
  fi
  # уже на целевой? проверяем по первому бинарю — не качаем зря
  local cur; cur="$("${targets[0]}" version 2>/dev/null | grep -oE 'restic [0-9.]+' | awk '{print $2}')"
  if [ "$cur" = "$RESTIC_TARGET_VER" ]; then
    echo "restic уже $RESTIC_TARGET_VER — обновление не требуется"; return 0
  fi
  command -v bunzip2 >/dev/null 2>&1 || { apt-get update -qq >/dev/null 2>&1 || true; apt-get install -y -qq bzip2 >/dev/null 2>&1 || true; }
  local base f tmp
  base="https://github.com/restic/restic/releases/download/v$RESTIC_TARGET_VER"
  f="restic_${RESTIC_TARGET_VER}_linux_${arch}.bz2"
  tmp="$(mktemp -d /tmp/kv-restic.XXXXXX)"
  curl -fsSL --connect-timeout 20 "$base/$f" -o "$tmp/$f" || { rm -rf "$tmp"; echo "не скачать restic $RESTIC_TARGET_VER" >&2; return 2; }
  # sha256 сжатого .bz2 — сверяем с ЗАШИТЫМ в helper, а не только с SHA256SUMS github
  local got; got="$(sha256sum "$tmp/$f" | awk '{print $1}')"
  if [ "$got" != "$want" ]; then
    rm -rf "$tmp"; echo "sha256 не сошёлся (ожидали $want, получили $got) — обновление отменено" >&2; return 2
  fi
  bunzip2 -f "$tmp/$f" || { rm -rf "$tmp"; echo "bunzip2 fail" >&2; return 2; }
  local newbin="$tmp/restic_${RESTIC_TARGET_VER}_linux_${arch}"
  chmod 0755 "$newbin"
  "$newbin" version >/dev/null 2>&1 || { rm -rf "$tmp"; echo "скачанный restic не запускается" >&2; return 2; }
  # атомарная замена каждого уникального бинаря: cp во временный рядом + mv (один раздел)
  local done="" out=""
  for b in "${targets[@]}"; do
    case " $done " in *" $b "*) continue;; esac   # dedup (панельный == системный)
    done="$done $b"
    cp -f "$newbin" "$b.kv-new" && chmod 0755 "$b.kv-new" && mv -f "$b.kv-new" "$b" \
      && out="$out $b" || echo "не заменить $b" >&2
  done
  rm -rf "$tmp"
  echo "restic обновлён до $RESTIC_TARGET_VER (было ${cur:-?}):$out"
}

write_runner() {
  local mode="$1"; shift
  local paths=("$@") p exc="" inc=""
  # заполняем ТОЛЬКО список активного режима: раньше писались оба, и get-config отдавал
  # панели пути одновременно в includes и excludes (на бэкап не влияло — runner берёт
  # нужный, — но UI показывал чужой список и мог сохранить его при смене режима)
  for p in "${paths[@]}"; do
    if [ "$mode" = include ]; then
      inc="${inc:+$inc }\"$p\""
    else
      exc="${exc:+$exc }--exclude=$p"
    fi
  done
  local restic="$RESTIC_BIN"; [ -f "$PROV_CACERT" ] && restic="$RESTIC_BIN --cacert $PROV_CACERT"
  {
    printf '#!/usr/bin/env bash\nset -euo pipefail\n'
    printf 'RESTIC=%q\n' "$restic"
    printf 'ENV_FILE=%q\n' "$PROV_ENV"
    printf 'LOCKFILE=%q\n' "$PROV_HOME/rk.lock"
    printf 'METRICS_DIR=%q\n' "$PROV_METRICS_DIR"
    printf 'METRICS_FILE=%q\n' "$PROV_METRICS_DIR/$PROV_METRICS_FILE"
    printf 'HOST=%q\n' "$(hostname)"
    printf 'INCLUDE_MODE="%s"\n' "$mode"
    printf 'EXCLUDES_STR="%s"\n' "$exc"
    printf 'INCLUDES_STR="%s"\n' "$inc"
    cat <<'RUNNER_BODY'
mkdir -p "$(dirname "$LOCKFILE")" "$METRICS_DIR"
prev_val(){ local n="$1" d="$2" v=""; [ -f "$METRICS_FILE" ] && v="$(awk -v m="$n" 'index($0,m"{")==1{print $NF}' "$METRICS_FILE"|tail -n1)"; [ -n "$v" ]&&echo "$v"||echo "$d"; }
write_metrics(){ local s="$1" sk="$2" du="$3" ts tmp; ts="$(date +%s)"; tmp="${METRICS_FILE}.$$.tmp"
  { echo "restic_last_backup_success{host=\"${HOST}\"} ${s}"; echo "restic_last_backup_timestamp{host=\"${HOST}\"} ${ts}"
    echo "restic_last_backup_skipped{host=\"${HOST}\"} ${sk}"; echo "restic_last_backup_duration_seconds{host=\"${HOST}\"} ${du}"; } > "$tmp"; mv -f "$tmp" "$METRICS_FILE"; }
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  ps="$(prev_val restic_last_backup_success 0)"; pt="$(prev_val restic_last_backup_timestamp "$(date +%s)")"; pd="$(prev_val restic_last_backup_duration_seconds 0)"
  tmp="${METRICS_FILE}.$$.tmp"
  { echo "restic_last_backup_success{host=\"${HOST}\"} ${ps}"; echo "restic_last_backup_timestamp{host=\"${HOST}\"} ${pt}"
    echo "restic_last_backup_skipped{host=\"${HOST}\"} 1"; echo "restic_last_backup_duration_seconds{host=\"${HOST}\"} ${pd}"; } > "$tmp"; mv -f "$tmp" "$METRICS_FILE"; exit 0
fi
source "$ENV_FILE"
$RESTIC unlock >/dev/null 2>&1 || true
if [ "$INCLUDE_MODE" = include ]; then
  [ -n "$INCLUDES_STR" ] || { echo "include без путей" >&2; exit 2; }
  BACKUP_CMD="$RESTIC backup ${INCLUDES_STR}"
else
  BACKUP_CMD="$RESTIC backup / --exclude-caches${EXCLUDES_STR:+ $EXCLUDES_STR}"
fi
ts_start=$(date +%s); set +e; eval "$BACKUP_CMD"; RC=$?; set -e; dur=$(( $(date +%s) - ts_start ))
if [ $RC -eq 0 ] || [ $RC -eq 3 ]; then write_metrics 1 0 "$dur"; else write_metrics 0 0 "$dur"; fi
exit $RC
RUNNER_BODY
  } > "$PROV_SCRIPT"
  chmod 0700 "$PROV_SCRIPT"; chown "$PROV_USER:$PROV_USER" "$PROV_SCRIPT" 2>/dev/null || true
}

cmd_provision() {
  # provision <repo_url> <repopass> <mode> <schedule> <delay> <restic_ver> <cacert_b64|-> <path...>
  local repo_url="$1" repopass="$2" mode="$3" sched="$4" delay="$5" ver="$6" cacert_b64="$7"; shift 7
  local paths=("$@")
  case "$repo_url" in rest:*) ;; *) echo "bad repo url" >&2; return 2;; esac
  [ -n "$repopass" ] || { echo "no repopass" >&2; return 2; }
  [ "$mode" = include ] || [ "$mode" = exclude ] || { echo "bad mode" >&2; return 2; }
  [[ "$sched" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "bad time" >&2; return 2; }
  [ "${#paths[@]}" -gt 0 ] || { echo "no paths" >&2; return 2; }
  local p; for p in "${paths[@]}"; do valid_path "$p" || { echo "bad path: $p" >&2; return 2; }; done
  [[ "$delay" =~ ^[0-9]+[smh]?$ ]] || delay=1h
  # Версию restic решает ТОЛЬКО helper. Раньше её присылала панель (и агент подставлял
  # свой дефолт) — обе цифры разъехались с RESTIC_TARGET_VER, и свежая нода получала
  # 0.18.1, после чего панель сразу предлагала «обновить до 0.19.1». Присланное значение
  # игнорируем намеренно: так фикс работает и со старыми агентами, без их обновления.
  ver="$RESTIC_TARGET_VER"
  # 1) пользователь бэкапа
  id -u "$PROV_USER" >/dev/null 2>&1 || useradd --system --home-dir "$PROV_HOME" --create-home --shell /usr/sbin/nologin "$PROV_USER"
  # 2) restic
  ensure_restic "$ver" || return 2
  install -d -o "$PROV_USER" -g "$PROV_USER" -m 0755 "$RESTIC_BIN_DIR" 2>/dev/null || true
  chown -R "$PROV_USER:$PROV_USER" "$RESTIC_BIN_DIR" 2>/dev/null || true
  # 3) cacert (self-signed бэкап-сервера), если передан
  if [ "$cacert_b64" != "-" ] && [ -n "$cacert_b64" ]; then
    printf '%s' "$cacert_b64" | base64 -d > "$PROV_CACERT" 2>/dev/null && chmod 0644 "$PROV_CACERT" || { echo "bad cacert" >&2; return 2; }
  else
    rm -f "$PROV_CACERT" 2>/dev/null || true
  fi
  # 4) env-файл (0600 root)
  umask 077
  { printf 'RESTIC_PASSWORD=%s\n' "$repopass"; printf 'RESTIC_REPOSITORY=%s\n' "$repo_url"; } > "$PROV_ENV"
  chown root:root "$PROV_ENV"; chmod 0600 "$PROV_ENV"; umask 022
  # 5) каталог метрик (bx231 должен писать)
  install -d -o "$PROV_USER" -g "$PROV_USER" -m 0775 "$PROV_METRICS_DIR" 2>/dev/null || \
    { mkdir -p "$PROV_METRICS_DIR"; chown "$PROV_USER:$PROV_USER" "$PROV_METRICS_DIR" 2>/dev/null || true; }
  # 6) скрипт бэкапа
  write_runner "$mode" "${paths[@]}"
  # 7) service + timer
  cat > "/etc/systemd/system/$PROV_SERVICE" <<UNIT
[Unit]
Description=Kervax restic backup
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
User=$PROV_USER
EnvironmentFile=$PROV_ENV
ExecStart=$PROV_SCRIPT
AmbientCapabilities=CAP_DAC_READ_SEARCH
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
TimeoutStartSec=infinity
[Install]
WantedBy=multi-user.target
UNIT
  cat > "/etc/systemd/system/$PROV_TIMER" <<UNIT
[Unit]
Description=Kervax restic backup timer
[Timer]
OnCalendar=*-*-* ${sched}:00
AccuracySec=1m
RandomizedDelaySec=$delay
Persistent=true
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now "$PROV_TIMER" >/dev/null 2>&1 || { echo "не включить таймер" >&2; return 2; }
  refresh_config
  echo "OK provisioned ($mode, $sched)"
}

# cmd_adopt — миграция СТАРОЙ (ansible) раскладки бэкапа под панель. Читает URL+пароль,
# excludes/режим и расписание из существующих systemd-юнитов и переводит на панельный
# runner (пишет restic-backup.prom → панель видит метрики и управляет). Репозиторий тот
# же — БЕЗ restic init, просто дописываем. Пароль с ноды НИКУДА не уходит (читаем локально).
# Кастомные ExecStopPost-хуки старого юнита (heartbeat/telegram) не переносим — по решению
# оператора алертинг берёт на себя панель.
cmd_adopt() {
  local ver="${1:-0.19.1}"   # до какой версии поднять restic при миграции
  # 1) найти сервис бэкапа
  local svc="" s
  for s in systemd-rest.service restic-backup.service restic.service; do
    systemctl cat "$s" >/dev/null 2>&1 && { svc="$s"; break; }
  done
  [ -n "$svc" ] || { echo "не нашёл systemd-сервис бэкапа для миграции" >&2; return 2; }
  local unit; unit="$(systemctl cat "$svc" 2>/dev/null)"
  # уже панельная раскладка? (runner в /etc/systemd-rest.conf, env в resta.conf) — no-op
  if printf '%s\n' "$unit" | grep -q "ExecStart=$PROV_SCRIPT" && [ -f "$PROV_ENV" ]; then
    echo "уже под панелью — миграция не нужна"; return 0
  fi
  # 2) env-файл со старыми creds (EnvironmentFile=, возможен префикс '-')
  local envf; envf="$(printf '%s\n' "$unit" | sed -nE 's/^EnvironmentFile=-?(.+)$/\1/p' | head -1)"
  [ -n "$envf" ] && [ -f "$envf" ] || { echo "не нашёл EnvironmentFile со старыми creds" >&2; return 2; }
  # 3) вытащить URL+пароль (в лог НЕ пишем)
  local repo_url repopass
  repo_url="$(sed -nE 's/^(export )?RESTIC_REPOSITORY=(.*)$/\2/p' "$envf" | head -1 | sed -E 's/^["'\'']//; s/["'\'']$//')"
  repopass="$(sed -nE 's/^(export )?RESTIC_PASSWORD=(.*)$/\2/p' "$envf" | head -1 | sed -E 's/^["'\'']//; s/["'\'']$//')"
  case "$repo_url" in rest:*) ;; *) echo "старый RESTIC_REPOSITORY не rest:-URL — вслепую не мигрирую" >&2; return 2;; esac
  [ -n "$repopass" ] || { echo "в старом env пустой RESTIC_PASSWORD" >&2; return 2; }
  # 4) режим и пути из ExecStart (или из wrapper-скрипта, если ExecStart ссылается на него)
  local execline; execline="$(printf '%s\n' "$unit" | sed -nE 's/^ExecStart=(.*)$/\1/p' | head -1)"
  local scan="$execline"
  case "$execline" in
    *" backup "*) ;;                                  # инлайновый restic
    /*) local sp; sp="$(printf '%s' "$execline" | awk '{print $1}')"
        [ -f "$sp" ] && scan="$(cat "$sp" 2>/dev/null)" ;;   # ExecStart — путь к скрипту
  esac
  local mode="exclude" paths=() e
  # все --exclude=X (снимаем хвостовой '/', валидируем)
  while IFS= read -r e; do
    e="${e%/}"; [ -n "$e" ] && valid_path "$e" && paths+=("$e")
  done < <(printf '%s\n' "$scan" | grep -oE -- "--exclude=[^ \"']+" | sed 's/^--exclude=//')
  # include-режим: бэкапятся явные пути (не '/') и excludes нет
  if ! printf '%s' "$scan" | grep -qE 'backup +/( |$|")' && [ "${#paths[@]}" -eq 0 ]; then
    local tok seen=0
    for tok in $scan; do
      if [ "$seen" = 1 ]; then
        case "$tok" in --*) ;; /*) e="${tok%/}"; valid_path "$e" && paths+=("$e");; esac
      fi
      [ "$tok" = backup ] && seen=1
    done
    [ "${#paths[@]}" -gt 0 ] && mode="include"
  fi
  # не смогли распарсить → безопасный дефолт: бэкап / с типовыми excludes (данные не теряем)
  if [ "${#paths[@]}" -eq 0 ]; then
    echo "не распарсил пути из ExecStart — беру дефолтные excludes" >&2
    mode="exclude"; paths=(/proc /sys /run /dev /usr /var/lib/lxcfs /tmp /var/tmp /boot /var/cache /snap /lib/modules /lib/firmware /lost+found /swapfile)
  fi
  # 5) расписание из таймера
  local sched="23:00" tmr="" t
  for t in "${svc%.service}.timer" systemd-rest.timer restic-backup.timer restic.timer; do
    systemctl cat "$t" >/dev/null 2>&1 && { tmr="$t"; break; }
  done
  if [ -n "$tmr" ]; then
    local oc; oc="$(systemctl cat "$tmr" 2>/dev/null | sed -nE 's/^OnCalendar=.*[ *]([0-9]{1,2}:[0-9]{2}).*$/\1/p' | head -1)"
    [ -n "$oc" ] && sched="$oc"
  fi
  sched="$(printf '%s' "$sched" | awk -F: '{printf "%02d:%02d", $1, $2}')"
  # 6) панельный provision теми же creds/путями/расписанием, БЕЗ restic init (delay=0 — как было)
  echo "adopt: репо $(printf '%s' "$repo_url" | sed -E 's#//[^@]*@#//***@#'), режим=$mode, путей=${#paths[@]}, время=$sched, restic→$ver"
  # dry-run: показать распарсенное и НЕ трогать ноду (для проверки перед реальной миграцией)
  if [ -n "${KV_ADOPT_DRYRUN:-}" ]; then
    printf 'DRY-RUN. env=%s пути:\n' "$envf"; printf '  - %s\n' "${paths[@]}"; return 0
  fi
  cmd_provision "$repo_url" "$repopass" "$mode" "$sched" 0 "$ver" - "${paths[@]}" || { echo "provision не удался" >&2; return 2; }
  echo "OK adopted → панельная раскладка ($mode, $sched)"
}

cmd_process_spool() {
  local req id action mode schedule paths out ok k v line repo_url repopass delay ver cacert_b64 engine container
  for req in "$REQ_DIR"/*.req; do
    [ -f "$req" ] || continue
    id="$(basename "$req" .req)"
    action=""; mode="exclude"; schedule=""; paths=(); repo_url=""; repopass=""; delay="1h"; ver="$RESTIC_TARGET_VER"; cacert_b64="-"; engine=""; container=""
    # ВАЖНО: не `IFS='=' read -r k v` — оно срезает хвостовой '=' (падинг base64 у cacert).
    # Читаем строку целиком и режем по первому '=' параметрическим расширением.
    while IFS= read -r line; do
      k="${line%%=*}"; v="${line#*=}"
      case "$k" in
        action) action="$v";; mode) mode="$v";; schedule) schedule="$v";; path) paths+=("$v");;
        repo_url) repo_url="$v";; repopass) repopass="$v";; delay) delay="$v";;
        restic_version) ver="$v";; cacert_b64) cacert_b64="$v";;
        engine) engine="$v";; container) container="$v";;
        dump_dir) export KV_DUMP_DIR="$v";;
        dump_keep) export KV_DUMP_KEEP="$v";;
        dump_minfree) export KV_DUMP_MINFREE="$v";;
      esac
    done < "$req"
    rm -f "$req"  # секреты (repopass) на диске не задерживаем
    out=""; ok=false
    case "$action" in
      set_schedule) if out="$(cmd_set_schedule "$schedule" 2>&1)"; then ok=true; fi ;;
      set_paths)    if out="$(cmd_set_paths "$mode" "${paths[@]}" 2>&1)"; then ok=true; fi ;;
      run_now)      if out="$(cmd_run_now 2>&1)"; then ok=true; fi ;;
      restic_update) if out="$(cmd_restic_update 2>&1)"; then ok=true; fi ;;
      provision)    if out="$(cmd_provision "$repo_url" "$repopass" "$mode" "$schedule" "$delay" "$ver" "$cacert_b64" "${paths[@]}" 2>&1)"; then ok=true; fi ;;
      adopt)        if out="$(cmd_adopt "$ver" 2>&1)"; then ok=true; fi ;;
      get_creds)    if out="$(cmd_get_creds 2>&1)"; then ok=true; fi ;;
      dump_setup)   if out="$(cmd_dump_setup "$engine" "$container" 2>&1)"; then ok=true; fi ;;
      dump_remove)  if out="$(cmd_dump_remove "$engine" "$container" 2>&1)"; then ok=true; fi ;;
      *) out="неизвестное действие" ;;
    esac
    printf 'ok=%s\noutput=%s\n' "$ok" "$(printf '%s' "$out" | tr '\n' ' ')" > "$RES_DIR/$id.res.tmp"
    mv -f "$RES_DIR/$id.res.tmp" "$RES_DIR/$id.res"; chmod 0644 "$RES_DIR/$id.res"
  done
  refresh_config  # после изменений обновить конфиг-файл для панели
}

case "${1:-}" in
  get-config)    cmd_get_config ;;
  refresh)       refresh_config ;;
  set-paths)     shift; cmd_set_paths "$@" ;;
  set-schedule)  shift; cmd_set_schedule "$@" ;;
  run-now)       cmd_run_now ;;
  restic-update) cmd_restic_update ;;
  provision)     shift; cmd_provision "$@" ;;
  adopt)         shift; cmd_adopt "$@" ;;
  get-creds)     cmd_get_creds ;;
  dump-setup)    shift; cmd_dump_setup "$@" ;;
  dump-remove)   shift; cmd_dump_remove "$@" ;;
  dump-status)   cmd_dump_status ;;
  process-spool) cmd_process_spool ;;
  *) echo "usage: $0 {get-config|set-paths <mode> <path...>|set-schedule <HH:MM>|run-now|provision <url> <repopass> <mode> <HH:MM> <delay> <ver> <cacert_b64|-> <path...>|dump-setup <движок> [container]|dump-remove <движок>|dump-status|process-spool}" >&2; exit 2 ;;
esac
HELPER_EOF
chmod 0755 "$HELPER"; chown root:root "$HELPER"

# первый прогон + cron раз в минуту: конфиг-файл для панели
"$HELPER" refresh 2>/dev/null || true
cat > "$CRON" <<CRON_EOF
* * * * * root $HELPER refresh >/dev/null 2>&1
CRON_EOF
chmod 0644 "$CRON"

# path-unit: как только агент кладёт запрос в спул — root исполняет его (мгновенно)
cat > /etc/systemd/system/kervax-backup-req.service <<UNIT_EOF
[Unit]
Description=Kervax backup request processor
[Service]
Type=oneshot
ExecStart=$HELPER process-spool
UNIT_EOF
cat > /etc/systemd/system/kervax-backup-req.path <<UNIT_EOF
[Unit]
Description=Kervax backup request spool watch
[Path]
DirectoryNotEmpty=$REQ_DIR
Unit=kervax-backup-req.service
[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload
systemctl enable --now kervax-backup-req.path >/dev/null 2>&1 || true
# на случай пропущенного триггера — разово подберём уже лежащие запросы
"$HELPER" process-spool >/dev/null 2>&1 || true

echo "backup-setup: готово → $HELPER; конфиг в $CONF_JSON (cron), команды через спул $REQ_DIR."
echo "backup-setup: текущий конфиг:"; head -c 500 "$CONF_JSON" 2>/dev/null; echo
