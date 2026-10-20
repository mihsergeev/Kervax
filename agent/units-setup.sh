#!/usr/bin/env bash
# Kervax: failed systemd units. A root helper lists the units in the "failed" state every
# minute - why they failed and the last lines of their log - and the restart counters of the
# services systemd restarts by itself (0.2: a crash loop never shows as failed), and writes
# /var/lib/kervax/report.d/units.json. The agent hands report.d to the panel as is.
#
# Since 0.3 it also lists running services that will not start after a reboot: started with
# "systemctl start" but never enabled, and nothing else (a socket, timer, path, D-Bus or another
# unit) pulls them in. Everything looks fine until the next reboot, then the service is gone.
#
# The panel can restart a failed unit or clear its failed mark through a spool, the same
# scheme as the other helpers. Only a unit that is failed right now: the panel cannot touch
# a working service.
#
# Since 0.4 the panel can also read the journal through the same spool: of one unit, of the
# whole system or of the kernel, for a period, up to 1000 lines, at a minimum priority. Read
# only: journalctl with fixed arguments, the unit name checked like for restart.
#
# Since 0.5 it also reads the OOM killer from the journal: what the kernel killed for memory
# (whole server or a cgroup limit), who held the memory (the task table the kernel prints before
# every kill), which units lost a process, and what systemd-oomd killed. The agent runs as an
# unprivileged user and cannot read the kernel journal. Read by cursor, written to
# /var/lib/kervax/report.d/oom.json (the last 7 days).
set -euo pipefail

KERVAX_SETUP_VERSION=0.5  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: it only reads systemctl and the journal

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-units"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/units.json"
AGENT_USER=kervax
REQ_DIR="$STATE_DIR/units-req"
RES_DIR="$STATE_DIR/units-res"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes,
# and rewriting it in place (cat >) made a run that was in progress continue from the same
# byte offset in the new text - a syntax error in the middle of a line and a failed unit.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# Failed systemd units -> /var/lib/kervax/report.d/units.json, and the panel's restart/reset
# requests from the spool. Reads systemctl and the journal; changes a unit only on request
# and only while that unit is failed.
set -u
export LC_ALL=C
OUT=/var/lib/kervax/report.d/units.json
REQ_DIR=/var/lib/kervax/units-req
RES_DIR=/var/lib/kervax/units-res
MAX_UNITS=30
MAX_RESTARTING=50

# JSON string body: control characters dropped, backslash and quote escaped. One raw control
# character makes the agent drop the whole file.
esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
num() { case "$1" in ''|*[!0-9]*) echo null ;; *) echo "$1" ;; esac; }

# A unit name systemd accepts (escaped names carry \x2d and the like) of a type worth
# watching. Transient units of systemd-run come and go by themselves and are left out.
unit_ok() {
  case "$1" in ''|-*|*[!A-Za-z0-9@._:\\-]*) return 1 ;; esac
  case "$1" in *.service|*.socket|*.timer|*.mount|*.automount|*.swap|*.path) ;; *) return 1 ;; esac
  case "$1" in run-u[0-9]*|run-r[0-9a-f]*|run-p[0-9]*) return 1 ;; esac
  return 0
}

# The last lines of the unit's log that say something: systemd's own "Main process exited",
# "Failed with result" and the like repeat what the panel already knows.
unit_log() {
  local raw
  raw=$(journalctl -u "$1" -n 40 --no-pager -o cat 2>/dev/null | tail -40)
  printf '%s\n' "$raw" \
    | grep -v -E '^[^ ]+\.(service|socket|timer|mount|automount|swap|path): (Main process exited|Failed with result|Consumed |Scheduled restart job|Deactivated successfully|Start request repeated|Control process exited|Killing process|Found left-over process|Unit process|Succeeded|Unit entered failed state|Triggering OnFailure)|^(Started|Stopped|Starting|Stopping|Failed to start|Finished|Dependency failed) ' \
    | grep -v '^$' | uniq | tail -5 > "$2"
  # nothing but systemd's lines - show those then, better than nothing
  [ -s "$2" ] || printf '%s\n' "$raw" | grep -v '^$' | uniq | tail -5 > "$2"
}

json_lines() {
  local out="" line
  while IFS= read -r line; do
    [ -n "$line" ] && out="$out${out:+,}\"$(esc "$(printf '%s' "$line" | cut -c1-200)")\""
  done < "$1"
  printf '%s' "$out"
}

scan() {
  local now up units="" n=0 unit props desc type result status mono restarts since tmp
  now=$(date +%s)
  up=$(awk '{printf "%.0f", $1 * 1000000}' /proc/uptime)
  tmp=$(mktemp)
  while read -r unit; do
    unit_ok "$unit" || continue
    [ "$n" -lt "$MAX_UNITS" ] || break
    n=$((n + 1))
    props=$(systemctl show "$unit" --no-pager -p Description -p Type -p Result -p ExecMainStatus \
      -p StateChangeTimestampMonotonic -p NRestarts 2>/dev/null)
    get() { printf '%s\n' "$props" | sed -n "s/^$1=//p" | head -1; }
    desc=$(get Description); type=$(get Type); result=$(get Result); status=$(get ExecMainStatus)
    mono=$(get StateChangeTimestampMonotonic); restarts=$(get NRestarts)
    # when it failed: the monotonic stamp is the same on every systemd, the wall-clock one
    # is printed in the local format and time zone
    since=0
    case "$mono" in ''|0|*[!0-9]*) ;; *) since=$(( now - (up - mono) / 1000000 )) ;; esac
    unit_log "$unit" "$tmp"
    units="$units${units:+,}{\"unit\":\"$(esc "$unit")\",\"desc\":\"$(esc "$desc")\",\"type\":\"$(esc "$type")\",\"result\":\"$(esc "$result")\",\"status\":$(num "$status"),\"since\":$since,\"restarts\":$(num "$restarts"),\"log\":[$(json_lines "$tmp")]}"
  done <<EOF_U
$(systemctl list-units --state=failed --all --no-legend --plain --no-pager 2>/dev/null | awk '{print $1}')
EOF_U
  # Services systemd restarts by itself (Restart=): one that crashes every few seconds and
  # comes back never shows as failed, only its restart counter grows. The panel counts the
  # growth between reports. Log lines only for a unit started in the last 15 minutes.
  local restarting="" m=0 svcs id nr act sub res st started
  svcs=$(systemctl list-units --type=service --all --no-legend --plain --no-pager 2>/dev/null | awk '{print $1}')
  if [ -n "$svcs" ]; then
    while IFS=$'\t' read -r id nr act sub res st mono; do
      unit_ok "$id" || continue
      [ "$m" -lt "$MAX_RESTARTING" ] || break
      m=$((m + 1))
      started=0
      case "$mono" in ''|0|*[!0-9]*) ;; *) started=$(( now - (up - mono) / 1000000 )) ;; esac
      : > "$tmp"
      [ "$started" -gt 0 ] && [ $(( now - started )) -lt 900 ] && unit_log "$id" "$tmp"
      restarting="$restarting${restarting:+,}{\"unit\":\"$(esc "$id")\",\"n\":$(num "$nr"),\"active\":\"$(esc "$act")\",\"sub\":\"$(esc "$sub")\",\"result\":\"$(esc "$res")\",\"status\":$(num "$st"),\"started\":$started,\"log\":[$(json_lines "$tmp")]}"
    done <<EOF_R
$(systemctl show --no-pager -p Id -p NRestarts -p ActiveState -p SubState -p Result -p ExecMainStatus \
    -p ActiveEnterTimestampMonotonic -- $svcs 2>/dev/null | awk -F= '
  function emit() { if (id != "" && nr + 0 > 0) print id "\t" nr "\t" act "\t" sb "\t" res "\t" st "\t" mono }
  NF == 0 { emit(); id = nr = act = sb = res = st = mono = ""; next }
  $1 == "Id" { id = $2 } $1 == "NRestarts" { nr = $2 } $1 == "ActiveState" { act = $2 }
  $1 == "SubState" { sb = $2 } $1 == "Result" { res = $2 } $1 == "ExecMainStatus" { st = $2 }
  $1 == "ActiveEnterTimestampMonotonic" { mono = $2 }
  END { emit() }')
EOF_R
  fi
  # Running services that will not come back after a reboot. enabled-runtime is left out: netplan
  # and other generators recreate it at every boot; so are units under /run (generated).
  local noboot="" k=0 running
  running=$(systemctl list-units --type=service --state=running --no-legend --plain --no-pager 2>/dev/null | awk '{print $1}')
  if [ -n "$running" ]; then
    while IFS=$'\t' read -r id mono; do
      unit_ok "$id" || continue
      [ "$k" -lt 50 ] || break
      k=$((k + 1))
      started=0
      case "$mono" in ''|0|*[!0-9]*) ;; *) started=$(( now - (up - mono) / 1000000 )) ;; esac
      noboot="$noboot${noboot:+,}{\"unit\":\"$(esc "$id")\",\"started\":$started}"
    done <<EOF_N
$(systemctl show --no-pager -p Id -p UnitFileState -p TriggeredBy -p WantedBy -p RequiredBy -p BoundBy \
    -p PartOf -p Transient -p BusName -p FragmentPath -p ActiveEnterTimestampMonotonic -- $running 2>/dev/null | awk -F= '
  function emit() {
    if (id != "" && st == "disabled" && trig == "" && wb == "" && rb == "" && bb == "" && po == "" \
        && tr != "yes" && bus == "" && frag !~ /^\/run\//) print id "\t" mono
  }
  NF == 0 { emit(); id = st = trig = wb = rb = bb = po = tr = bus = frag = mono = ""; next }
  $1 == "Id" { id = $2 } $1 == "UnitFileState" { st = $2 } $1 == "TriggeredBy" { trig = $2 }
  $1 == "WantedBy" { wb = $2 } $1 == "RequiredBy" { rb = $2 } $1 == "BoundBy" { bb = $2 }
  $1 == "PartOf" { po = $2 } $1 == "Transient" { tr = $2 } $1 == "BusName" { bus = $2 }
  $1 == "FragmentPath" { frag = $2 } $1 == "ActiveEnterTimestampMonotonic" { mono = $2 }
  END { emit() }')
EOF_N
  fi
  rm -f "$tmp"
  local fix=false
  [ -d "$REQ_DIR" ] && fix=true
  printf '{"v":1,"ts":%s,"units":[%s],"restarting":[%s],"noboot":[%s],"fix":%s}\n' "$now" "$units" "$restarting" "$noboot" "$fix" > "$OUT.tmp.$$"
  mv -f "$OUT.tmp.$$" "$OUT"
  chmod 0644 "$OUT"
}

# The journal for the panel: of a unit, of the whole system (_all) or of the kernel (_kernel),
# for the last N minutes, at most N lines, at a minimum priority. The text goes back gzipped
# and in base64 on one line: the result file is "key=value" lines.
journal_out() {
  local unit=$1 since=$2 lines=$3 prio=$4 tmp
  local -a sel
  case "$unit" in
    _all) sel=() ;;
    _kernel) sel=(-k) ;;
    *) unit_ok "$unit" || return 1; sel=(-u "$unit") ;;
  esac
  case "$since" in ''|*[!0-9]*) since=60 ;; esac
  [ "$since" -ge 1 ] || since=1
  [ "$since" -le 10080 ] || since=10080
  case "$lines" in ''|*[!0-9]*) lines=300 ;; esac
  [ "$lines" -ge 10 ] || lines=10
  [ "$lines" -le 1000 ] || lines=1000
  case "$prio" in err|warning|info) ;; *) prio=info ;; esac
  tmp=$(mktemp)
  timeout 30 journalctl ${sel[@]+"${sel[@]}"} --since "-${since}min" -n "$lines" -p "$prio" -o short-iso --no-pager \
    > "$tmp" 2>&1
  head -c 300000 "$tmp" | gzip -c | base64 | tr -d '\n'
  rm -f "$tmp"
}

process_spool() {
  local req id line k v op unit ok out rc state result tmp since lines prio changed=0
  for req in "$REQ_DIR"/*.req; do
    [ -f "$req" ] || continue
    id=$(basename "$req" .req)
    op=""; unit=""; since=""; lines=""; prio=""
    while IFS= read -r line; do
      k=${line%%=*}; v=${line#*=}
      case "$k" in op) op=$v ;; unit) unit=$v ;; since) since=$v ;; lines) lines=$v ;; prio) prio=$v ;; esac
    done < "$req"
    rm -f "$req"
    ok=false
    if [ "$op" = journal ]; then
      if out=$(journal_out "$unit" "$since" "$lines" "$prio"); then ok=true; else out="bad request"; fi
      printf 'ok=%s\noutput=%s\n' "$ok" "$out" > "$RES_DIR/$id.res.tmp"
      mv -f "$RES_DIR/$id.res.tmp" "$RES_DIR/$id.res"
      chmod 0644 "$RES_DIR/$id.res"
      continue
    fi
    changed=1
    if ! unit_ok "$unit" || { [ "$op" != restart ] && [ "$op" != reset ]; }; then
      out="bad request"
    elif ! systemctl is-failed --quiet -- "$unit"; then
      out="$(esc "$unit") is not failed now: the panel restarts and resets only failed units"
    else
      tmp=$(mktemp)
      if [ "$op" = restart ]; then
        # a oneshot unit runs to the end inside restart: certbot and the like take minutes
        timeout 300 systemctl restart -- "$unit" > "$tmp" 2>&1; rc=$?
      else
        systemctl reset-failed -- "$unit" > "$tmp" 2>&1; rc=$?
      fi
      state=$(systemctl is-active -- "$unit" 2>/dev/null)
      result=$(systemctl show -p Result --value -- "$unit" 2>/dev/null)
      [ "$rc" -eq 0 ] && [ "$state" != failed ] && ok=true
      unit_log "$unit" "$tmp"
      out="{\"op\":\"$op\",\"unit\":\"$(esc "$unit")\",\"state\":\"$(esc "$state")\",\"result\":\"$(esc "$result")\",\"rc\":$rc,\"log\":[$(json_lines "$tmp")]}"
      rm -f "$tmp"
    fi
    printf 'ok=%s\noutput=%s\n' "$ok" "$out" > "$RES_DIR/$id.res.tmp"
    mv -f "$RES_DIR/$id.res.tmp" "$RES_DIR/$id.res"
    chmod 0644 "$RES_DIR/$id.res"
  done
  # the panel should see the new state at once, not in a minute (reading the journal changes nothing)
  [ "$changed" = 1 ] && scan
  return 0
}

# -- OOM killer (0.5) --
# Kernel lines and systemd's notes about them, by journal cursor. The kernel prints a block per
# kill: "X invoked oom-killer", Mem-Info, the task table, "oom-kill:..." with the cgroup of the
# victim and "Out of memory: Killed process ...". Each kill becomes one line of $OOM_DIR/kills.tsv,
# the panel groups kills into episodes. "invoked" names the task that did not get a page, not the
# one that ate the memory: who held it is the task table, summed by name and uid (rss_anon, the
# shared memory of postgres would count once per process otherwise).
OOM_DIR=/var/lib/kervax/oom
OOM_OUT=/var/lib/kervax/report.d/oom.json
OOM_KEEP=$((7 * 86400))
OOM_MAX=60
# "A process of this unit has been killed by the OOM killer" (systemd SD_MESSAGE_UNIT_OUT_OF_MEMORY)
OOM_MSGID=fe6faa94e7774663a0da52717891d8ef

OOM_AWK=$(cat <<'OOM_AWK_EOF'
# input: journalctl -o short-unix; pg = page size in kB
function san(s) { gsub(/[^ -~]/, "?", s); gsub(/["\\|;]/, " ", s); return s }
function num(s) { return (s ~ /^-?[0-9]+$/) ? s : "" }
function after(s, key,   v) {
  if (!match(s, key "-?[0-9]+")) return ""
  return substr(s, RSTART + length(key), RLENGTH - length(key))
}
function block() { tb = 0; mi = 0; tn = 0; split("", tk); split("", tp); split("", tc); split("", M); split("", KV)
  swf = ""; swt = ""; ram = ""; lim = ""; cu = 0; ca = 0; cn = 0 }
function addrow(name, uid, pages,   k) {
  k = name SUBSEP uid
  if (!(k in tp)) tk[++tn] = k
  tp[k] += pages; tc[k]++
}
function top(   out, i, j, best, bk, used, k, parts) {
  out = ""; split("", used)
  for (j = 1; j <= 6; j++) {
    best = 0; bk = ""
    for (i = 1; i <= tn; i++) { k = tk[i]; if (!(k in used) && tp[k] > best) { best = tp[k]; bk = k } }
    if (bk == "") break
    used[bk] = 1; split(bk, parts, SUBSEP)
    out = out (out == "" ? "" : ";") parts[1] "|" parts[2] "|" tc[bk] "|" sprintf("%.0f", best * pg)
  }
  return out
}
function pgkb(x) { return (x == "") ? "" : sprintf("%.0f", x * pg) }
function emit(ts, pid, name, uid, anon, file, shm, adj, kind, cons, memcg, omemcg, line,   a, f) {
  a = (("active_anon" in M) || ("inactive_anon" in M)) ? pgkb(M["active_anon"] + M["inactive_anon"]) : ""
  f = (("active_file" in M) || ("inactive_file" in M)) ? pgkb(M["active_file"] + M["inactive_file"]) : ""
  print "K\t" ts "\t" num(pid) "\t" san(name) "\t" num(uid) "\t" num(anon) "\t" num(file) "\t" num(shm) "\t" num(adj) \
    "\t" kind "\t" san(cons) "\t" san(memcg) "\t" san(omemcg) "\t" san(inv) "\t" a "\t" f "\t" pgkb(M["shmem"]) \
    "\t" num(swf) "\t" num(swt) "\t" pgkb(ram) "\t" num(lim) "\t" top() "\t" san(substr(line, 1, 220))
}
BEGIN { block(); inv = "" }
/^-- cursor: / { print "C\t" substr($0, 12); next }
{
  if ($0 ~ /^[ \t]/) { msg = $0; sub(/^[ \t]+/, "", msg) }
  else {
    if ($1 !~ /^[0-9]+(\.[0-9]+)?$/ || NF < 3) next
    ts = $1; id = $3; p = index($0, " " id " ")
    msg = (p > 0) ? substr($0, p + length(id) + 2) : ""
  }
}
id == "kernel:" && msg ~ / invoked oom-killer: / { block(); inv = substr(msg, 1, index(msg, " invoked oom-killer") - 1); next }
id == "kernel:" && msg == "Mem-Info:" { mi = 1; next }
id == "kernel:" && mi == 1 {
  if (msg ~ /^Node /) { mi = 2; next }
  n = split(msg, W, " ")
  for (i = 1; i <= n; i++) if (W[i] ~ /^[a-z_]+:[0-9]+$/) { q = index(W[i], ":"); M[substr(W[i], 1, q - 1)] = substr(W[i], q + 1) }
  next
}
id == "kernel:" && msg ~ /^Free swap *= *[0-9]+/ { swf = msg; sub(/^[^=]*= */, "", swf); sub(/[^0-9].*$/, "", swf); next }
id == "kernel:" && msg ~ /^Total swap *= *[0-9]+/ { swt = msg; sub(/^[^=]*= */, "", swt); sub(/[^0-9].*$/, "", swt); next }
id == "kernel:" && msg ~ /^[0-9]+ pages RAM$/ { ram = msg; sub(/ .*/, "", ram); next }
id == "kernel:" && msg ~ /^memory: usage [0-9]+kB, limit [0-9]+kB/ { lim = after(msg, "limit "); next }
id == "kernel:" && msg ~ /^Tasks state / { tb = 1; next }
id == "kernel:" && tb && msg ~ /^\[ *pid *\]/ {
  h = msg; sub(/^\[ *pid *\] */, "", h); n = split(h, H, " "); cu = 0; ca = 0; cr = 0; cn = 0
  for (i = 1; i <= n; i++) { if (H[i] == "uid") cu = i; if (H[i] == "rss_anon") ca = i; if (H[i] == "rss") cr = i; if (H[i] == "name") cn = i }
  if (!ca) ca = cr
  next
}
id == "kernel:" && tb && msg ~ /^\[ *[0-9]+\] / {
  r = msg; sub(/^\[ *[0-9]+\] +/, "", r); n = split(r, F, " ")
  if (cu && ca && cn && n >= cn && F[ca] ~ /^[0-9]+$/) {
    nm = F[cn]; for (i = cn + 1; i <= n; i++) nm = nm " " F[i]
    addrow(san(nm), F[cu], F[ca] + 0)
  }
  next
}
id == "kernel:" && msg ~ /^oom-kill:/ {
  tb = 0; split("", KV); n = split(substr(msg, 10), P, ",")
  for (i = 1; i <= n; i++) { q = index(P[i], "="); if (q) KV[substr(P[i], 1, q - 1)] = substr(P[i], q + 1); else KV[P[i]] = 1 }
  next
}
id == "kernel:" && match(msg, /Killed process [0-9]+ \(/) {
  pre = substr(msg, 1, RSTART - 1); t = substr(msg, RSTART + 15)
  pid = t; sub(/ .*/, "", pid); t = substr(t, length(pid) + 3)
  e = index(t, ") total-vm:")
  if (!e) { e = length(t); while (e > 0 && substr(t, e, 1) != ")") e-- }
  nm = (e > 1) ? substr(t, 1, e - 1) : ""
  kind = (pre ~ /cgroup/ || KV["constraint"] == "CONSTRAINT_MEMCG") ? "memcg" : "global"
  mine = (KV["pid"] == pid)
  emit(ts, pid, nm, after(msg, "UID:"), after(msg, "anon-rss:"), after(msg, "file-rss:"), after(msg, "shmem-rss:"),
       after(msg, "oom_score_adj:"), kind, mine ? KV["constraint"] : "", mine ? KV["task_memcg"] : "",
       mine ? KV["oom_memcg"] : "", msg)
  block(); inv = ""
  next
}
id ~ /^systemd-oomd(\[[0-9]+\])?:$/ && msg ~ /^Killed \// {
  cg = substr(msg, 8); sub(/ .*/, "", cg); inv = ""; block()
  emit(ts, "", "", "", "", "", "", "", "oomd", (msg ~ /swap/) ? "swap" : "pressure", cg, "", msg)
  next
}
id ~ /^systemd(\[[0-9]+\])?:$/ && (msg ~ /: A process of this unit has been killed by the OOM killer/ || msg ~ /: Failed with result 'oom-kill'/) {
  u = substr(msg, 1, index(msg, ": ") - 1)
  if (u ~ /\.(service|scope)$/) print "U\t" ts "\t" san(u) "\t" ((msg ~ /Failed with/) ? "failed" : "oom")
  next
}
OOM_AWK_EOF
)

# oom.json for the panel: kills and unit notes of the last 7 days, uid with the user name
OOM_JSON=$(cat <<'OOM_JSON_EOF'
function n(x) { return (x ~ /^-?[0-9]+(\.[0-9]+)?$/) ? x : "null" }
function q(x) { return "\"" x "\"" }
function usr(u) { return (u in un) ? ",\"user\":" q(un[u]) : "" }
FNR == NR { if (split($0, pw, ":") >= 3 && pw[1] !~ /["\\]/) un[pw[3]] = pw[1]; next }
$1 == "K" && $2 + 0 >= cut { k[++nk] = $0; next }
$1 == "U" && $2 + 0 >= cut { u[++nu] = $0; next }
END {
  printf "{\"v\":1,\"ts\":%s,\"from\":%s,\"kills\":[", now, n(from)
  for (i = (nk > max ? nk - max + 1 : 1); i <= nk; i++) {
    split(k[i], f, "\t")
    tops = ""; nt = split(f[22], tt, ";")
    for (j = 1; j <= nt; j++) {
      if (split(tt[j], e, "|") != 4) continue
      tops = tops (tops == "" ? "" : ",") "{\"name\":" q(e[1]) ",\"uid\":" n(e[2]) usr(e[2]) ",\"n\":" n(e[3]) ",\"anon\":" n(e[4]) "}"
    }
    printf "%s{\"ts\":%s,\"pid\":%s,\"name\":%s,\"uid\":%s%s,\"anon\":%s,\"file\":%s,\"shmem\":%s,\"adj\":%s,\"kind\":%s,\"constraint\":%s,\"memcg\":%s,\"oom_memcg\":%s,\"by\":%s,\"mem\":{\"anon\":%s,\"file\":%s,\"shmem\":%s,\"swap_free\":%s,\"swap_total\":%s,\"ram\":%s,\"limit\":%s},\"top\":[%s],\"line\":%s}", \
      (i > (nk > max ? nk - max + 1 : 1) ? "," : ""), n(f[2]), n(f[3]), q(f[4]), n(f[5]), usr(f[5]), n(f[6]), n(f[7]), n(f[8]), n(f[9]), \
      q(f[10]), q(f[11]), q(f[12]), q(f[13]), q(f[14]), n(f[15]), n(f[16]), n(f[17]), n(f[18]), n(f[19]), n(f[20]), n(f[21]), tops, q(f[23])
  }
  printf "],\"units\":["
  for (i = (nu > 100 ? nu - 99 : 1); i <= nu; i++) {
    split(u[i], f, "\t")
    printf "%s{\"ts\":%s,\"unit\":%s,\"what\":%s}", (i > (nu > 100 ? nu - 99 : 1) ? "," : ""), n(f[2]), q(f[3]), q(f[4])
  }
  printf "]}\n"
}
OOM_JSON_EOF
)

oom_scan() {
  command -v journalctl >/dev/null 2>&1 || return 0
  local now cur from raw rc pg
  now=$(date +%s)
  install -d -m 0700 "$OOM_DIR"
  [ -f "$OOM_DIR/kills.tsv" ] || : > "$OOM_DIR/kills.tsv"
  cur=$(cat "$OOM_DIR/cursor" 2>/dev/null || true)
  from=$(cat "$OOM_DIR/from" 2>/dev/null || true)
  pg=$(( $(getconf PAGESIZE 2>/dev/null || echo 4096) / 1024 ))
  [ "$pg" -ge 1 ] || pg=4
  raw=$(mktemp)
  set -- _TRANSPORT=kernel + MESSAGE_ID=$OOM_MSGID + UNIT_RESULT=oom-kill + SYSLOG_IDENTIFIER=systemd-oomd
  if [ -n "$cur" ]; then
    timeout 60 journalctl "$@" --after-cursor="$cur" -o short-unix -q --no-pager --show-cursor > "$raw" 2>/dev/null; rc=$?
  else
    # the first read: a week back. A kernel log too big to read in a minute is skipped: from now on
    timeout 60 journalctl "$@" --since "-7 days" -o short-unix -q --no-pager --show-cursor > "$raw" 2>/dev/null; rc=$?
    from=$((now - OOM_KEEP))
    if [ "$rc" -eq 124 ]; then
      timeout 20 journalctl "$@" -n 1 -o short-unix -q --no-pager --show-cursor > "$raw" 2>/dev/null
      rc=0; from=$now; : > "$OOM_DIR/kills.tsv"
    fi
    echo "$from" > "$OOM_DIR/from"
  fi
  if [ "$rc" -ne 124 ]; then
    awk -v pg="$pg" "$OOM_AWK" "$raw" > "$raw.p" 2>/dev/null || : > "$raw.p"
    grep -v '^C	' "$raw.p" >> "$OOM_DIR/kills.tsv"
    cur=$(sed -n 's/^C	//p' "$raw.p" | tail -1)
    [ -n "$cur" ] && printf '%s\n' "$cur" > "$OOM_DIR/cursor"
    # a week, at most 400 lines
    awk -F'\t' -v cut=$((now - OOM_KEEP - 86400)) '$2 + 0 >= cut' "$OOM_DIR/kills.tsv" | tail -400 > "$raw.k"
    mv -f "$raw.k" "$OOM_DIR/kills.tsv"
  fi
  rm -f "$raw" "$raw.p" "$raw.k"
  awk -F'\t' -v now="$now" -v from="${from:-$now}" -v cut=$((now - OOM_KEEP)) -v max="$OOM_MAX" "$OOM_JSON" \
    /etc/passwd "$OOM_DIR/kills.tsv" > "$OOM_OUT.tmp.$$" 2>/dev/null && mv -f "$OOM_OUT.tmp.$$" "$OOM_OUT"
  rm -f "$OOM_OUT.tmp.$$"
  chmod 0644 "$OOM_OUT" 2>/dev/null || true
}

case "${1:-scan}" in
  scan) scan; oom_scan ;;
  process-spool) process_spool ;;
  *) echo "usage: $0 [scan|process-spool]" >&2; exit 2 ;;
esac
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chmod 0755 "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-units.service <<EOF
[Unit]
Description=Kervax: failed systemd units (read only)
[Service]
Type=oneshot
ExecStart=$HELPER scan
Nice=10
TimeoutStartSec=3min
EOF
cat > /etc/systemd/system/kervax-units.timer <<'EOF'
[Unit]
Description=Kervax: check for failed systemd units every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

# -- restart/reset requests from the panel through the spool --
if getent group "$AGENT_USER" >/dev/null 2>&1; then
  install -d -o root -g "$AGENT_USER" -m 0730 "$REQ_DIR"
  install -d -o root -g "$AGENT_USER" -m 0770 "$RES_DIR"
  # the agent runs under ProtectSystem=strict: let it write the spool in /var/lib/kervax
  if systemctl cat kervax-agent >/dev/null 2>&1 \
     && [ ! -f /etc/systemd/system/kervax-agent.service.d/kervax-spool.conf ]; then
    install -d -m 0755 /etc/systemd/system/kervax-agent.service.d
    printf '[Service]\nReadWritePaths=/var/lib/kervax\n' \
      > /etc/systemd/system/kervax-agent.service.d/kervax-spool.conf
    systemctl daemon-reload 2>/dev/null || true
    systemctl try-restart kervax-agent 2>/dev/null || true
  fi
  cat > /etc/systemd/system/kervax-units-req.service <<EOF
[Unit]
Description=Kervax: restart/reset requests for failed units from the panel
[Service]
Type=oneshot
ExecStart=$HELPER process-spool
TimeoutStartSec=10min
EOF
  cat > /etc/systemd/system/kervax-units-req.path <<EOF
[Unit]
Description=Kervax: watch the failed units request spool
[Path]
DirectoryNotEmpty=$REQ_DIR
Unit=kervax-units-req.service
[Install]
WantedBy=multi-user.target
EOF
else
  echo "· no Kervax agent group on this node: restart from the panel is not installed." >&2
fi

systemctl daemon-reload
systemctl enable --now kervax-units.timer >/dev/null 2>&1 || true
[ -d "$REQ_DIR" ] && systemctl enable --now kervax-units-req.path >/dev/null 2>&1 || true
systemctl start --no-block kervax-units.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/units-setup.ver"
chmod 0644 "$STATE_DIR/versions/units-setup.ver"
echo "✓ units-setup: failed systemd units -> $OUT (every minute); restart/reset through $REQ_DIR."
