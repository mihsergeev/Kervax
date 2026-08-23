#!/usr/bin/env bash
# Kervax: инвентарь СУБД (базы, их размеры, логины, версия движка) — для раздела «Сервисы».
# Неприв. агент (kervax) в контейнеры ходить не может (docker-exec запрещён его проксёй),
# поэтому root-хелпер по таймеру опрашивает движки и пишет /var/lib/kervax/db-stats.json.
# Агент ТОЛЬКО ЧИТАЕТ этот файл.
#
# ПАРОЛИ НЕ ХРАНИМ И НЕ ПЕРЕДАЁМ: в контейнер ходим `docker exec` под служебным юзером,
# пароль (если нужен) берём из окружения САМОГО контейнера — ровно как уже делает
# дамп-хелпер backup-setup. Наружу отдаём только имена баз/логинов, размеры и версию.
set -euo pipefail

KERVAX_SETUP_VERSION=0.2  # МАЖОР.МИНОР; сравнивается покомпонентно
KERVAX_SETUP_ALWAYS=1     # безопасно на любой ноде: без СУБД собирает пустой файл

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-db-stats"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/db-stats.json"

if [ "$(id -u)" != 0 ]; then echo "Нужен root." >&2; exit 1; fi

# Родителя /var/lib/kervax задаём 0755 ЯВНО (под umask 077 неприв. агент иначе не войдёт).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions"

cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Снимает инвентарь СУБД → /var/lib/kervax/db-stats.json. Только чтение, без паролей наружу.
set -u
OUT=/var/lib/kervax/db-stats.json
TMP="$OUT.tmp.$$"
TO=10   # таймаут на один запрос: подвисшая база не должна вешать сбор

have() { command -v "$1" >/dev/null 2>&1; }
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\000-\037'; }

# «имя|размер» построчно → JSON-массив [{"name":…,"size":…}]. Ограничение сверху:
# на ноде бывает под сотню инстансов (видели 104), полный список раздул бы отчёт.
MAXDB=20
MAXUSR=50
# ВАЖНО: размер печатаем СТРОКОЙ (%s), а не числом. У mawk (дефолт в Debian/Ubuntu)
# printf "%d" 32-битный: всё больше 2 ГиБ схлопывалось ровно в 2147483648 — база на
# 2.6 ГБ показывалась как «2048.0 МБ», и так у каждой крупной. Значение и так приходит
# из БД целым, конвертировать его незачем.
dbs_json() {
  awk -F'|' -v max="$MAXDB" 'BEGIN{printf "["; n=0}
    n>=max{next}
    { name=$1; size=$2;
      gsub(/^[ \t]+|[ \t]+$/,"",name); gsub(/^[ \t]+|[ \t]+$/,"",size);
      if (name=="") next;
      if (size !~ /^[0-9]+$/) size="0";
      gsub(/\\/,"\\\\",name); gsub(/"/,"\\\"",name);
      printf "%s{\"name\":\"%s\",\"size\":%s}", (n++?",":""), name, size }
    END{printf "]"}'
}
# построчный список → JSON-массив строк
list_json() {
  awk -v max="$MAXUSR" 'BEGIN{printf "["; n=0}
    n>=max{next}
    { s=$0; gsub(/^[ \t]+|[ \t]+$/,"",s); if (s=="") next;
      gsub(/\\/,"\\\\",s); gsub(/"/,"\\\"",s);
      printf "%s\"%s\"", (n++?",":""), s }
    END{printf "]"}'
}

ENTRIES=""
add_entry() { # engine container version dbs_json users_json
  local e="$1" c="$2" v="$3" d="$4" u="$5"
  [ "$d" = "[]" ] && [ "$u" = "[]" ] && [ -z "$v" ] && return 0  # совсем пусто — не шумим
  ENTRIES="$ENTRIES${ENTRIES:+,}{\"engine\":\"$(esc "$e")\",\"container\":\"$(esc "$c")\",\"version\":\"$(esc "$v")\",\"dbs\":$d,\"users\":$u}"
}

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
# psql запускаем от служебного юзера postgres (peer-аутентификация по сокету) —
# пароль не нужен; фолбэк на POSTGRES_USER контейнера, если образ нестандартный.
pg_q() { # container query
  local c="$1" q="$2" u
  if [ -n "$c" ]; then
    u=$(docker exec "$c" printenv POSTGRES_USER 2>/dev/null || true)
    timeout $TO docker exec -u postgres "$c" psql -Atqc "$q" 2>/dev/null \
      || timeout $TO docker exec "$c" psql -Atqc "$q" ${u:+-U "$u"} 2>/dev/null
  else
    timeout $TO su -s /bin/sh postgres -c "psql -Atqc \"$q\"" 2>/dev/null
  fi
}
collect_pg() {
  local c="$1" v d u
  v=$(pg_q "$c" "show server_version" | head -1 | awk '{print $1}')
  [ -z "$v" ] && return 0
  d=$(pg_q "$c" "select datname||'|'||pg_database_size(datname) from pg_database where not datistemplate order by pg_database_size(datname) desc" | dbs_json)
  u=$(pg_q "$c" "select rolname from pg_roles where rolcanlogin order by 1" | list_json)
  add_entry pg "$c" "$v" "$d" "$u"
}

# ── MySQL / MariaDB ────────────────────────────────────────────────────────────
# Пароль берём из окружения контейнера ВНУТРИ него (MYSQL_PWD), в аргументы не
# попадает — иначе светился бы в `ps` на хосте.
my_q() { # container query
  local c="$1" q="$2"
  if [ -n "$c" ]; then
    timeout $TO docker exec "$c" sh -c 'MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}" exec mysql -u root -N -B -e "'"$q"'"' 2>/dev/null
  else
    timeout $TO mysql -N -B -e "$q" 2>/dev/null
  fi
}
collect_mysql() {
  local c="$1" v d u
  v=$(my_q "$c" "select version()" | head -1)
  [ -z "$v" ] && return 0
  d=$(my_q "$c" "select table_schema, ifnull(sum(data_length+index_length),0) from information_schema.tables where table_schema not in ('information_schema','performance_schema','sys') group by table_schema order by 2 desc" \
      | awk -F'\t' '{print $1"|"$2}' | dbs_json)
  u=$(my_q "$c" "select distinct user from mysql.user order by user" | list_json)
  add_entry mysql "$c" "$v" "$d" "$u"
}

# ── ClickHouse ─────────────────────────────────────────────────────────────────
ch_q() { local c="$1" q="$2"; [ -n "$c" ] \
  && timeout $TO docker exec "$c" clickhouse-client --query "$q" 2>/dev/null \
  || timeout $TO clickhouse-client --query "$q" 2>/dev/null; }
collect_ch() {
  local c="$1" v d u
  v=$(ch_q "$c" "select version()" | head -1)
  [ -z "$v" ] && return 0
  d=$(ch_q "$c" "select database, sum(bytes_on_disk) from system.parts where active group by database order by 2 desc format TabSeparated" \
      | awk -F'\t' '{print $1"|"$2}' | dbs_json)
  u=$(ch_q "$c" "select name from system.users order by name format TabSeparated" | list_json)
  add_entry clickhouse "$c" "$v" "$d" "$u"
}

# ── Redis ──────────────────────────────────────────────────────────────────────
# «Базы» у Redis — db0..dbN; вместо размера показываем число ключей (размер только
# суммарный, по used_memory — на отдельные БД он не разбит).
rd_q() { local c="$1" a="$2"; [ -n "$c" ] \
  && timeout $TO docker exec "$c" redis-cli info "$a" 2>/dev/null \
  || timeout $TO redis-cli info "$a" 2>/dev/null; }
collect_redis() {
  local c="$1" v d
  v=$(rd_q "$c" server | awk -F: '/^redis_version:/{print $2}' | tr -d '\r' | head -1)
  [ -z "$v" ] && return 0
  d=$(rd_q "$c" keyspace | tr -d '\r' | awk -F'[:,=]' '/^db[0-9]+:/{print $1"|"$3}' | dbs_json)
  add_entry redis "$c" "$v" "$d" "[]"
}

# ── обход: контейнеры по образу + хостовые процессы ────────────────────────────
if have docker; then
  docker ps --format '{{.Names}}|{{.Image}}' 2>/dev/null | while IFS='|' read -r name img; do
    echo "$name|$img"
  done > /tmp/kv-dbs.$$ 2>/dev/null || : > /tmp/kv-dbs.$$
  while IFS='|' read -r name img; do
    [ -n "$name" ] || continue
    case "$(printf '%s' "$img" | tr 'A-Z' 'a-z')" in
      *postgres*|*timescale*|*pgvector*) collect_pg "$name" ;;
      *mariadb*|*mysql*|*percona*)       collect_mysql "$name" ;;
      *clickhouse*)                      collect_ch "$name" ;;
      *redis*|*valkey*)                  collect_redis "$name" ;;
    esac
  done < /tmp/kv-dbs.$$
  rm -f /tmp/kv-dbs.$$
fi
# хостовые (не в docker) — только если движок реально слушает локально
pgrep -x postgres        >/dev/null 2>&1 && have psql             && collect_pg ""
pgrep -x mysqld          >/dev/null 2>&1 && have mysql            && collect_mysql ""
pgrep -x mariadbd        >/dev/null 2>&1 && have mysql            && collect_mysql ""
pgrep -x redis-server    >/dev/null 2>&1 && have redis-cli        && collect_redis ""
pgrep -x clickhouse-serv >/dev/null 2>&1 && have clickhouse-client && collect_ch ""

printf '{"ts":%s,"items":[%s]}\n' "$(date +%s)" "$ENTRIES" > "$TMP"
mv -f "$TMP" "$OUT"
chmod 0644 "$OUT"
HELPER_EOF
chmod 0755 "$HELPER"

cat > /etc/systemd/system/kervax-db-stats.service <<EOF
[Unit]
Description=Kervax: инвентарь СУБД (базы, размеры, логины)
[Service]
Type=oneshot
ExecStart=$HELPER
EOF
cat > /etc/systemd/system/kervax-db-stats.timer <<'EOF'
[Unit]
Description=Kervax: периодически обновлять инвентарь СУБД
[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
Persistent=true
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-db-stats.timer >/dev/null 2>&1 || true
"$HELPER" || true   # первый прогон сразу

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/dbstat-setup.ver"
chmod 0644 "$STATE_DIR/versions/dbstat-setup.ver"
echo "✓ dbstat-setup: инвентарь СУБД → $OUT (обновление при старте и каждые 15 мин)."
