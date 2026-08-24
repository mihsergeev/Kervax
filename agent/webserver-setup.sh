#!/usr/bin/env bash
# Kervax: домены, которые обслуживает хостовый/контейнерный веб-сервер (nginx, apache) —
# для раздела «Сервисы». Неприв. агент (kervax) конфиги не читает и docker-exec запрещён
# агентской проксёй, поэтому root-хелпер по таймеру дампит `nginx -T` (+ `docker exec` в
# nginx-контейнеры напрямую, минуя проксю; + apache -S; + Host()-метки Traefik) → пишет
# /var/lib/kervax/web-sites.json. Агент ТОЛЬКО ЧИТАЕТ этот файл. Ставит ansible-плейбук.
# Только домены маршрутов (server_name / namevhost), без секретов и содержимого конфигов.
set -euo pipefail

KERVAX_SETUP_VERSION=0.4  # МАЖОР.МИНОР; сравнивается покомпонентно
KERVAX_SETUP_ALWAYS=1     # безопасно на любой ноде: refresh no-op'ит без веб-сервера

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-web-sites"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/web-sites.json"

if [ "$(id -u)" != 0 ]; then echo "Нужен root." >&2; exit 1; fi

# Родителя /var/lib/kervax задаём 0755 ЯВНО (иначе под активным umask 077 неприв. агент в
# него не зайдёт и файл не прочитает — тот самый баг kube-setup).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions"

# ── refresh-скрипт (root, по таймеру). Кавычки '…' — тело литеральное, без подстановок. ──
cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Собирает server_name с хостового и контейнерного nginx + namevhost apache → web-sites.json.
set -u
OUT=/var/lib/kervax/web-sites.json
TMP="$OUT.tmp.$$"

# server_name из nginx -T: выкидываем _/localhost/мусор, регекс сводим к читаемому виду.
# ВАЖНО: кавычки снимаем ДО проверок — регекс часто пишут именно так:
#   server_name "~^(?<sub>.+)\.trafflow\.tech$";
# из-за этого проверка «первый символ ~» его пропускала, обратные слэши уезжали в JSON,
# и агент отбрасывал ВЕСЬ файл (так терялись все 50+ доменов ноды).
extract_nginx() {
  awk '/^[[:space:]]*server_name/ {
    for (i=2;i<=NF;i++){ g=$i; sub(/;$/,"",g);
      gsub(/^["\047]+|["\047]+$/,"",g);           # снять кавычки (\047 = апостроф)
      if (g=="" || g=="_" || g=="localhost") continue;
      if (g ~ /^[~^]/) {                          # регекс: сводим к *.domain.tld
        r=g; sub(/^~/,"",r); sub(/^\^/,"",r); sub(/\$$/,"",r);
        gsub(/\([^)]*\\\.\)\?/,"",r);             # необязательный префикс (www\.)? — просто убираем
        gsub(/\(\?<[A-Za-z0-9_]+>[^)]*\)/,"*",r); # (?<sub>.+) → *
        gsub(/\([^)]*\)\??/,"*",r);               # прочие группы → *
        gsub(/\\\./,".",r);                       # \. → .
        gsub(/\.\+|\.\*/,"*",r);
        g=r }
      if (g !~ /[A-Za-z]/) continue;              # не домен
      if (g !~ /^[A-Za-z0-9.*_-]+$/) continue;    # только домен/wildcard: JSON не сломается
      print g }
  }'
}

collect_nginx() {
  command -v nginx >/dev/null 2>&1 && nginx -T 2>/dev/null | extract_nginx
  if command -v docker >/dev/null 2>&1; then
    # nginx-контейнеры (root дампит напрямую docker exec, минуя агентскую проксю)
    docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null \
      | awk '/nginx/{print $1}' | sort -u | while read -r c; do
        [ -n "$c" ] && docker exec "$c" nginx -T 2>/dev/null | extract_nginx
      done
  fi
}

collect_apache() {
  local a
  for a in apache2ctl apachectl httpd; do
    if command -v "$a" >/dev/null 2>&1; then
      "$a" -S 2>/dev/null | grep -oiE 'namevhost [^ ]+' | awk '{print $2}' \
        | grep -viE '^(localhost|\*|_default_)$'
      break
    fi
  done
}

# Caddy: у caddy-docker-proxy домены — в метке `caddy` контейнеров (значение = список
# адресов сайтов). Плюс хостовый Caddyfile (адреса сайтов до `{`). Чистим схему/порт.
extract_domains() { tr ' ,' '\n' | sed -E 's~^https?://~~; s~:[0-9]+$~~; s~/.*$~~' | grep -E '^\*?[A-Za-z0-9._-]+\.[A-Za-z]{2,}$'; }
collect_caddy() {
  if command -v docker >/dev/null 2>&1; then
    for cid in $(docker ps -q 2>/dev/null); do
      docker inspect --format '{{index .Config.Labels "caddy"}}' "$cid" 2>/dev/null
    done | extract_domains
  fi
  [ -f /etc/caddy/Caddyfile ] && grep -vE '^[[:space:]]*#' /etc/caddy/Caddyfile \
    | grep -oiE '(^|[[:space:]])\*?[A-Za-z0-9._-]+\.[A-Za-z]{2,}[[:space:]]*\{' | extract_domains
}

# Traefik: домены живут в docker-метках роутеров —
#   traefik.http.routers.<r>.rule = Host(`a.tld`) || Host(`b.tld`)
# (провайдер docker; тот же принцип, что у caddy-docker-proxy). HostSNI — TCP-роутеры.
collect_traefik() {
  command -v docker >/dev/null 2>&1 || return 0
  for cid in $(docker ps -q 2>/dev/null); do
    docker inspect --format '{{json .Config.Labels}}' "$cid" 2>/dev/null
  done | grep -oE 'Host(SNI)?\(`[^)]*\)' | grep -oE '`[^`]+`' | tr -d '`' | extract_domains
}

NGINX=$(collect_nginx | sort -u)
APACHE=$(collect_apache | sort -u)
CADDY=$(collect_caddy | sort -u)
TRAEFIK=$(collect_traefik | sort -u)

# JSON без jq: экранируем кавычки на всякий случай, склеиваем массив
json_arr() { awk 'BEGIN{printf "["} {gsub(/[\\"]/,""); printf "%s\"%s\"", (NR>1?",":""), $0} END{printf "]"}'; }
put() { [ $c -eq 1 ] && printf ','; printf '"%s":' "$1"; printf '%s\n' "$2" | json_arr; c=1; }
{
  printf '{'
  c=0
  [ -n "$NGINX" ]  && put nginx  "$NGINX"
  [ -n "$APACHE" ] && put Apache "$APACHE"
  [ -n "$CADDY" ]  && put Caddy  "$CADDY"
  [ -n "$TRAEFIK" ] && put Traefik "$TRAEFIK"
  printf '}\n'
} > "$TMP"
mv -f "$TMP" "$OUT"
chmod 0644 "$OUT"
HELPER_EOF
chmod 0755 "$HELPER"

# ── systemd: oneshot + таймер (при старте и каждые 15 мин) ──
cat > /etc/systemd/system/kervax-web-sites.service <<EOF
[Unit]
Description=Kervax: собрать домены веб-серверов (server_name)
[Service]
Type=oneshot
ExecStart=$HELPER
EOF
cat > /etc/systemd/system/kervax-web-sites.timer <<'EOF'
[Unit]
Description=Kervax: периодически обновлять домены веб-серверов
[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-web-sites.timer >/dev/null 2>&1 || true
"$HELPER" || true   # первый прогон сразу, чтобы данные появились без ожидания таймера

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/webserver-setup.ver"
chmod 0644 "$STATE_DIR/versions/webserver-setup.ver"
echo "✓ webserver-setup: домены веб-серверов → $OUT (обновление при старте и каждые 15 мин)."
