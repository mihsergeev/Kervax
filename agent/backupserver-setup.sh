#!/usr/bin/env bash
# Kervax: включить статистику И ПРОВИЖИНИНГ СЕРВЕРА бэкапов (rest-server) для панели.
#
# Агент под непривилегированным `kervax` + NoNewPrivileges НЕ может sudo и не видит
# репозитории restic в /app/rest-server/data/<client> (root 0700). Поэтому:
#  • root-cron запускает read-only helper и кладёт статистику в
#    /var/lib/kervax/backupserver.json (world-readable) — агент просто читает файл;
#  • ПРОВИЖИНИНГ (htpasswd/init/ufw/prune/tls-front) идёт ЧЕРЕЗ СПУЛ: агент кладёт
#    запрос в /var/lib/kervax/bsrv-req (0600, секреты hpass/repopass внутри), root
#    path-unit исполняет узким helper и пишет ответ в /var/lib/kervax/bsrv-res.
# Агент остаётся полностью изолированным. Действия строго из белого списка, имена/IP
# валидируются, секреты в запросе живут до обработки и сразу удаляются. Запускать root'ом.
set -euo pipefail

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-backupserver-helper"
STATE_DIR=/var/lib/kervax
STATS="$STATE_DIR/backupserver.json"
REQ_DIR="$STATE_DIR/bsrv-req"
RES_DIR="$STATE_DIR/bsrv-res"
CRON=/etc/cron.d/kervax-backupserver
AGENT_USER=kervax
# helper обслуживает агента (спул принадлежит группе kervax) — без агента он бессмыслен.
# Без этой проверки падало невнятным «install: invalid group: kervax».
if ! getent group "$AGENT_USER" >/dev/null 2>&1; then
  echo "На этой ноде нет агента Kervax (нет группы '$AGENT_USER')." >&2
  echo "Сначала заведите ноду в панель (Сервера → Добавить), потом запустите этот скрипт." >&2
  exit 2
fi

# v2: provision-client подключается к УЖЕ СУЩЕСТВУЮЩЕМУ репо (переиспользует его пароль),
# а не ломает его новым — иначе клиент/prune/восстановление получали неверный пароль
# v3: deploy-server — поднять rest-server с нуля на чистой ноде (docker/htpasswd/restic из
# штатных реп + compose с зашитыми --append-only --private-repos)
# v4: stats читает retention и из легаси-монолита /etc/systemd-rest.conf (только чтение)
# v5: stats отдаёт lock_ts — панель отличает лок ИДУЩЕГО бэкапа от висячего (алерт был ложным)
# v6 (0.14): TLS без caddy-слоя — deploy_tls_front поднимает ВТОРОЙ rest-server с нативным
#           --tls на :64101 (те же данные/htpasswd), а старый caddy-фронт сносит (миграция)
# v7 (0.15): TLS-проект переехал в свой каталог /app/rest-server-tls (был зарыт в system/,
#           где лежат скрипты/метрики); deploy_tls_front мигрирует старую раскладку
# v8 (0.16): ФИКС: prune-env писался БЕЗ `set -a` → переменные не экспортировались, restic в
#           prune-скрипте не видел RESTIC_REPOSITORY/PASSWORD и чистка молча не работала
#           («repo not accessible», success=0) у ВСЕХ репо, заведённых панелью. Env теперь
#           экспортируется, а установка чинит уже созданные env'ы (миграция ниже).
KERVAX_SETUP_VERSION=0.20  # МАЖОР.МИНОР; сравнивается покомпонентно (0.13 > 0.2!)
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" /var/lib/kervax/versions
echo "$KERVAX_SETUP_VERSION" > /var/lib/kervax/versions/backupserver-setup.ver
chmod 0644 /var/lib/kervax/versions/backupserver-setup.ver  # явно: агент (kervax) обязан прочитать
# файловая/спул-схема sudo не требует — снимаем старое sudoers-правило (если было)
rm -f /etc/sudoers.d/kervax-backupserver 2>/dev/null || true
# спул: агент (kervax) кладёт запросы (нужен -wx на каталог), читает ответы (r-x)
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
# Kervax backup-server helper (root): read-only stats + узкий провижининг клиентов.
set -euo pipefail

DATA=/app/rest-server/data
HTPASSWD="$DATA/.htpasswd"
COMPOSE=/app/rest-server/docker-compose.yml

# Фактический порт rest-server. Панель разрешает развернуть его на своём порту
# (в форме есть поле), а провижининг клиента раньше ходил по КОНСТАНТЕ 64100 —
# сервер на другом порту разворачивался успешно, а следующий шаг падал с
# невнятным «init failed». Берём порт оттуда, где он записан на самом деле.
rest_port() {
  local p=""
  [ -f "$COMPOSE" ] && p="$(grep -oE '"[0-9]+:8000"' "$COMPOSE" | head -1 | grep -oE '^"[0-9]+' | tr -d '"')"
  printf '%s' "${p:-$REST_PORT}"
}
PRUNE_DIR=/app/rest-server/system/scripts
ENV_DIR=/app/rest-server/system/envs
LOG_DIR=/app/rest-server/system/logs
# Метрики ротации пишем ТУДА, ГДЕ ИХ ЧИТАЮТ: стандартный textfile_collector — его
# разбирают и агент, и node-exporter. Свой каталог остаётся вторым адресом (там их
# ждут старые сборки), но он «в стол»: ни один компонент туда не смотрит, из-за чего
# мёртвая ротация 17 дней выглядела зелёной.
NE_METRICS_DIR=/var/lib/node_exporter/textfile_collector
METRICS_DIR=/app/rest-server/system/metrics
# TLS-rest-server живёт СВОИМ compose-проектом рядом с основным (не в system/ со скриптами
# и не вторым сервисом в /app/rest-server/docker-compose.yml — тот файл ansible-managed).
TLS_DIR=/app/rest-server-tls
TLS_DIR_OLD=/app/rest-server/system/kervax-tls  # раскладка 0.14 (мигрируем в deploy_tls_front)
REST_PORT=64100
TLS_PORT=64101
HELPER_VER=1  # версия backupserver-helper; панель флагует ноды со старым helper'ом
REQ_DIR=/var/lib/kervax/bsrv-req
RES_DIR=/var/lib/kervax/bsrv-res
# образ rest-server ЗАШИТ в helper (панель не выбирает — иначе это вектор подмены образа)
REST_IMAGE="restic/rest-server:0.14.0"
# restic для сервера (init/cat config/prune). Системный, иначе свой в /lib65 (НЕ /usr:
# /usr исключён из бэкапа). Порядок важен: на живых серверах уже есть системный.
KERVAX_RESTIC=/lib65/kervax/restic
RESTIC_BIN="$(command -v restic || true)"
[ -n "$RESTIC_BIN" ] || RESTIC_BIN="$KERVAX_RESTIC"

# где реально лежит серт: новая раскладка, иначе старая (нода могла не пройти миграцию —
# stats/get-cert/ufw обязаны видеть HTTPS в обоих случаях)
tls_dir() {
  if [ -f "$TLS_DIR/cert.pem" ]; then echo "$TLS_DIR"
  elif [ -f "$TLS_DIR_OLD/cert.pem" ]; then echo "$TLS_DIR_OLD"
  else echo "$TLS_DIR"; fi
}

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
# извлечь число keep-<x> из prune-скрипта (--keep-last 3 …). 0, если нет (pipefail-safe).
keep_of() {
  [ -f "$2" ] || { echo 0; return 0; }
  local v; v=$(grep -oE -- "--$1[= ]+[0-9]+" "$2" 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)
  echo "${v:-0}"
}
# ЛЕГАСИ (только чтение, для отображения): на серверах, поднятых руками/ансиблом, чистка
# бывает одним монолитом — репы идут блоками «RESTIC_REPOSITORY=<data>/<имя>` … `restic
# forget --keep-*`. Без этого панель показывала пустую политику у полностью чистящихся реп.
# Панель НИЧЕГО тут не меняет: новые бэкапы всегда получают свой per-repo скрипт (install_prune).
LEGACY_PRUNE=/etc/systemd-rest.conf
keep_of_legacy() {
  local flag="$1" name="$2" v
  [ -f "$LEGACY_PRUNE" ] || { echo 0; return 0; }
  # блок = от строки ИМЕННО этой репы до следующей RESTIC_REPOSITORY=. Сравнение точное:
  # по подстроке `backup-01` поймал бы и `backup-01-dev` — политика уехала бы от соседа.
  v=$(awk -v repo="RESTIC_REPOSITORY=$DATA/$name" '
        $0 == repo { inblk = 1; next }
        inblk && /^[[:space:]]*RESTIC_REPOSITORY=/ { exit }
        inblk { print }
      ' "$LEGACY_PRUNE" 2>/dev/null \
      | grep -oE -- "--$flag[= ]+[0-9]+" | grep -oE '[0-9]+' | head -1 || true)
  echo "${v:-0}"
}
# валидация имени клиента (= имя репо/htpasswd-юзера): только hostname-безопасные символы
valid_name() { case "$1" in ''|*[!A-Za-z0-9._-]*) return 1 ;; *) return 0 ;; esac; }
valid_ip()   { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || [[ "$1" =~ ^[0-9A-Fa-f:]+$ ]]; }
valid_num()  { [[ "$1" =~ ^[0-9]+$ ]]; }

cmd_stats() {
  if [ ! -d "$DATA" ]; then echo '{"present":false}'; return; fi
  # РЕАЛЬНАЯ версия из бинаря, а не тег из compose: тег «latest» врал — на диске мог
  # застрять старый образ (0.11), docker его не перекачивает, а панель думала «свежо».
  local ver="" cid
  cid="$(docker ps -qf 'name=rest-server' 2>/dev/null | head -1)"
  [ -n "$cid" ] && ver="$(docker exec "$cid" rest-server --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  # фолбэк на тег из compose, если контейнера нет/не ответил
  [ -n "$ver" ] || { [ -f "$COMPOSE" ] && ver="$(grep -oE 'rest-server:[A-Za-z0-9._-]+' "$COMPOSE" | head -1 | cut -d: -f2)"; }
  local repos_json="" repo name snaps last locked lock_ts valid size prune kl kd kw km
  for repo in "$DATA"/*/; do
    [ -d "$repo" ] || continue
    name="$(basename "$repo")"
    valid=false; [ -f "${repo}config" ] && valid=true
    [ "$valid" = true ] || [ -d "${repo}data" ] || [ -d "${repo}snapshots" ] || continue
    snaps=0
    [ -d "${repo}snapshots" ] && snaps=$(find "${repo}snapshots" -maxdepth 1 -type f 2>/dev/null | wc -l)
    last=0
    if [ -d "${repo}snapshots" ]; then
      last=$(find "${repo}snapshots" -maxdepth 1 -type f -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)
    fi
    [ -z "$last" ] || [ "$last" = 0 ] && last="$(stat -c %Y "$repo" 2>/dev/null || echo 0)"
    # лок сам по себе не проблема: restic держит его всё время бэкапа и освежает
    # раз в 5 минут. Отдаём mtime самого свежего лока — панель по нему отличает
    # живой бэкап от висячего лока после падения процесса.
    locked=false; lock_ts=0
    if [ -d "${repo}locks" ]; then
      lock_ts=$(find "${repo}locks" -maxdepth 1 -type f -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)
      [ -z "$lock_ts" ] && lock_ts=0
      [ "$lock_ts" != 0 ] && locked=true
    fi
    size="$(du -sb "$repo" 2>/dev/null | cut -f1 || true)"; [ -z "$size" ] && size=0
    prune="$PRUNE_DIR/restic-prune-$name.sh"
    if [ -f "$prune" ]; then
      kl=$(keep_of keep-last "$prune"); kd=$(keep_of keep-daily "$prune")
      kw=$(keep_of keep-weekly "$prune"); km=$(keep_of keep-monthly "$prune")
    else
      # своего скрипта нет — репу может чистить легаси-монолит (см. keep_of_legacy)
      kl=$(keep_of_legacy keep-last "$name"); kd=$(keep_of_legacy keep-daily "$name")
      kw=$(keep_of_legacy keep-weekly "$name"); km=$(keep_of_legacy keep-monthly "$name")
    fi
    repos_json="${repos_json:+$repos_json,}{\"name\":\"$(json_escape "$name")\",\"valid\":$valid,\"snapshots\":$snaps,\"last_activity\":$last,\"locked\":$locked,\"lock_ts\":$lock_ts,\"size_bytes\":$size,\"keep_last\":${kl:-0},\"keep_daily\":${kd:-0},\"keep_weekly\":${kw:-0},\"keep_monthly\":${km:-0}}"
  done
  # Место на томе С РЕПОЗИТОРИЯМИ, а не на / — они часто на отдельном диске, и
  # общая метрика диска ноды про заполнение хранилища бэкапов ничего не говорит.
  # df -P: POSIX-вывод, одна строка на ФС (без -P длинные имена устройств переносятся).
  local dfl d_total=0 d_used=0 d_free=0
  dfl="$(df -PB1 "$DATA" 2>/dev/null | awk 'NR==2{printf "%s %s %s", $2, $3, $4}')"
  case "$dfl" in
    [0-9]*)
      d_total="${dfl%% *}"; dfl="${dfl#* }"
      d_used="${dfl%% *}"; d_free="${dfl##* }" ;;
  esac
  # признак наличия нашего TLS-фронта (для панели — какой транспорт можно предложить)
  local tls=false; [ -f "$(tls_dir)/cert.pem" ] && tls=true
  # РАБОТАЕТ ли контейнер: агент видит это только через docker-прокси, а на свежем
  # бэкап-сервере прокси обычно нет → без этого поля панель считала бы rest-server
  # остановленным и слала ложный алерт. Helper под root'ом знает точно.
  local running=false
  if command -v docker >/dev/null 2>&1; then
    [ "$(docker inspect -f '{{.State.Running}}' rest-server 2>/dev/null)" = "true" ] && running=true
  fi
  # port — ФАКТИЧЕСКИЙ порт rest-server: по нему панель строит адрес репозитория
  # клиента. Раньше она подставляла константу и промахивалась мимо сервера,
  # развёрнутого на другом порту.
  printf '{"present":true,"version":"%s","helper_version":%s,"running":%s,"port":%s,"tls_front":%s,"tls_port":%s,"data_dir":"%s","disk_total":%s,"disk_used":%s,"disk_free":%s,"repos":[%s]}\n' \
    "$ver" "$HELPER_VER" "$running" "$(rest_port)" "$tls" "$TLS_PORT" "$(json_escape "$DATA")" "$d_total" "$d_used" "$d_free" "$repos_json"
}
refresh_stats() { cmd_stats > "$STATE_DIR/backupserver.json.tmp" 2>/dev/null && mv -f "$STATE_DIR/backupserver.json.tmp" "$STATE_DIR/backupserver.json" && chmod 0644 "$STATE_DIR/backupserver.json"; }

# ------- провижининг клиента: htpasswd + init + ufw + prune (транспорт-агностично) -------
install_prune() {
  local name="$1" kl="$2" kd="$3" kw="$4" km="$5" repopass="$6"
  install -d -m 0755 "$PRUNE_DIR" "$ENV_DIR" "$LOG_DIR" "$METRICS_DIR" "$NE_METRICS_DIR"
  # per-client env (repo = локальный путь; пароль нужен для forget/prune на сервере)
  umask 077
  # `set -a` обязателен: prune-скрипт лишь `. env`, а restic — ДОЧЕРНИЙ процесс и видит
  # только ЭКСПОРТИРОВАННОЕ. Без него был «repo not accessible» и чистка не работала.
  cat > "$ENV_DIR/$name.env" <<ENVEOF
# generated by kervax for $name
set -a
RESTIC_REPOSITORY="$DATA/$name"
RESTIC_PASSWORD="$repopass"
set +a
ENVEOF
  chown root:root "$ENV_DIR/$name.env"; chmod 0600 "$ENV_DIR/$name.env"
  umask 022
  write_prune_script "$name" "$kl" "$kd" "$kw" "$km"
}

# Генерация ТОЛЬКО скрипта и его cron — без env: там пароль репозитория, и при
# перегенерации его взять неоткуда (да и незачем).
write_prune_script() {
  local name="$1" kl="$2" kd="$3" kw="$4" km="$5"
  install -d -m 0755 "$PRUNE_DIR" "$LOG_DIR" "$METRICS_DIR" "$NE_METRICS_DIR"
  # per-client prune-скрипт: retention в строке KEEP=(…) — панель читает её через keep_of.
  local ps="$PRUNE_DIR/restic-prune-$name.sh"
  {
    printf '#!/usr/bin/env bash\n'
    printf '# generated by kervax — forget/prune %s\nset -uo pipefail\n' "$name"
    printf 'CLIENT=%q\n' "$name"
    printf 'BIN=%q\n' "$RESTIC_BIN"
    printf 'ENV_FILE=%q\n' "$ENV_DIR/$name.env"
    printf 'LOG=%q\n' "$LOG_DIR/restic-prune-$name.log"
    printf 'METRICS_FILE=%q\n' "$NE_METRICS_DIR/restic_server_$name.prom"
    printf 'METRICS_FILE_ALT=%q\n' "$METRICS_DIR/restic_server_$name.prom"
    printf 'KEEP=(--keep-last %q --keep-daily %q --keep-weekly %q --keep-monthly %q)\n' "$kl" "$kd" "$kw" "$km"
    cat <<'PRUNE_BODY'
set -a; . "$ENV_FILE"; set +a  # restic — дочерний процесс: без экспорта он репо не увидит
mkdir -p "$(dirname "$LOG")" "$(dirname "$METRICS_FILE")" "$(dirname "$METRICS_FILE_ALT")"
ts_start=$(date +%s); success=0
snap_before=-1; snap_after=-1; removed=-1
bytes_before=-1; bytes_after=-1; oldest_ts=0

# Снапшоты считаем БЕЗ jq: на бэкап-серверах его нет, и метрика годами была -1.
# В выводе --json на каждый снапшот приходится ровно один "short_id".
count_snaps() { "$BIN" snapshots --json 2>/dev/null | grep -o '"short_id"' | wc -l | tr -d ' '; }
# Время САМОГО СТАРОГО снапшота (unix). Это главный признак живой ротации: он не
# зависит от того, почему она встала — сломалась группировка, упал prune, снят cron.
# Дробную часть секунд срезаем: не всякий date её принимает.
oldest_snap_ts() {
  local iso
  iso="$("$BIN" snapshots --json 2>/dev/null | grep -o '"time":"[^"]*"' | cut -d'"' -f4 | sort | head -1)"
  [ -n "$iso" ] || { echo 0; return; }
  date -d "$(printf '%s' "$iso" | sed 's/\.[0-9]*//')" +%s 2>/dev/null || echo 0
}
repo_size() { du -sb "$RESTIC_REPOSITORY" 2>/dev/null | awk '{print $1}'; }
{
  echo "=== $(date -Is) ${CLIENT}: start forget/prune ==="
  if ! "$BIN" cat config >/dev/null 2>&1; then
    echo "ERROR: repo not accessible at $RESTIC_REPOSITORY"
  else
    snap_before="$(count_snaps)"; [ -n "$snap_before" ] || snap_before=-1
    bytes_before="$(repo_size)"; [ -n "$bytes_before" ] || bytes_before=-1
    "$BIN" unlock >/dev/null 2>&1 || true
    # ЗАЩИТА: keep-last<1 → НЕ прунить (иначе forget сотрёт все снапшоты). Defense-in-depth.
    kl_val=1; for _i in "${!KEEP[@]}"; do [ "${KEEP[$_i]}" = "--keep-last" ] && kl_val="${KEEP[$((_i+1))]}"; done
    if ! [ "${kl_val:-0}" -ge 1 ] 2>/dev/null; then
      echo "SAFETY: keep-last<1 → forget/prune ПРОПУЩЕН (защита от полного стирания)"
      success=1
    else
      # --group-by host,tags: по умолчанию restic группирует по host+paths и применяет
      # политику к каждой группе отдельно. Если клиент бэкапит файл с датой в имени
      # (shared-20260812-030002.zip.enc), каждый снапшот образует группу из одного
      # элемента, становится в ней «last snapshot» и не удаляется никогда.
      forget_rc=0; "$BIN" forget --group-by host,tags "${KEEP[@]}" 2>&1 || forget_rc=$?
      prune_rc=0;  "$BIN" prune 2>&1 || prune_rc=$?
      [ "$forget_rc" -eq 0 ] && [ "${prune_rc:-0}" -eq 0 ] && success=1
    fi
  fi
  # Замер ПОСЛЕ prune: раньше размер снимался до чистки, и репо на 1.5 ГБ
  # отчитывался как 4.6 ГиБ — метрика показывала то, чего уже нет.
  snap_after="$(count_snaps)"; [ -n "$snap_after" ] || snap_after=-1
  bytes_after="$(repo_size)"; [ -n "$bytes_after" ] || bytes_after=-1
  oldest_ts="$(oldest_snap_ts)"
  if [ "$snap_before" -ge 0 ] && [ "$snap_after" -ge 0 ] 2>/dev/null; then
    removed=$(( snap_before - snap_after ))
    [ "$removed" -lt 0 ] && removed=0
  fi
  echo "=== $(date -Is) ${CLIENT}: done (success=${success}, снапшотов ${snap_before}→${snap_after}, удалено ${removed}) ==="
} >> "$LOG" 2>&1
ts_end=$(date +%s)
{
  # prune_success — отработали ли КОМАНДЫ без ошибки. Это НЕ «ротация состоялась»:
  # forget, которому нечего удалять, тоже возвращает 0. За «состоялась» отвечают
  # forget_removed и oldest_snapshot_timestamp ниже.
  echo "restic_server_prune_success{client=\"${CLIENT}\"} ${success}"
  echo "restic_server_prune_timestamp{client=\"${CLIENT}\"} ${ts_end}"
  echo "restic_server_prune_duration_seconds{client=\"${CLIENT}\"} $((ts_end-ts_start))"
  echo "restic_server_repo_bytes{client=\"${CLIENT}\"} ${bytes_after}"
  echo "restic_server_repo_bytes_before{client=\"${CLIENT}\"} ${bytes_before}"
  echo "restic_server_repo_snapshots{client=\"${CLIENT}\"} ${snap_after}"
  echo "restic_server_repo_snapshots_before{client=\"${CLIENT}\"} ${snap_before}"
  echo "restic_server_forget_removed{client=\"${CLIENT}\"} ${removed}"
  echo "restic_server_oldest_snapshot_timestamp{client=\"${CLIENT}\"} ${oldest_ts}"
} > "${METRICS_FILE}.partial" && {
  chmod 0644 "${METRICS_FILE}.partial"
  # node-exporter читает файл целиком: публикуем переименованием, чтобы он не поймал
  # его на середине записи
  cp -f "${METRICS_FILE}.partial" "${METRICS_FILE_ALT}.partial" 2>/dev/null &&
    mv -f "${METRICS_FILE_ALT}.partial" "${METRICS_FILE_ALT}"
  mv -f "${METRICS_FILE}.partial" "${METRICS_FILE}"
}
PRUNE_BODY
  } > "$ps"
  chmod 0755 "$ps"; chown root:root "$ps"
  # cron: prune раз в сутки (час = +3 от 04:00 по умолчанию, разброс по имени)
  local h=$(( ( $(printf '%s' "$name" | cksum | cut -d' ' -f1) % 6 ) + 2 ))
  cat > "/etc/cron.d/kervax-prune-$name" <<CRONEOF
$(( RANDOM % 60 )) $h * * * root $ps >/dev/null 2>&1
CRONEOF
  chmod 0644 "/etc/cron.d/kervax-prune-$name"
}

# Перегенерация скриптов уже заведённых клиентов по текущему шаблону. Retention
# вычитываем из самого скрипта (KEEP=(…) — тот же источник, что читает панель), env
# не трогаем. Прежний скрипт кладём рядом с суффиксом .bak-<дата>, чтобы было к чему
# вернуться, если шаблон окажется хуже прежнего.
cmd_regen_prune() {
  local n=0 f name kl kd kw km
  [ -d "$PRUNE_DIR" ] || { echo "prune-скриптов нет ($PRUNE_DIR)"; return 0; }
  for f in "$PRUNE_DIR"/restic-prune-*.sh; do
    [ -f "$f" ] || continue
    case "$f" in *.bak-*) continue;; esac
    name="${f##*/restic-prune-}"; name="${name%.sh}"
    [ -n "$name" ] || continue
    kl=$(keep_of keep-last "$f"); kd=$(keep_of keep-daily "$f")
    kw=$(keep_of keep-weekly "$f"); km=$(keep_of keep-monthly "$f")
    # keep-last=0 в шаблоне означает «пруна не будет» (см. защиту в теле скрипта):
    # сохраняем как есть, чтобы перегенерация не меняла политику молча
    cp -f "$f" "$f.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    write_prune_script "$name" "$kl" "$kd" "$kw" "$km"
    n=$((n+1))
  done
  echo "перегенерировано prune-скриптов: $n"
}

# ------- развёртывание rest-server с нуля (чистая нода → бэкап-сервер) -------
# Ставим только из штатных реп дистрибутива (никаких curl|sh с чужих доменов).
ensure_pkgs() {
  local need=() p
  command -v docker    >/dev/null 2>&1 || need+=(docker.io)
  docker compose version >/dev/null 2>&1 || need+=(docker-compose-v2)
  command -v htpasswd  >/dev/null 2>&1 || need+=(apache2-utils)
  command -v bunzip2   >/dev/null 2>&1 || need+=(bzip2)
  [ ${#need[@]} -eq 0 ] && return 0
  command -v apt-get >/dev/null 2>&1 || { echo "нужны пакеты (${need[*]}), но apt-get не найден — поставьте вручную" >&2; return 2; }
  DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
  for p in "${need[@]}"; do
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$p" >/dev/null 2>&1 \
      || { echo "не удалось поставить $p" >&2; return 2; }
  done
  systemctl enable --now docker >/dev/null 2>&1 || true
  return 0
}

# restic на бэкап-сервере: системный, иначе качаем с проверкой sha256 (как у клиента)
ensure_restic_srv() {
  [ -x "$RESTIC_BIN" ] && return 0
  local ver="0.18.1" arch f base
  case "$(uname -m)" in x86_64) arch=amd64;; aarch64|arm64) arch=arm64;; *) echo "неизвестная архитектура" >&2; return 2;; esac
  install -d -m 0755 "$(dirname "$KERVAX_RESTIC")"
  base="https://github.com/restic/restic/releases/download/v$ver"; f="restic_${ver}_linux_${arch}.bz2"
  curl -fsSL --connect-timeout 20 "$base/$f" -o "/tmp/$f" || { echo "не скачать restic" >&2; return 2; }
  if curl -fsSL --connect-timeout 20 "$base/SHA256SUMS" -o /tmp/restic-sums 2>/dev/null && [ -s /tmp/restic-sums ]; then
    ( cd /tmp && grep " $f\$" restic-sums | sha256sum -c - >/dev/null 2>&1 ) \
      || { echo "checksum restic не сошёлся" >&2; rm -f "/tmp/$f" /tmp/restic-sums; return 2; }
  fi
  bunzip2 -f "/tmp/$f" || { echo "bunzip2 fail" >&2; return 2; }
  install -m 0755 "/tmp/restic_${ver}_linux_${arch}" "$KERVAX_RESTIC"
  rm -f "/tmp/restic_${ver}_linux_${arch}" /tmp/restic-sums
  RESTIC_BIN="$KERVAX_RESTIC"
}

cmd_deploy_server() {
  local port="${1:-$REST_PORT}"
  valid_num "$port" || { echo "bad port" >&2; return 2; }
  [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || { echo "порт вне диапазона 1024-65535" >&2; return 2; }
  ensure_pkgs || return 2
  ensure_restic_srv || return 2
  # верхние каталоги 0700 (как на существующих серверах): ниже лежат prune-env с паролями
  # репозиториев — не даём даже листать локальным непривилегированным пользователям
  install -d -m 0700 /app/rest-server /app/rest-server/system
  install -d -m 0755 "$PRUNE_DIR" "$ENV_DIR" "$LOG_DIR" "$METRICS_DIR" "$NE_METRICS_DIR"
  install -d -m 0700 "$DATA"
  # htpasswd должен существовать: с --private-repos пустой файл = никто не пускается
  [ -f "$HTPASSWD" ] || { : > "$HTPASSWD"; chown root:root "$HTPASSWD"; chmod 0600 "$HTPASSWD"; }
  # СУЩЕСТВУЮЩИЙ compose НЕ трогаем: там могут быть caddy-лейблы//сети/тюнинг конкретного
  # сервера (общего на несколько проектов). Разворачивание на живом сервере не должно
# ломать его конфиг.
  local existed=0
  if [ -f "$COMPOSE" ]; then
    existed=1
  else
    cat > "$COMPOSE" <<COMPOSEEOF
services:
  rest-server:
    hostname: rest-server
    container_name: rest-server
    image: $REST_IMAGE
    restart: always
    ports:
      - "$port:8000"
    volumes:
      - "$DATA:/data"
    environment:
      OPTIONS: "--append-only --path /data --private-repos"
COMPOSEEOF
    chmod 0644 "$COMPOSE"
  fi
  ( cd /app/rest-server && docker compose up -d ) >/tmp/kv-rs.$$ 2>&1 \
    || { echo "docker compose up failed: $(tr '\n' ' ' </tmp/kv-rs.$$)" >&2; rm -f /tmp/kv-rs.$$; return 2; }
  rm -f /tmp/kv-rs.$$
  # health: с пустым htpasswd rest-server обязан отвечать 401 — значит слушает и требует auth
  local code="" i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$port/" 2>/dev/null || echo 000)"
    [ "$code" != "000" ] && break
    sleep 1
  done
  [ "$code" = "000" ] && { echo "rest-server не отвечает на 127.0.0.1:$port" >&2; return 2; }
  refresh_stats
  local ufw_state="неактивен"
  command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi '^Status: active' && ufw_state="активен"
  if [ "$existed" -eq 1 ]; then
    echo "OK rest-server уже был развёрнут, поднят (порт $port, HTTP $code, ufw $ufw_state)"
  else
    echo "OK rest-server развёрнут: порт $port, append-only+private-repos, HTTP $code, ufw $ufw_state"
  fi
}

# cmd_update_image — обновить образ rest-server до зашитого REST_IMAGE. Данные (репо) в
# bind-mount /data, они НЕ в образе — обновление образа их не трогает. Меняем тег в
# существующем compose, pull, up -d, ждём HTTP 401 (сервер снова слушает). Образ ЗАШИТ
# в helper (панель его не выбирает — иначе вектор подмены).
cmd_update_image() {
  [ -f "$COMPOSE" ] || { echo "rest-server не развёрнут этой панелью (нет $COMPOSE)" >&2; return 2; }
  local port cur
  port="$(rest_port)"
  cur="$(grep -oE 'restic/rest-server:[A-Za-z0-9._-]+' "$COMPOSE" | head -1)"
  # правим ТОЛЬКО строку image, остального compose не касаемся (флаги --append-only и пр.)
  sed -i "s#image:.*restic/rest-server:[A-Za-z0-9._-]*#image: $REST_IMAGE#" "$COMPOSE"
  ( cd /app/rest-server && docker compose pull && docker compose up -d ) >/tmp/kv-rsu.$$ 2>&1 \
    || { echo "обновление образа не удалось: $(tr '\n' ' ' </tmp/kv-rsu.$$ | tail -c 300)" >&2; rm -f /tmp/kv-rsu.$$; return 2; }
  rm -f /tmp/kv-rsu.$$
  # health: rest-server с приватными репо отвечает 401 на «/» — значит слушает
  local code="" i
  for i in $(seq 1 15); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$port/" 2>/dev/null || echo 000)"
    [ "$code" != "000" ] && break
    sleep 1
  done
  if [ "$code" = "000" ]; then
    echo "rest-server не отвечает после обновления (порт $port) — проверьте docker logs rest-server" >&2; return 2
  fi
  local newver cid; cid="$(docker ps -qf 'name=rest-server' 2>/dev/null | head -1)"
  [ -n "$cid" ] && newver="$(docker exec "$cid" rest-server --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  refresh_stats
  echo "OK rest-server обновлён → ${REST_IMAGE#*:} (было ${cur#*:}, сейчас ${newver:-?}), отвечает $code"
}

cmd_provision_client() {
  local name="$1" hpass="$2" repopass="$3" client_ip="$4" kl="${5:-3}" kd="${6:-7}" kw="${7:-4}" km="${8:-6}"
  valid_name "$name" || { echo "bad name" >&2; return 2; }
  valid_ip "$client_ip" || { echo "bad ip" >&2; return 2; }
  for n in "$kl" "$kd" "$kw" "$km"; do valid_num "$n" || { echo "bad retention" >&2; return 2; }; done
  # ЗАЩИТА: каждое keep >= 1 (всегда держим минимум 1 последний/дневной/недельный/месячный,
  # иначе forget/prune сотрёт эти срезы). Пол задаём на самом бэкап-сервере — панель/бэкенд
  # не могут прислать 0 и уничтожить историю.
  [ "$kl" -ge 1 ] 2>/dev/null || kl=1
  [ "$kd" -ge 1 ] 2>/dev/null || kd=1
  [ "$kw" -ge 1 ] 2>/dev/null || kw=1
  [ "$km" -ge 1 ] 2>/dev/null || km=1
  [ -d "$DATA" ] || { echo "no rest-server data dir" >&2; return 2; }
  # 0) ПОВТОРНАЯ НАСТРОЙКА: репо уже существует.
  # `restic init` идемпотентен, но репозиторий НАВСЕГДА остаётся на ПЕРВОМ пароле. Прислать
  # новый нельзя: клиент получит «wrong password», а prune-env станет врать — сломается и
  # ротация, и выдача пароля для восстановления (get-client-creds отдал бы мусор). Поэтому
  # ПОДКЛЮЧАЕМСЯ к существующему репо (история снапшотов сохраняется), а не пересоздаём:
  # достаём его пароль из prune-env и проверяем, что он реально открывает репо.
  # Удаления тут нет и не будет — снос репо остаётся ручной операцией root'а на этом сервере.
  local existing=0 envf="$ENV_DIR/$name.env"
  if [ -f "$DATA/$name/config" ]; then
    existing=1
    local old=""
    [ -f "$envf" ] && old="$(sed -n 's/^RESTIC_PASSWORD="\?\([^"]*\)"\?$/\1/p' "$envf" | head -1)"
    if [ -z "$old" ]; then
      echo "репозиторий '$name' уже существует, но его пароль на сервере не найден ($envf) — репо провижинилось не этой панелью. Возьмите пароль из vault и настройте клиента вручную, либо от root на бэкап-сервере удалите репо: rm -rf $DATA/$name (СОТРЁТ историю)" >&2
      return 2
    fi
    if ! RESTIC_REPOSITORY="$DATA/$name" RESTIC_PASSWORD="$old" "$RESTIC_BIN" cat config >/dev/null 2>&1; then
      echo "репозиторий '$name' существует, но сохранённый на сервере пароль его НЕ открывает ($envf) — нужен верный пароль из vault. Автоматически починить нельзя" >&2
      return 2
    fi
    repopass="$old"
    # retention существующего репо не трогаем — он задаётся при первичной настройке
    local ps="$PRUNE_DIR/restic-prune-$name.sh"
    if [ -f "$ps" ]; then
      local okl okd okw okm
      okl="$(keep_of keep-last "$ps")"; okd="$(keep_of keep-daily "$ps")"
      okw="$(keep_of keep-weekly "$ps")"; okm="$(keep_of keep-monthly "$ps")"
      [ "${okl:-0}" -ge 1 ] 2>/dev/null && kl="$okl"
      [ "${okd:-0}" -ge 1 ] 2>/dev/null && kd="$okd"
      [ "${okw:-0}" -ge 1 ] 2>/dev/null && kw="$okw"
      [ "${okm:-0}" -ge 1 ] 2>/dev/null && km="$okm"
    fi
  fi
  # 1) htpasswd (bcrypt cost 10) — доступ клиента к rest-server (транспортный пароль ротируем
  # свободно: он не про шифрование данных, панель отдаст клиенту новый)
  htpasswd -bB -C 10 "$HTPASSWD" "$name" "$hpass" >/dev/null 2>&1 || { echo "htpasswd failed" >&2; return 2; }
  # 1a) ДОЖДАТЬСЯ, пока rest-server увидит нового пользователя. Он перечитывает htpasswd
  # по изменению файла, и между записью и перечитыванием есть окно: init, стартующий
  # сразу, получает 401 и вся настройка падает (наблюдалось на живой ноде —
  # первый прогон 401, все последующие проходят). Опрашиваем, пока креды не заработают.
  if command -v curl >/dev/null 2>&1; then
    local i code
    for i in 1 2 3 4 5 6 7 8 9 10; do
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
             -u "$name:$hpass" "http://127.0.0.1:$(rest_port)/$name/config" 2>/dev/null || echo 000)
      [ "$code" = "401" ] || break   # 404/403/200 — креды уже приняты
      sleep 1
    done
    [ "$code" = "401" ] && { echo "rest-server не принял нового пользователя за 10с (htpasswd записан, но авторизация не проходит)" >&2; return 2; }
  else
    sleep 2   # без curl проверить нечем — даём серверу время перечитать файл
  fi
  # 2) init репозитория через локальный rest-server (правильные права файлов внутри)
  if [ "$existing" -eq 0 ]; then
    RESTIC_REPOSITORY="rest:http://$name:$hpass@127.0.0.1:$(rest_port)/$name" RESTIC_PASSWORD="$repopass" \
      "$RESTIC_BIN" init >/tmp/kv-init.$$ 2>&1
    local rc=$?
    if [ $rc -ne 0 ] && ! grep -q 'config file already exists\|already initialized' /tmp/kv-init.$$; then
      echo "init failed: $(tr '\n' ' ' </tmp/kv-init.$$)" >&2; rm -f /tmp/kv-init.$$; return 2
    fi
    rm -f /tmp/kv-init.$$
  fi
  # 3) ufw allow IP клиента к rest-порту и tls-порту (если фронт есть) — best-effort
  if command -v ufw >/dev/null 2>&1; then
    ufw allow proto tcp from "$client_ip" to any port "$REST_PORT" >/dev/null 2>&1 || true
    ufw route allow proto tcp from "$client_ip" to any port 8000 >/dev/null 2>&1 || true
    [ -f "$(tls_dir)/cert.pem" ] && ufw allow proto tcp from "$client_ip" to any port "$TLS_PORT" >/dev/null 2>&1 || true
  fi
  # 4) prune env+script+cron с retention (для существующего репо — тем же, ВЕРНЫМ паролем:
  # заодно чинит env, если его когда-то испортила повторная настройка)
  install_prune "$name" "$kl" "$kd" "$kw" "$km" "$repopass"
  refresh_stats
  if [ "$existing" -eq 1 ]; then
    # панель не знает пароль существующего репо — отдаём, иначе клиента не настроить
    printf 'OK existing %s\nREPOPASS_B64=%s\n' "$name" "$(printf '%s' "$repopass" | base64 -w0)"
  else
    echo "OK provisioned $name"
  fi
}

# ------- нативный TLS rest-server на :64101 (БЕЗ лишнего caddy-слоя) -------
# Второй контейнер rest-server с --tls на ТЕХ ЖЕ данных/htpasswd (append-only+private-repos),
# self-signed серт (openssl, 3650 дней — без продления). HTTP :64100 не трогаем → и HTTP, и
# HTTPS работают одновременно. Раньше тут был caddy-фронт — теперь сносим его (миграция).
cmd_deploy_tls_front() {
  local san_ip="$1" san_dns="${2:-}"
  valid_ip "$san_ip" || { echo "bad san ip" >&2; return 2; }
  command -v docker >/dev/null 2>&1 || { echo "no docker" >&2; return 2; }
  install -d -m 0755 "$TLS_DIR"
  # МИГРАЦИЯ раскладки 0.14 (system/kervax-tls → свой каталог). Строго ДО генерации серта:
  # серт переносим, а не выпускаем заново — иначе клиенты с закреплённым --cacert отвалятся.
  if [ -f "$TLS_DIR_OLD/cert.pem" ] || [ -f "$TLS_DIR_OLD/docker-compose.yml" ]; then
    if [ -f "$TLS_DIR_OLD/docker-compose.yml" ]; then
      ( cd "$TLS_DIR_OLD" && docker compose down ) >/dev/null 2>&1 || true
    fi
    docker rm -f kervax-rest-tls >/dev/null 2>&1 || true  # имя занято старым проектом
    for f in cert.pem key.pem; do
      if [ -f "$TLS_DIR_OLD/$f" ] && [ ! -f "$TLS_DIR/$f" ]; then mv -f "$TLS_DIR_OLD/$f" "$TLS_DIR/$f"; fi
    done
    rm -rf "$TLS_DIR_OLD"
  fi
  if [ ! -f "$TLS_DIR/cert.pem" ] || [ ! -f "$TLS_DIR/key.pem" ]; then
    local ext="subjectAltName=IP:$san_ip"; [ -n "$san_dns" ] && ext="$ext,DNS:$san_dns"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" \
      -subj "/CN=${san_dns:-$san_ip}" -addext "$ext" >/dev/null 2>&1 || { echo "cert gen failed" >&2; return 2; }
    chmod 0600 "$TLS_DIR/key.pem"; chmod 0644 "$TLS_DIR/cert.pem"
  fi
  # снести старый caddy-фронт, если был (миграция на нативный TLS rest-server)
  docker rm -f kervax-tls-front >/dev/null 2>&1 || true
  rm -f "$TLS_DIR/Caddyfile" 2>/dev/null || true
  # второй rest-server с нативным TLS на тех же данных; образ ЗАШИТ (как основной, не с панели).
  # entrypoint сам добавит --path /data --htpasswd-file /data/.htpasswd; OPTIONS — остальное.
  cat > "$TLS_DIR/docker-compose.yml" <<COMPOSEEOF
services:
  rest-server-tls:
    image: $REST_IMAGE
    hostname: rest-server-tls
    container_name: kervax-rest-tls
    restart: always
    ports:
      - "$TLS_PORT:8000"
    environment:
      OPTIONS: "--append-only --path /data --private-repos --tls --tls-cert /certs/cert.pem --tls-key /certs/key.pem"
    volumes:
      - "$DATA:/data"
      - "$TLS_DIR/cert.pem:/certs/cert.pem:ro"
      - "$TLS_DIR/key.pem:/certs/key.pem:ro"
COMPOSEEOF
  ( cd "$TLS_DIR" && docker compose up -d ) >/tmp/kv-tls.$$ 2>&1 || { echo "rest-tls up failed: $(tr '\n' ' ' </tmp/kv-tls.$$)" >&2; rm -f /tmp/kv-tls.$$; return 2; }
  rm -f /tmp/kv-tls.$$
  refresh_stats
  echo "OK native TLS rest-server on :$TLS_PORT"
}

# отдаём cert одной строкой base64 (переживает спул tr '\n'; клиент decode'ит в cacert)
cmd_get_cert() { local d; d="$(tls_dir)"; [ -f "$d/cert.pem" ] && base64 -w0 "$d/cert.pem" || { echo "no cert" >&2; return 2; }; }

# DR: пароль репо клиента из prune-env (repopass дублируется тут рядом с бэкапами) —
# на случай, если клиент умер и с него данные уже не достать. base64 одной строкой.
cmd_get_client_creds() {
  local name="$1"
  valid_name "$name" || { echo "bad name" >&2; return 2; }
  local env="$ENV_DIR/$name.env"
  [ -f "$env" ] || { echo "нет prune-env для $name (репо провижинилось не этой панелью?)" >&2; return 2; }
  local pass repo
  pass="$(sed -n 's/^RESTIC_PASSWORD="\?\([^"]*\)"\?$/\1/p' "$env" | head -1)"
  repo="$(sed -n 's/^RESTIC_REPOSITORY="\?\([^"]*\)"\?$/\1/p' "$env" | head -1)"
  printf 'repopass=%s\nrepo_local=%s\n' "$pass" "$repo" | base64 -w0
}

# ------- спул: исполнить запросы провижининга (секреты в 0600, удаляются сразу) -------
cmd_process_spool() {
  local req id action name hpass repopass client_ip kl kd kw km san_ip san_dns port out ok k v line
  for req in "$REQ_DIR"/*.req; do
    [ -f "$req" ] || continue
    id="$(basename "$req" .req)"
    action=""; name=""; hpass=""; repopass=""; client_ip=""; kl=3; kd=7; kw=4; km=6; san_ip=""; san_dns=""; port="$REST_PORT"
    # читаем строку целиком, режем по первому '=' (сохраняет хвостовой '=' в значениях)
    while IFS= read -r line; do
      k="${line%%=*}"; v="${line#*=}"
      case "$k" in
        action) action="$v";; name) name="$v";; hpass) hpass="$v";; repopass) repopass="$v";;
        client_ip) client_ip="$v";; keep_last) kl="$v";; keep_daily) kd="$v";;
        keep_weekly) kw="$v";; keep_monthly) km="$v";; san_ip) san_ip="$v";; san_dns) san_dns="$v";;
        port) port="$v";;
      esac
    done < "$req"
    rm -f "$req"  # секреты на диске не задерживаем
    out=""; ok=false
    case "$action" in
      deploy_server)    if out="$(cmd_deploy_server "$port" 2>&1)"; then ok=true; fi ;;
      update_image)     if out="$(cmd_update_image 2>&1)"; then ok=true; fi ;;
      provision_client) if out="$(cmd_provision_client "$name" "$hpass" "$repopass" "$client_ip" "$kl" "$kd" "$kw" "$km" 2>&1)"; then ok=true; fi ;;
      deploy_tls_front) if out="$(cmd_deploy_tls_front "$san_ip" "$san_dns" 2>&1)"; then ok=true; fi ;;
      get_cert)         if out="$(cmd_get_cert 2>&1)"; then ok=true; fi ;;
      get_client_creds) if out="$(cmd_get_client_creds "$name" 2>&1)"; then ok=true; fi ;;
      *) out="неизвестное действие" ;;
    esac
    printf 'ok=%s\noutput=%s\n' "$ok" "$(printf '%s' "$out" | tr '\n' '\r')" > "$RES_DIR/$id.res.tmp"
    mv -f "$RES_DIR/$id.res.tmp" "$RES_DIR/$id.res"; chmod 0644 "$RES_DIR/$id.res"
  done
}

STATE_DIR=/var/lib/kervax
case "${1:-}" in
  stats)            cmd_stats ;;
  regen-prune)      cmd_regen_prune ;;
  refresh)          refresh_stats ;;
  deploy-server)    shift; cmd_deploy_server "$@" ;;
  update-image)     cmd_update_image ;;
  provision-client) shift; cmd_provision_client "$@" ;;
  deploy-tls-front) shift; cmd_deploy_tls_front "$@" ;;
  get-cert)         cmd_get_cert ;;
  get-client-creds) shift; cmd_get_client_creds "$@" ;;
  process-spool)    cmd_process_spool ;;
  *) echo "usage: $0 {stats|regen-prune|deploy-server [port]|update-image|provision-client <name> <hpass> <repopass> <ip> [kl kd kw km]|deploy-tls-front <ip> [dns]|get-cert|get-client-creds <name>|process-spool}" >&2; exit 2 ;;
esac
HELPER_EOF
chmod 0755 "$HELPER"; chown root:root "$HELPER"

# МИГРАЦИЯ (0.16): чиним уже созданные панелью prune-env'ы без `set -a`. Переустановка
# helper'а сама env'ы не перегенерирует (это делает только повторный провижининг клиента),
# а без экспорта restic в prune-скрипте не видит репо → чистка молча не работает.
# Ansible-managed env'ы (у них свой заголовок) не трогаем — там `set -a` и так есть.
for _f in /app/rest-server/system/envs/*.env; do
  [ -f "$_f" ] || continue
  grep -q '^# generated by kervax' "$_f" || continue
  grep -q '^set -a' "$_f" && continue
  if awk 'NR==1{print; print "set -a"; next} {print} END{print "set +a"}' "$_f" > "$_f.kvtmp"; then
    chmod 0600 "$_f.kvtmp"; chown root:root "$_f.kvtmp"; mv -f "$_f.kvtmp" "$_f"
    echo "backupserver-setup: починен prune-env $_f (не экспортировался)"
  else
    rm -f "$_f.kvtmp"
  fi
done

# первый прогон сразу (чтобы файл появился) + cron раз в минуту (stats дёшев)
"$HELPER" stats > "$STATS.tmp" 2>/dev/null && mv -f "$STATS.tmp" "$STATS" || true
chmod 0644 "$STATS" 2>/dev/null || true
cat > "$CRON" <<CRON_EOF
* * * * * root $HELPER stats > $STATS.tmp 2>/dev/null && mv -f $STATS.tmp $STATS && chmod 0644 $STATS
CRON_EOF
chmod 0644 "$CRON"

# path-unit: как только агент кладёт запрос в спул — root исполняет его (мгновенно)
cat > /etc/systemd/system/kervax-bsrv-req.service <<UNIT_EOF
[Unit]
Description=Kervax backup-server request processor
[Service]
Type=oneshot
ExecStart=$HELPER process-spool
UNIT_EOF
cat > /etc/systemd/system/kervax-bsrv-req.path <<UNIT_EOF
[Unit]
Description=Kervax backup-server request spool watch
[Path]
DirectoryNotEmpty=$REQ_DIR
Unit=kervax-bsrv-req.service
[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload 2>/dev/null || true
systemctl enable --now kervax-bsrv-req.path >/dev/null 2>&1 || true
"$HELPER" process-spool >/dev/null 2>&1 || true

# Скрипты уже заведённых клиентов — копии прежнего шаблона: обновляем их сразу,
# иначе исправления (метрики ротации, --group-by) достанутся только новым.
"$HELPER" regen-prune 2>/dev/null || true

echo "backupserver-setup: готово → $HELPER; статистика в $STATS (cron 1/мин), провижининг через спул $REQ_DIR."
echo "backupserver-setup: текущая статистика:"
head -c 500 "$STATS" 2>/dev/null; echo
