#!/usr/bin/env bash
# Kervax: GitLab (the omnibus image in Docker). A self-hosted GitLab falls behind quietly: it keeps
# working on a version with known holes, and the way up gets longer with every required stop
# (upgrade path) it misses. Its own backup is easy to break without noticing too: a cron line with
# SKIP=repositories makes an archive without the code, and without gitlab-secrets.json the
# database from that archive cannot be read at all.
#
# A root helper finds every GitLab container on the node (a mount on /var/opt/gitlab, or the image
# gitlab/gitlab-ce|ee) and every 15 minutes writes /var/lib/kervax/report.d/gitlab.json; the agent
# hands report.d to the panel as is. For each instance: the version from the VERSION file inside
# the container, the edition, external_url, how it is installed (an image tag in compose, or a
# Dockerfile built FROM gitlab/gitlab-*), the compose directory and the host paths of its data and
# config. Backups: where the *_gitlab_backup.tar files are (backup_path of gitlab.rb on the host),
# how many, the newest one with its size and the components it skipped (read from
# backup_information.yml at the head of the archive, the tar is not unpacked), which cron lines and
# systemd timers make them and with what SKIP, and the newest archive of the config
# (gitlab-ctl backup-etc or a tar of the config directory). Batched background migrations by
# status from the database of the instance (gitlab-psql in the container): a failed one blocks the
# next upgrade. If gitlab-autoupgrade is installed: its timer, the last result, a blocked version,
# the "broken" mark and since when an update has been waiting, from its state files. The versions
# GitLab has released are fetched by the panel itself, once for every node.
#
# Read only: docker inspect, cat and a select in the container, the cron and systemd files, the
# names, sizes and dates of the backup files. Nothing of the environment of the container is sent
# but external_url; of a cron line only its schedule and SKIP.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
# the panel wants this helper for a compose service named gitlab on an image of its own too
# (setup_needed); with only the grep the playbook skipped such a node, and it stayed in kervax_outdated
KERVAX_SETUP_WHEN="command -v docker >/dev/null 2>&1 && (docker ps -a 2>/dev/null | grep -qi -e gitlab-ce -e gitlab-ee || docker ps -aq --filter label=com.docker.compose.service=gitlab 2>/dev/null | grep -q .)"

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-gitlab"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/gitlab.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes,
# and rewriting it in place would make a run in progress continue from the same byte offset.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# GitLab containers -> /var/lib/kervax/report.d/gitlab.json. Read only.
set -u
# UTF-8 when the node has it: the reasons gitlab-autoupgrade writes are in Russian, and ${s:0:N}
# must not cut a letter in half (an invalid string makes the agent drop the whole block)
if locale -a 2>/dev/null | grep -qix 'c.utf-\?8'; then export LC_ALL=C.UTF-8; else export LC_ALL=C; fi
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
OUT=${KERVAX_GITLAB_OUT:-/var/lib/kervax/report.d/gitlab.json}
AU_SCRIPT=${KERVAX_GITLAB_AU:-/lib65/scripts/gitlab-autoupgrade.sh}
AU_DIR=${KERVAX_GITLAB_AU_DIR:-/lib65/gitlab-autoupgrade}
CRON_FILES=${KERVAX_GITLAB_CRON:-"/etc/crontab /etc/cron.d/* /var/spool/cron/crontabs/* /var/spool/cron/*"}

exec 9>/run/kervax-gitlab.lock
flock -n 9 || exit 0
NOW=$(date +%s)

write() { # $1 - the instances array
  mkdir -p "${OUT%/*}"
  printf '{"v":1,"ts":%s,"instances":[%s]}\n' "$NOW" "$1" > "$OUT.tmp.$$" && chmod 0644 "$OUT.tmp.$$" && mv -f "$OUT.tmp.$$" "$OUT"
}

command -v docker >/dev/null 2>&1 || { write ""; exit 0; }
IDS=$(timeout 20 docker ps -a -q 2>/dev/null) || IDS=""
[ -n "$IDS" ] || { write ""; exit 0; }

# a JSON string: control characters dropped, backslash and quote escaped, at most $2 characters
js() {
  local s
  s=$(printf '%s' "$1" | tr -d '\000-\037\177')
  s=${s:0:${2:-300}}
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}
num() { case "$1" in ''|*[!0-9]*) printf 0 ;; *) printf '%s' "$1" ;; esac; }
# a path is sent and used only from the plain set of characters
plain() { case "$1" in ''|*..*|*[!A-Za-z0-9._/@+-]*) return 1 ;; esac; }
# systemd prints times in local time with a zone abbreviation that date(1) may not know (MSK)
sd_ts() {
  local v=$1
  case "$v" in ''|n/a|0) printf 0; return ;; esac
  case "${v##* }" in *[!A-Za-z]*) ;; *) v=${v% *} ;; esac
  date -d "$v" +%s 2>/dev/null || printf 0
}

# cron lines and systemd services that may make GitLab backups: "src<TAB>when<TAB>text", text is
# the command and, when it runs a script, the backup lines of that script
BK_RE='gitlab-backup[[:space:]]+create|gitlab:backup:create|gitlab-ctl[[:space:]]+backup-etc|gitlab-rake[[:space:]]+gitlab:backup'
script_lines() { # the backup lines of the scripts a command runs
  local w
  for w in $1; do
    w=${w%%[;|&]*}
    case "$w" in /*) ;; *) continue ;; esac
    plain "$w" || continue
    [ -f "$w" ] && [ "$(stat -c %s "$w" 2>/dev/null || echo 9999999)" -lt 1048576 ] || continue
    grep -E "$BK_RE|SKIP=|/etc/gitlab|config_backup|gitlab_config|tar " "$w" 2>/dev/null | grep -vE '^[[:space:]]*#' | head -20
  done
}
SCHED=$(mktemp /tmp/kervax-gitlab.XXXXXX) || exit 0
trap 'rm -f "$SCHED"' EXIT
for f in $CRON_FILES; do
  [ -f "$f" ] || continue
  sys=0
  case "$f" in /etc/crontab|/etc/cron.d/*) sys=1 ;; esac
  grep -iE 'gitlab' "$f" 2>/dev/null | grep -vE '^[[:space:]]*(#|$)|gitlab-autoupgrade' | while IFS= read -r line; do
    case "$line" in [A-Za-z_]*=*) continue ;; esac
    set -f
    # shellcheck disable=SC2086
    set -- $line
    set +f
    if [ "${1#@}" != "$1" ]; then when=$1; shift; else when="$1 $2 $3 $4 $5"; shift 5 2>/dev/null || continue; fi
    [ "$sys" = 1 ] && shift
    cmd="$*"
    text="$cmd"$'\n'"$(script_lines "$cmd")"
    printf '%s' "$text" | grep -qE "$BK_RE" || continue
    printf 'cron:%s\t%s\t%s\n' "$f" "$when" "$(printf '%s' "$text" | tr '\n\t' '  ')"
  done >> "$SCHED"
done
for t in $(systemctl list-unit-files --type=timer --no-legend 2>/dev/null | awk '{print $1}' | grep -v '^gitlab-autoupgrade'); do
  svc=$(systemctl show -p Unit --value "$t" 2>/dev/null)
  [ -n "$svc" ] || continue
  ex=$(systemctl show -p ExecStart --value "$svc" 2>/dev/null | sed -n 's/.*argv\[\]=\([^;]*\);.*/\1/p' | head -3)
  [ -n "$ex" ] || continue
  text="$ex"$'\n'"$(script_lines "$ex")"
  printf '%s' "$text" | grep -qE "$BK_RE" || continue
  when=$(systemctl cat "$t" 2>/dev/null | sed -n 's/^OnCalendar=//p' | head -1)
  printf 'timer:%s\t%s\t%s\n' "$t" "$when" "$(printf '%s' "$text" | tr '\n\t' '  ')" >> "$SCHED"
done

# gitlab-autoupgrade configs: "APP_DIR (resolved)<TAB>conf<TAB>state dir"; values are read, not sourced
AUS=""
if [ -x "$AU_SCRIPT" ]; then
  for c in "$AU_DIR"/*.conf; do
    [ -f "$c" ] || continue
    cv() { sed -n "s/^[[:space:]]*$1=//p" "$c" | tail -1 | sed -E "s/[[:space:]]+#.*$//; s/^[\"']//; s/[\"']$//"; }
    ad=$(cv APP_DIR); ad=${ad:-/app/gitlab}
    sd=$(cv STATE_DIR); [ -n "$sd" ] || sd="$AU_DIR/state/$(basename "$c" .conf)"
    plain "$ad" && plain "$sd" || continue
    ad=$(readlink -f "$ad" 2>/dev/null || printf '%s' "$ad")
    AUS="$AUS$ad	$c	$sd	$(cv GITLAB_BACKUP)	$(cv BACKUP_SKIP)	$(cv ALLOW_MAJOR)	$(cv STALE_DAYS)
"
  done
fi

# the GitLab containers first: a schedule line is given to the instance whose name or directory it
# names, and with one GitLab on the node every line is its own
GL=""
for id in $IDS; do
  info=$(timeout 10 docker inspect -f '{{.Id}}|{{.Name}}|{{.State.Status}}|{{.Config.Image}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.project.config_files"}}|{{range .Mounts}}{{.Destination}}={{.Source}};{{end}}' "$id" 2>/dev/null) || continue
  case "$(printf '%s' "$info" | cut -d'|' -f4,8)" in *"/var/opt/gitlab="*|*gitlab/gitlab-ce*|*gitlab/gitlab-ee*) ;; *) continue ;; esac
  GL="$GL$info
"
done
NGL=$(printf '%s' "$GL" | grep -c .)
mine() { # $1 - a schedule text: does it belong to the instance $name in $dir
  [ "$NGL" -le 1 ] && return 0
  printf '%s' "$1" | grep -qF -e "$name" && return 0
  [ -n "$dir" ] && printf '%s ' "$1" | grep -qF -e "$dir/" -e "$dir " && return 0
  return 1
}

ROWS=""
while IFS= read -r info <&3; do
  [ -n "$info" ] || continue
  IFS='|' read -r cid name state image dir svc files mounts <<< "$info"
  name=${name#/}
  host_of() { # a path inside the container -> on the host, by the longest mount
    local p=$1 best="" src="" m d
    IFS=';' read -ra _m <<< "$mounts"
    for m in "${_m[@]}"; do
      d=${m%%=*}
      [ -n "$d" ] || continue
      case "$p/" in "${d%/}"/*) if [ ${#d} -gt ${#best} ]; then best=$d; src=${m#*=}; fi ;; esac
    done
    [ -n "$best" ] && printf '%s%s' "$src" "${p#"${best%/}"}"
  }
  data=$(host_of /var/opt/gitlab); conf=$(host_of /etc/gitlab)
  plain "$data" || data=""
  plain "$conf" || conf=""
  run=0; [ "$state" = running ] && run=1
  ver=""; ed=""; url=""
  if [ $run = 1 ]; then
    ver=$(timeout 15 docker exec "$cid" cat /opt/gitlab/embedded/service/gitlab-rails/VERSION 2>/dev/null | tr -d '\r\n ')
  fi
  case "$ver" in *-ee) ed=ee ;; ?*) ed=ce ;; esac
  ver=${ver%-ee}; ver=${ver%-ce}
  case "$ver" in [0-9]*.[0-9]*.[0-9]*) ;; *) ver="" ;; esac
  [ -n "$ed" ] || case "$image" in *gitlab-ee*) ed=ee ;; *gitlab-ce*) ed=ce ;; esac
  # external_url: GITLAB_OMNIBUS_CONFIG of the container, else gitlab.rb on the host. Only this
  # one value leaves the environment, there are passwords around it
  omni=$(timeout 10 docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" 2>/dev/null | sed -n '/^GITLAB_OMNIBUS_CONFIG=/,$p')
  rb=""; [ -n "$conf" ] && [ -f "$conf/gitlab.rb" ] && rb=$(grep -vE '^[[:space:]]*#' "$conf/gitlab.rb" 2>/dev/null)
  url=$(printf '%s\n%s\n' "$omni" "$rb" | grep -oE "(^|[^_])external_url[[:space:]]*\(?[[:space:]]*['\"][^'\"]+" | head -1 | sed -E "s/.*['\"]//")
  case "$url" in http://*|https://*) ;; *) url="" ;; esac
  # how it is installed: a gitlab/gitlab-* tag in compose, or an image built FROM one
  mode=image; from=""; dfile=""
  case "$image" in
    gitlab/gitlab-ce:*|gitlab/gitlab-ee:*) from=$image ;;
    *)
      mode=build
      if plain "$dir"; then
        for f in "$dir/Dockerfile" "$dir"/*/Dockerfile "$dir"/Dockerfile.*; do
          [ -f "$f" ] || continue
          l=$(grep -m1 -iE '^[[:space:]]*FROM[[:space:]]+gitlab/gitlab-(ce|ee):' "$f" 2>/dev/null) || continue
          from=$(printf '%s' "$l" | awk '{print $2}'); dfile=$f
          break
        done
      fi ;;
  esac
  # backups: backup_path of gitlab.rb or GITLAB_OMNIBUS_CONFIG, else the default, on the host
  bp=$(printf '%s\n%s\n' "$omni" "$rb" | grep -oE "gitlab_rails\[['\"]backup_path['\"]\][[:space:]]*=[[:space:]]*['\"][^'\"]+" | tail -1 | sed -E "s/.*['\"]//")
  keep=$(printf '%s\n%s\n' "$omni" "$rb" | grep -oE "gitlab_rails\[['\"]backup_keep_time['\"]\][[:space:]]*=[[:space:]]*[0-9]+" | tail -1 | grep -oE '[0-9]+$')
  bdir=$(host_of "${bp:-/var/opt/gitlab/backups}")
  plain "$bdir" || bdir=""
  bk=""
  if [ -n "$bdir" ] && [ -d "$bdir" ]; then
    list=$(find "$bdir" -maxdepth 1 -type f -name '*_gitlab_backup.tar' -printf '%T@ %s %f\n' 2>/dev/null | sort -n)
    n=0; total=0; first=0
    [ -n "$list" ] && n=$(printf '%s\n' "$list" | wc -l) \
      && total=$(printf '%s\n' "$list" | awk '{s += $2} END {printf "%.0f", s}') \
      && first=$(printf '%s\n' "$list" | head -1 | awk '{printf "%d", $1}')
    last=""
    if [ -n "$list" ]; then
      read -r lts lsz lname <<< "$(printf '%s\n' "$list" | tail -1)"
      bi=$(head -c 65536 "$bdir/$lname" 2>/dev/null | tar -xO backup_information.yml 2>/dev/null | head -40)
      skipped=$(printf '%s\n' "$bi" | sed -n "s/^:skipped:[[:space:]]*//p" | tr -d "'\" ")
      bver=$(printf '%s\n' "$bi" | sed -n "s/^:gitlab_version:[[:space:]]*//p" | tr -d "'\" ")
      binfo=0; [ -n "$bi" ] && binfo=1
      last=$(printf ',"last":{"name":%s,"ts":%d,"size":%s,"info":%d,"skipped":%s,"ver":%s}' \
        "$(js "$lname" 120)" "${lts%.*}" "$(num "$lsz")" "$binfo" "$(js "$skipped" 120)" "$(js "$bver" 40)")
    fi
    bk=$(printf '{"n":%d,"total":%s,"first":%d%s}' "$n" "$(num "$total")" "$first" "$last")
  fi
  # the config archive: gitlab-ctl backup-etc (config_backup), a config/ beside the backups,
  # gitlab_config_*.tar or gitlab-config-* anywhere next to them, a manual one in /root
  cfg=$(find ${conf:+"$conf/config_backup"} ${bdir:+"$bdir" "$bdir/config"} /root -maxdepth 1 -type f \
          \( -name 'gitlab_config_*.tar' -o -name 'gitlab-config-*' -o -name 'gitlab_config-*' -o -name 'etc-gitlab*' \) \
          -printf '%T@ %p\n' 2>/dev/null | sort -n)
  cfgj=""
  if [ -n "$cfg" ]; then
    read -r cts cpath <<< "$(printf '%s\n' "$cfg" | tail -1)"
    cfgj=$(printf ',"cfg":{"n":%d,"ts":%d,"path":%s}' "$(printf '%s\n' "$cfg" | wc -l)" "${cts%.*}" "$(js "$cpath" 200)")
  fi
  sec=0; [ -n "$conf" ] && [ -f "$conf/gitlab-secrets.json" ] && sec=1
  # schedules of this instance: lines naming its container or its directory; with one GitLab on
  # the node every line is its own
  sch=""
  while IFS=$'\t' read -r src when text; do
    [ -n "$src" ] || continue
    mine "$text" || continue
    skip=$(printf '%s' "$text" | grep -oE 'SKIP=[A-Za-z0-9_,]*' | head -1 | cut -d= -f2)
    etc=0; printf '%s' "$text" | grep -qE 'backup-etc|/etc/gitlab|config_backup|gitlab_config|gitlab-config' && etc=1
    db=0; printf '%s' "$text" | grep -qE "gitlab-backup[[:space:]]+create|gitlab:backup:create|gitlab-rake[[:space:]]+gitlab:backup" && db=1
    extra=""
    if [ "${src#timer:}" != "$src" ]; then
      t=${src#timer:}
      u=$(systemctl show -p Unit --value "$t" 2>/dev/null)
      extra=$(printf ',"enabled":%s,"last":%s,"next":%s,"result":%s' \
        "$(js "$(systemctl is-enabled "$t" 2>/dev/null)" 20)" \
        "$(sd_ts "$(systemctl show -p LastTriggerUSec --value "$t" 2>/dev/null)")" \
        "$(sd_ts "$(systemctl show -p NextElapseUSecRealtime --value "$t" 2>/dev/null)")" \
        "$(js "$(systemctl show -p Result --value "$u" 2>/dev/null)" 30)")
    fi
    sch="$sch${sch:+,}$(printf '{"how":%s,"src":%s,"when":%s,"db":%d,"skip":%s,"etc":%d%s}' \
      "$(js "${src%%:*}" 10)" "$(js "${src#*:}" 120)" "$(js "$when" 60)" "$db" "$(js "$skip" 80)" "$etc" "$extra")"
  done < "$SCHED"
  # batched background migrations: 0 paused, 1 active, 3 finished, 4 failed, 5 finalizing, 6 finalized
  bbm=""
  if [ $run = 1 ]; then
    q=$(timeout 60 docker exec "$cid" gitlab-psql -d gitlabhq_production -tA -F'|' -c \
      "select status, count(*) from batched_background_migrations group by 1 order by 1" 2>/dev/null | tr -d '\r')
    if printf '%s\n' "$q" | grep -qE '^[0-9]+\|[0-9]+$'; then
      cnt=$(printf '%s\n' "$q" | awk -F'|' '/^[0-9]+\|[0-9]+$/ { t += $2; if ($1 == 4) f += $2; else if ($1 != 3 && $1 != 6) u += $2 }
        END { printf "\"total\":%d,\"failed\":%d,\"open\":%d", t, f, u }')
      lst=""
      if printf '%s' "$cnt" | grep -qvE '"failed":0,"open":0$'; then
        lst=$(timeout 60 docker exec "$cid" gitlab-psql -d gitlabhq_production -tA -F'|' -c \
          "select status, job_class_name, table_name, column_name from batched_background_migrations where status not in (3, 6) order by status desc, id limit 8" 2>/dev/null \
          | tr -d '\r' | while IFS='|' read -r st job tb col; do
              case "$st" in ''|*[!0-9]*) continue ;; esac
              printf '{"st":%d,"job":%s,"table":%s,"col":%s}\n' "$st" "$(js "$job" 100)" "$(js "$tb" 80)" "$(js "$col" 60)"
            done | paste -sd, -)
      fi
      bbm=$(printf ',"bbm":{%s,"list":[%s]}' "$cnt" "$lst")
    fi
  fi
  # gitlab-autoupgrade of this directory
  au=""
  rd=""; plain "$dir" && rd=$(readlink -f "$dir" 2>/dev/null || printf '%s' "$dir")
  if [ -n "$AUS" ] && [ -n "$rd" ]; then
    line=$(printf '%s' "$AUS" | awk -F'\t' -v d="$rd" '$1 == d' | head -1)
    if [ -n "$line" ]; then
      IFS=$'\t' read -r _ac aconf asd agb askip amaj astale <<< "$line"
      st() { [ -f "$asd/$1" ] && head -c 2000 "$asd/$1" | head -1 || true; }
      hist=$(tail -5 "$asd/history" 2>/dev/null | while IFS= read -r h; do js "$h" 200; printf '\n'; done | paste -sd, -)
      au=$(printf ',"au":{"conf":%s,"timer":%s,"next":%s,"svc":%s,"result":%s,"blocked":%s,"broken":%s,"behind_since":%s,"last_result":%s,"history":[%s],"gitlab_backup":%s,"skip":%s,"allow_major":%s,"stale_days":%s}' \
        "$(js "$aconf" 120)" \
        "$(js "$(systemctl is-enabled gitlab-autoupgrade.timer 2>/dev/null)" 20)" \
        "$(sd_ts "$(systemctl show -p NextElapseUSecRealtime --value gitlab-autoupgrade.timer 2>/dev/null)")" \
        "$(js "$(systemctl show -p ActiveState --value gitlab-autoupgrade.service 2>/dev/null)" 20)" \
        "$(js "$(systemctl show -p Result --value gitlab-autoupgrade.service 2>/dev/null)" 30)" \
        "$(js "$(st blocked)" 300)" "$(js "$(st broken)" 300)" "$(num "$(st behind_since)")" "$(js "$(st last_result)" 300)" \
        "$hist" "$(js "${agb:-yes}" 10)" "$(js "${askip:-artifacts,registry}" 80)" "$(js "${amaj:-no}" 10)" "$(num "${astale:-7}")")
    fi
  fi
  row=$(printf '{"ct":%s,"state":%s,"image":%s,"dir":%s,"service":%s,"files":%s,"mode":%s,"from":%s,"dockerfile":%s,"version":%s,"edition":%s,"url":%s,"data":%s,"config":%s,"secrets":%d,"bdir":%s,"keep":%s%s%s%s%s,"sched":[%s]}' \
    "$(js "$name" 120)" "$(js "$state" 20)" "$(js "$image" 200)" "$(js "$dir" 200)" "$(js "$svc" 80)" "$(js "$files" 300)" \
    "$(js "$mode" 10)" "$(js "$from" 120)" "$(js "$dfile" 200)" "$(js "$ver" 20)" "$(js "$ed" 4)" "$(js "$url" 200)" \
    "$(js "$data" 200)" "$(js "$conf" 200)" "$sec" "$(js "$bdir" 200)" "$(num "$keep")" \
    "${bk:+,\"bk\":$bk}" "$cfgj" "$bbm" "$au" "$sch")
  ROWS="$ROWS${ROWS:+,}$row"
done 3<<< "$GL"
write "$ROWS"
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-gitlab.service <<EOF
[Unit]
Description=Kervax: GitLab version, backups and background migrations (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
TimeoutStartSec=5min
EOF
cat > /etc/systemd/system/kervax-gitlab.timer <<'EOF'
[Unit]
Description=Kervax: look at GitLab every 15 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-gitlab.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-gitlab.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/gitlab-setup.ver"
chmod 0644 "$STATE_DIR/versions/gitlab-setup.ver"
echo "✓ gitlab-setup: GitLab version, backups and background migrations -> $OUT (every 15 minutes)."
