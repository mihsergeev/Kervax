#!/usr/bin/env bash
# Kervax: the domains served by host and container web servers (nginx, apache) for the
# Services section. The unprivileged agent (kervax) cannot read configs, and docker exec is
# blocked by its own proxy, so a root helper dumps `nginx -T` on a timer (plus `docker exec`
# straight into nginx containers, bypassing the proxy; plus apache -S; plus Traefik Host()
# labels) and writes /var/lib/kervax/web-sites.json. The agent ONLY READS that file.
# Installed by the ansible playbook. Route domains only (server_name / namevhost), without
# secrets or config contents.
set -euo pipefail

KERVAX_SETUP_VERSION=0.10  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: the refresh is a no-op without a web server

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-web-sites"
RATE="$HELPER_DIR/kervax-web-rate"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/web-sites.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (otherwise, under an active umask
# 077, the unprivileged agent cannot enter it and never reads the file — the very bug from
# kube-setup).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# -- refresh script (root, on a timer). Single quotes keep the body literal, no expansion. --
cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Collects server_name from host and container nginx plus apache namevhost -> web-sites.json.
set -u
OUT=/var/lib/kervax/web-sites.json
TMP="$OUT.tmp.$$"

# server_name from nginx -T: drop _/localhost/garbage and reduce regexes to a readable form.
# IMPORTANT: quotes are stripped BEFORE the checks — regexes are often written exactly like
# this:
#   server_name "~^(?<sub>.+)\.trafflow\.tech$";
# so the "first character is ~" test missed it, backslashes leaked into the JSON and the
# agent discarded the WHOLE file (that is how all 50+ domains of a node were lost).
extract_nginx() {
  awk '/^[[:space:]]*server_name/ {
    for (i=2;i<=NF;i++){ g=$i; sub(/;$/,"",g);
      gsub(/^["\047]+|["\047]+$/,"",g);           # strip quotes (\047 = apostrophe)
      if (g=="" || g=="_" || g=="localhost") continue;
      if (g ~ /^[~^]/) {                          # regex: reduce to *.domain.tld
        r=g; sub(/^~/,"",r); sub(/^\^/,"",r); sub(/\$$/,"",r);
        gsub(/\([^)]*\\\.\)\?/,"",r);             # optional prefix (www\.)? is simply removed
        gsub(/\(\?<[A-Za-z0-9_]+>[^)]*\)/,"*",r); # (?<sub>.+) → *
        gsub(/\([^)]*\)\??/,"*",r);               # other groups become *
        gsub(/\\\./,".",r);                       # \. → .
        gsub(/\.\+|\.\*/,"*",r);
        g=r }
      if (g !~ /[A-Za-z]/) continue;              # not a domain
      if (g !~ /^[A-Za-z0-9.*_-]+$/) continue;    # domain or wildcard only: keeps the JSON valid
      print g }
  }'
}

collect_nginx() {
  command -v nginx >/dev/null 2>&1 && nginx -T 2>/dev/null | extract_nginx
  if command -v docker >/dev/null 2>&1; then
    # nginx containers (root dumps them via docker exec directly, bypassing the agent proxy)
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

# Caddy: with caddy-docker-proxy the domains live in the containers' `caddy` label (its
# value is a list of site addresses). Plus a host Caddyfile (site addresses before `{`).
# The scheme and port are stripped.
extract_domains() { tr ' ,' '\n' | sed -E 's~^https?://~~; s~:[0-9]+$~~; s~/.*$~~' | grep -E '^\*?[A-Za-z0-9._-]+\.[A-Za-z]{2,}$'; }
# Контейнеры, чьи метки считаем: работающие, только что созданные и перезапускающиеся.
# Только работающих мало. Compose при деплое сначала создаёт новый контейнер и лишь
# потом запускает его; сбор, попавший в эти секунды, видел старый уже остановленным, а
# новый ещё не запущенным — и все домены с его меток выпадали из списка до следующего
# сбора через 15 минут. Мониторы этих сайтов теряли ноду, и приходил алерт «агент не
# присылает результат» на живые сайты. Повтор --filter status — это ИЛИ.
label_containers() {
  docker ps -q --filter status=running --filter status=created --filter status=restarting 2>/dev/null
}

collect_caddy() {
  if command -v docker >/dev/null 2>&1; then
    for cid in $(label_containers); do
      # Адрес сайта живёт в метке `caddy` — ЛИБО в пронумерованных `caddy_0`,
      # `caddy_1`… когда один контейнер обслуживает несколько сайтов. Раньше читалась
      # только первая форма, и нода с метками caddy_0 выглядела как сервер вообще без
      # доменов: веб-сервер найден, сайтов ноль. Ключи с точкой (caddy_0.reverse_proxy
      # и прочие директивы) пропускаем — там не адреса.
      docker inspect --format '{{json .Config.Labels}}' "$cid" 2>/dev/null \
        | tr ',' '\n' \
        | sed -n 's/.*"caddy\(_[0-9][0-9]*\)\{0,1\}"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\2/p'
    done | extract_domains
  fi
  [ -f /etc/caddy/Caddyfile ] && grep -vE '^[[:space:]]*#' /etc/caddy/Caddyfile \
    | grep -oiE '(^|[[:space:]])\*?[A-Za-z0-9._-]+\.[A-Za-z]{2,}[[:space:]]*\{' | extract_domains
}

# Traefik: domains live in the routers' docker labels —
#   traefik.http.routers.<r>.rule = Host(`a.tld`) || Host(`b.tld`)
# (the docker provider; same idea as caddy-docker-proxy). HostSNI covers TCP routers.
collect_traefik() {
  command -v docker >/dev/null 2>&1 || return 0
  for cid in $(label_containers); do
    docker inspect --format '{{json .Config.Labels}}' "$cid" 2>/dev/null
  done | grep -oE 'Host(SNI)?\(`[^)]*\)' | grep -oE '`[^`]+`' | tr -d '`' | extract_domains
}

# ── access-логи: какой лог какие домены обслуживает ───────────────────────────
# Нужно для счёта запросов в минуту (его делает kervax-web-rate раз в минуту). Здесь
# только карта «файл лога -> домены», её кладём в web-logs.tsv рядом: файл служебный,
# агент читает не его, а готовый web-rate.json.
#
# Разбор дампа nginx -T: у каждого server-блока запоминаем его access_log (или
# унаследованный с уровня http) и server_name. Вложенные location со своим логом не
# трогаем - считаем виртуальный хост целиком.
extract_logs() {
  awk '
    # лог печатаем и без доменов: общий access.log ловит всё, что не разложено по
    # виртуальным хостам, и его поток тоже надо видеть
    function flush() { if (slog!="") print slog "\t" names }
    /^[[:space:]]*server[[:space:]]*\{/ && !insrv { insrv=1; depth=1; names=""; slog=hlog; next }
    !insrv && $1=="access_log" { p=$2; sub(/;$/,"",p); if (p!="off") hlog=p; next }
    insrv {
      o=gsub(/\{/,"{"); c=gsub(/\}/,"}"); depth += o-c
      if ($1=="server_name") {
        for (i=2;i<=NF;i++) { g=$i; sub(/;$/,"",g); gsub(/^["\047]+|["\047]+$/,"",g);
          if (g=="" || g=="_" || g=="localhost") continue;
          if (g ~ /^[~^]/) {                          # regexp сводим к *.domain.tld, как в доменах
            r=g; sub(/^~/,"",r); sub(/^\^/,"",r); sub(/\$$/,"",r);
            gsub(/\([^)]*\\\.\)\?/,"",r);
            gsub(/\(\?<[A-Za-z0-9_]+>[^)]*\)/,"*",r);
            gsub(/\([^)]*\)\??/,"*",r);
            gsub(/\\\./,".",r);
            gsub(/\.\+|\.\*/,"*",r);
            g=r }
          if (g !~ /[A-Za-z]/) continue;
          if (g !~ /^[A-Za-z0-9.*_-]+$/) continue;
          names = names (names==""?"":" ") g }
      } else if ($1=="access_log" && depth==1) { p=$2; sub(/;$/,"",p); slog=(p=="off"?"":p) }
      if (depth<=0) { flush(); insrv=0 }
    }
  '
}

# Один лог - одна строка: иначе десяток виртуальных хостов с общим логом дал бы десяток
# строк, и счётчик сложил бы один и тот же поток столько же раз. Третья колонка - имя для
# показа (у логов подов домены неизвестны, и «0.log» в панели ни о чём не говорит).
# Переменная lg, а не log: log - встроенная функция awk (логарифм), и awk на такой
# переменной падает синтаксической ошибкой. Карта уезжала пустой, а с ней молчал счётчик.
merge_logs() {
  awk -F'\t' '
    { lg=$1; if (!(lg in seen_log)) { seen_log[lg]=1; a[lg]=""; nm[lg]=$3 }
      if (nm[lg]=="" && $3!="") nm[lg]=$3
      n=split($2, w, " ")
      for (i=1;i<=n;i++) if (w[i]!="" && !((lg SUBSEP w[i]) in seen)) {
        seen[lg SUBSEP w[i]]=1; a[lg]=a[lg] (a[lg]==""?"":" ") w[i] } }
    END { for (k in a) printf "%s\t%s\t%s\n", k, a[k], nm[k] }'
}

# Путь лога внутри контейнера -> путь на хосте по его bind-mount'ам.
host_path() {
  awk -F'|' -v p="$1" '
    { d=$1; s=$2; if (d=="" || s=="") next;
      if (p==d) { print s; exit }
      if (substr(p,1,length(d)+1)==d"/") { print s substr(p,length(d)+1); exit } }'
}

# Логи одного nginx-контейнера в хостовых путях. Два случая:
#  * лог смонтирован с хоста - берём его по bind-mount'ам;
#  * лог уходит в stdout (в образе nginx access.log это симлинк на /dev/stdout) - тогда
#    строки лежат в json-файле докера, и путь к нему знает сам докер (.LogPath). Драйвер
#    обязан быть json-file: у local формат бинарный, строки в нём не посчитать.
# В json-файл попадает и stderr, то есть редкие строки ошибок nginx тоже считаются.
container_logs() {
  c="$1"
  mounts=$(docker inspect --format '{{range .Mounts}}{{.Destination}}|{{.Source}}
{{end}}' "$c" 2>/dev/null)
  logpath=$(docker inspect --format '{{.LogPath}}' "$c" 2>/dev/null)
  driver=$(docker inspect --format '{{.HostConfig.LogConfig.Type}}' "$c" 2>/dev/null)
  docker exec "$c" nginx -T 2>/dev/null | extract_logs | while IFS="$(printf '\t')" read -r lg names; do
    real=$(docker exec "$c" sh -c 'readlink -f "$1" 2>/dev/null' _ "$lg" 2>/dev/null)
    [ -n "$real" ] || real="$lg"
    case "$real" in
      /dev/*|/proc/*)
        case "$driver" in
          json-file|"")
            [ -n "$logpath" ] && [ -f "$logpath" ] && printf '%s\t%s\n' "$logpath" "$names"
            ;;
        esac
        ;;
      *)
        hp=$(printf '%s\n' "$mounts" | host_path "$real")
        [ -n "$hp" ] && printf '%s\t%s\n' "$hp" "$names"
        ;;
    esac
  done
}

# Логи подов kubernetes: containerd кладёт их в
# /var/log/pods/<ns>_<под>_<uid>/<контейнер>/0.log, докера на такой ноде нет вовсе.
# Берём поды, у которых nginx в имени пода или контейнера: у ingress-nginx контейнер
# называется controller, у обычных - nginx. Домены отсюда не узнать (они в Ingress, а
# хелпер в кластер не ходит), поэтому третьей колонкой пишем ns/под - его и покажет панель.
collect_pod_logs() {
  [ -d /var/log/pods ] || return 0
  for f in /var/log/pods/*/*/0.log; do
    [ -f "$f" ] || continue
    rest=${f#/var/log/pods/}
    poddir=${rest%%/*}
    cont=${rest#*/}; cont=${cont%%/*}
    case "$poddir/$cont" in
      *nginx*) ;;
      *) continue ;;
    esac
    ns=${poddir%%_*}
    pod=${poddir#*_}; pod=${pod%_*}
    printf '%s\t\t%s/%s\n' "$f" "$ns" "$pod"
  done
}

collect_logs() {
  command -v nginx >/dev/null 2>&1 && nginx -T 2>/dev/null | extract_logs
  collect_pod_logs
  command -v docker >/dev/null 2>&1 || return 0
  docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | awk '/nginx/{print $1}' | sort -u \
    | while read -r c; do
        [ -n "$c" ] && container_logs "$c"
      done
}

LOGTMP="/var/lib/kervax/web-logs.tsv.tmp.$$"
collect_logs | merge_logs | sort > "$LOGTMP" 2>/dev/null || : > "$LOGTMP"
mv -f "$LOGTMP" /var/lib/kervax/web-logs.tsv
chmod 0644 /var/lib/kervax/web-logs.tsv

NGINX=$(collect_nginx | sort -u)
APACHE=$(collect_apache | sort -u)
CADDY=$(collect_caddy | sort -u)
TRAEFIK=$(collect_traefik | sort -u)

# JSON without jq: escape quotes just in case and join the array
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

# -- счёт запросов в минуту (root, раз в минуту) --
cat > "$RATE" <<'RATE_EOF'
#!/usr/bin/env bash
# Сколько строк добавилось в каждый access-лог с прошлого запуска -> запросов в минуту.
# Карту «лог -> домены» пишет сборщик доменов (web-logs.tsv), сами логи читаем только на
# длину: в web-rate.json уходят числа и имена доменов, ни одной строки лога.
set -u
# Кладём в report.d: агент отдаёт этот каталог панели как есть, своей версии ему для
# новых блоков не нужно.
OUT=/var/lib/kervax/report.d/web-rate.json
MAP=/var/lib/kervax/web-logs.tsv
STATE=/var/lib/kervax/web-rate.state
TMP="$OUT.tmp.$$"
NEW="$STATE.tmp.$$"
CAP=$((64 * 1024 * 1024))     # больше за раз не вычитываем
SAMPLE=$((8 * 1024 * 1024))   # на таком куске оцениваем среднюю длину строки
TAB=$(printf '\t')

now=$(date +%s)
: > "$NEW"
ITEMS=""
TOTAL=0
T5=0

esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\000-\037'; }
sites_json() { tr ' ' '\n' | awk 'BEGIN{printf "["} {gsub(/[\\"]/,""); if($0=="")next; printf "%s\"%s\"", (n++?",":""), $0} END{printf "]"}'; }

# Строки -> «всего 5xx 4xx». Код ответа ищем тремя способами, потому что формат лога
# у всех свой: после кавычки с запросом (combined, в том числе внутри docker-json, где
# кавычка экранирована), полем через табуляцию (так пишет ingress-nginx) и как
# "status":503 в json-логах. Не нашли - строка считается только в «всего».
count_codes() {
  awk '
    { n++
      s=""
      if (match($0, /\\?"[ \t]+[1-5][0-9][0-9]([ \t]|$)/)) { s=substr($0,RSTART,RLENGTH); gsub(/[^0-9]/,"",s) }
      else if (match($0, /"status"[ \t]*:[ \t]*"?[1-5][0-9][0-9]/)) { s=substr($0,RSTART+RLENGTH-3,3) }
      else if (match($0, /\t[1-5][0-9][0-9]\t/)) { s=substr($0,RSTART+1,3) }
      if (s=="") next
      c=s+0
      if (c>=500) e5++
      else if (c>=400) e4++ }
    END { printf "%d %d %d\n", n+0, e5+0, e4+0 }'
}

if [ -s "$MAP" ]; then
  # Колонки режем руками: read с IFS=TAB схлопывает подряд идущие табы (таб для
  # оболочки - пробельный разделитель), и у логов подов, где вторая колонка пустая,
  # имя уезжало в домены.
  while IFS= read -r line; do
    lg=${line%%"$TAB"*}
    rest=${line#*"$TAB"}
    [ "$rest" = "$line" ] && rest=""
    names=${rest%%"$TAB"*}
    name=${rest#*"$TAB"}
    [ "$name" = "$rest" ] && name=""
    [ -n "$lg" ] && [ -f "$lg" ] || continue
    ino=$(stat -c %i "$lg" 2>/dev/null) || continue
    size=$(stat -c %s "$lg" 2>/dev/null) || continue
    prev=$(awk -F"$TAB" -v p="$lg" '$1==p{print $2, $3, $4; exit}' "$STATE" 2>/dev/null)
    printf '%s\t%s\t%s\t%s\n' "$lg" "$ino" "$size" "$now" >> "$NEW"
    # shellcheck disable=SC2086
    set -- $prev
    pino="${1:-}"; psize="${2:-0}"; pts="${3:-0}"
    # Первый запуск, ротация (сменился inode) или лог обрезали - точки отсчёта нет,
    # в этот раз про него молчим, посчитаем со следующего запуска.
    [ -n "$pino" ] && [ "$pino" = "$ino" ] && [ "$size" -ge "$psize" ] || continue
    el=$((now - pts))
    [ "$el" -ge 20 ] || continue
    delta=$((size - psize))
    counts="0 0 0"
    if [ "$delta" -gt 0 ]; then
      if [ "$delta" -le "$CAP" ]; then
        counts=$(tail -c "+$((psize + 1))" "$lg" 2>/dev/null | count_codes)
      else
        # слишком много за раз: считаем кусок и масштабируем - и строки, и ошибки
        k=$((delta / SAMPLE + 1))
        counts=$(tail -c "+$((psize + 1))" "$lg" 2>/dev/null | head -c "$SAMPLE" \
                 | count_codes | awk -v k="$k" '{printf "%d %d %d\n", $1*k, $2*k, $3*k}')
      fi
    fi
    lines=${counts%% *}; tail2=${counts#* }; e5=${tail2%% *}; e4=${tail2##* }
    rpm=$((lines * 60 / el))
    r5=$((e5 * 60 / el))
    r4=$((e4 * 60 / el))
    TOTAL=$((TOTAL + rpm))
    T5=$((T5 + r5))
    ITEMS="$ITEMS${ITEMS:+,}{\"log\":\"$(esc "$lg")\",\"name\":\"$(esc "$name")\",\"rpm\":$rpm,\"e5\":$r5,\"e4\":$r4,\"sites\":$(printf '%s' "$names" | sites_json)}"
  done < "$MAP"
fi

mv -f "$NEW" "$STATE"
chmod 0600 "$STATE"
# Ни одного посчитанного лога - блок не пишем вовсе (и убираем старый). Пустой блок
# означал бы "запросов ноль", хотя правда в другом: логи уехали в stdout контейнера
# либо nginx тут вообще не пишет их в файлы.
if [ -z "$ITEMS" ]; then
  rm -f "$OUT"
  exit 0
fi
printf '{"ts":%s,"rpm":%s,"e5":%s,"logs":[%s]}\n' "$now" "$TOTAL" "$T5" "$ITEMS" > "$TMP"
mv -f "$TMP" "$OUT"
chmod 0644 "$OUT"
RATE_EOF
chmod 0755 "$RATE"

cat > /etc/systemd/system/kervax-web-rate.service <<EOF
[Unit]
Description=Kervax: count web server requests per minute
[Service]
Type=oneshot
ExecStart=$RATE
EOF
cat > /etc/systemd/system/kervax-web-rate.timer <<'EOF'
[Unit]
Description=Kervax: count web server requests every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=5s
[Install]
WantedBy=timers.target
EOF

# -- systemd: a oneshot unit plus a timer (at boot and every 15 minutes) --
cat > /etc/systemd/system/kervax-web-sites.service <<EOF
[Unit]
Description=Kervax: collect web server domains (server_name)
[Service]
Type=oneshot
ExecStart=$HELPER
EOF
cat > /etc/systemd/system/kervax-web-sites.timer <<'EOF'
[Unit]
Description=Kervax: refresh web server domains periodically
[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-web-sites.timer >/dev/null 2>&1 || true
systemctl enable --now kervax-web-rate.timer >/dev/null 2>&1 || true
"$HELPER" || true   # run once immediately so the data appears without waiting for the timer
"$RATE" || true     # первый запуск только запоминает позиции в логах, счёт со второго

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/webserver-setup.ver"
chmod 0644 "$STATE_DIR/versions/webserver-setup.ver"
echo "✓ webserver-setup: web server domains -> $OUT (refreshed at boot and every 15 minutes),"
echo "  requests per minute -> /var/lib/kervax/report.d/web-rate.json (every minute)."
