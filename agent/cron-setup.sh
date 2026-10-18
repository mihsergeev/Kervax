#!/usr/bin/env bash
# Kervax: cron jobs on the host and inside containers. A job started by cron is invisible to
# systemd and to Docker. When it hangs, cron starts the next one on schedule next to it and they
# pile up: a minute job stuck on a network call makes sixty copies an hour, each holding memory,
# connections and locks. A job behind flock hangs alone, and the work it does silently stops.
# Nobody sees it until the server runs out of something. Cron also throws away whatever a job
# prints when there is no mail transport on the server: the job's errors go nowhere.
#
# A root helper finds every cron daemon on the node by its process (Debian cron and cronie,
# busybox crond, supercronic; on the host, in docker containers and Kubernetes pods), reads its
# crontabs inside its root and every minute looks at the jobs it runs right now: how many copies
# of each, since when, how much CPU a copy and its children used. A copy that ends is remembered
# with the time it ran (the last ten per job), so "much longer than usual" can be judged. On the
# host the journal gives each job's last start and its starts in a day, read by cursor. Whether a
# job's output reaches cron is read from its command line: redirections in order (2>&1 >> log
# sends stderr to cron, not to the log), pipes, lists of commands.
# Writes /var/lib/kervax/report.d/cron.json; the agent hands report.d to the panel as is.
#
# Passwords in command lines (password=, token=, mysql -p..., user:pass@ in URLs, curl -u, Bearer)
# are masked before anything is written. Read only: nothing is run inside containers.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: without a cron daemon it runs one ps and exits

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-cron"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/cron.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes,
# and rewriting it in place would make a run in progress continue from the same byte offset.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# Cron jobs on the host and in containers -> /var/lib/kervax/report.d/cron.json. Read only:
# one ps, the crontab files of each cron daemon, the CRON lines of the journal.
set -u
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
OUT=${KERVAX_CRON_OUT:-/var/lib/kervax/report.d/cron.json}
CACHE=${KERVAX_CRON_CACHE:-/var/lib/kervax/cron.cache}
MAX_JOBS=300   # jobs in the report
MAX_RUN=5      # running copies of one job listed; all of them are counted
FIRST_S=86400  # the first journal read looks a day back

exec 9>/run/kervax-cron.lock
flock -n 9 || exit 0
NOW=$(date +%s)
TMPD=$(mktemp -d /tmp/kervax-cron.XXXXXX) || exit 0
trap 'rm -rf "$TMPD"' EXIT

# -- cron daemons and the jobs they run now: one ps. A daemon is a cron process whose parent is
# not one (Debian cron forks "CRON" per job, cronie "crond", busybox and supercronic start the
# shell directly); a job is the shell under it with all its children --
ps -e -o pid=,ppid=,etimes=,time=,args= > "$TMPD/ps" 2>/dev/null
awk -v OFS='\t' '
  function cl(a) { return a ~ /^([^ ]*\/)?(cron|crond|CRON|CROND|supercronic)( |$)/ || a ~ /^([^ ]*\/)?busybox crond( |$)/ }
  function walk(p,   n, k, i) { sum += cpu[p]; cnt++; n = split(kids[p], k, " "); for (i = 1; i <= n; i++) walk(k[i]) }
  function job(m, j, age,   c) {
    c = args[j]
    if (c ~ /^([^ ]*\/)?(sendmail|exim4?|postdrop|ssmtp|msmtp)( |$)/) return  # cron mailing the output
    sub(/^([^ ]*\/)?(ba|da|a)?sh -c /, "", c)
    sum = 0; cnt = 0; walk(j)
    print "J", m, j, age, sum, cnt, c }
  { p = $1; pp[p] = $2; et[p] = $3; t = $4; a = $0; sub(/^ *[0-9]+ +[0-9]+ +[0-9]+ +[^ ]+ +/, "", a); args[p] = a
    d = 0; i = index(t, "-"); if (i) { d = substr(t, 1, i - 1); t = substr(t, i + 1) }
    n = split(t, tt, ":"); s = 0; for (i = 1; i <= n; i++) s = s * 60 + tt[i]; cpu[p] = s + d * 86400
    kids[$2] = kids[$2] " " p; iscl[p] = cl(a); ord[++np] = p }
  END {
    for (i = 1; i <= np; i++) {
      p = ord[i]
      if (!iscl[p] || iscl[pp[p]]) continue
      kind = args[p] ~ /supercronic/ ? "supercronic" : args[p] ~ /busybox/ ? "busybox" : "cron"
      print "M", p, kind, args[p]
      nk = split(kids[p], ks, " ")
      for (j = 1; j <= nk; j++) {
        c = ks[j]
        if (iscl[c]) { n2 = split(kids[c], k2, " "); for (m = 1; m <= n2; m++) job(p, k2[m], et[c]) }
        else job(p, c, et[c])
      }
    }
  }' "$TMPD/ps" > "$TMPD/tree"

if ! grep -q '^M' "$TMPD/tree"; then
  # no cron at all: an empty report, so the panel knows the helper works
  mkdir -p "${OUT%/*}"
  printf '{"v":1,"ts":%s,"journal":false,"disc24":0,"daemons":[],"jobs":[]}\n' "$NOW" > "$OUT.tmp.$$" \
    && chmod 0644 "$OUT.tmp.$$" && mv -f "$OUT.tmp.$$" "$OUT"
  exit 0
fi

cid_of() { # pid -> container id from its cgroup
  sed 's#.*/##' "/proc/$1/cgroup" 2>/dev/null | sed 's/^docker-//; s/^cri-containerd-//; s/^crio-//; s/\.scope$//' \
    | grep -m1 -E '^[0-9a-f]{64}$'
}
spec_of() { # container id -> its OCI config.json
  ls -d /run/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /run/k0s/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /run/k3s/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /var/snap/microk8s/common/run/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /run/docker/containerd/daemon/io.containerd.runtime.v2.task/*/"$1"/config.json 2>/dev/null | head -1
}
where_of() { # pid -> "container X" | "pod ns/name" | "host"
  local p=$1 id spec ns name
  id=$(cid_of "$p")
  [ -n "$id" ] || { echo host; return; }
  # docker ps once, into a file: where_of runs in $(...), a variable set here would not survive
  if [ ! -f "$TMPD/dps" ]; then
    : > "$TMPD/dps"
    command -v docker >/dev/null 2>&1 && timeout 10 docker ps --no-trunc --format '{{.ID}} {{.Names}}' > "$TMPD/dps" 2>/dev/null
  fi
  name=$(awk -v i="$id" '$1 == i { print $2; exit }' "$TMPD/dps")
  [ -n "$name" ] && { echo "container $name"; return; }
  spec=$(spec_of "$id")
  if [ -n "$spec" ]; then
    name=$(grep -o '"io.kubernetes.cri.sandbox-name":"[^"]*"' "$spec" | head -1 | cut -d'"' -f4)
    ns=$(grep -o '"io.kubernetes.cri.sandbox-namespace":"[^"]*"' "$spec" | head -1 | cut -d'"' -f4)
    [ -n "$name" ] && { echo "pod ${ns:+$ns/}$name"; return; }
  fi
  echo "container ${id:0:12}"
}
# a path inside the root of process $1: an absolute symlink there points into ITS root, while the
# kernel would follow it from ours (Alpine links /var/spool/cron/crontabs to /etc/crontabs)
in_root() {
  local p=$1 path=$2 t
  if [ -L "/proc/$p/root$path" ]; then
    t=$(readlink "/proc/$p/root$path" 2>/dev/null)
    case "$t" in /*) path=$t ;; *) path="${path%/*}/$t" ;; esac
  fi
  printf '/proc/%s/root%s' "$p" "$path"
}

# one crontab file -> "T master file user schedule mailto command" rows; $4 = 1 for a system
# crontab (a user field before the command); mailto: "none" after MAILTO="", "set", or empty
cron_file() {
  awk -v OFS='\t' -v mp="$1" -v src="$2" -v sys="$3" -v user="$4" '
    { l = $0; gsub(/\t/, " ", l); sub(/\r$/, "", l); sub(/^ +/, "", l)
      if (l == "" || l ~ /^#/) next
      if (l ~ /^[A-Za-z_][A-Za-z0-9_]*[ ]*=/) {
        if (l ~ /^MAILTO[ ]*=/) { v = l; sub(/^MAILTO[ ]*=[ ]*/, "", v); gsub(/["\047 ]/, "", v); mt = (v == "" ? "none" : "set") }
        next }
      if (l ~ /^@[a-z]+ /) {
        sched = l; sub(/ .*/, "", sched); c = l; sub(/^@[a-z]+ +/, "", c)
        if (sched !~ /^@(reboot|yearly|annually|monthly|weekly|daily|midnight|hourly)$/) next
      } else if (match(l, /^[^ ]+ +[^ ]+ +[^ ]+ +[^ ]+ +[^ ]+ +/)) {
        sched = substr(l, 1, RLENGTH); c = substr(l, RLENGTH + 1); gsub(/ +/, " ", sched); sub(/ $/, "", sched)
        if (sched !~ /^[0-9*\/,A-Za-z?LW#-]+ [0-9*\/,A-Za-z?LW#-]+ [0-9*\/,A-Za-z?LW#-]+ [0-9*\/,A-Za-z?LW#-]+ [0-9*\/,A-Za-z?LW#-]+$/) next
      } else next
      u = user
      if (sys) { u = c; sub(/ .*/, "", u); sub(/^[^ ]+ */, "", c) }
      if (c != "") print "T", mp, src, u, sched, mt, c }' "$5" 2>/dev/null | head -200 >> "$TMPD/rows"
}

: > "$TMPD/rows"
HOSTCRON=0
while IFS=$'\t' read -r tag P KIND ARGS; do
  [ "$tag" = M ] && [ -d "/proc/$P/root/" ] || continue
  W=$(where_of "$P")
  mta=0
  for f in /usr/sbin/sendmail /usr/lib/sendmail; do [ -e "$(in_root "$P" "$f")" ] && mta=1; done
  printf 'M\t%s\t%s\t%s\t%s\n' "$P" "$KIND" "$W" "$mta" >> "$TMPD/rows"
  case "$KIND" in
    cron)
      [ "$W" = host ] && HOSTCRON=1
      f=$(in_root "$P" /etc/crontab); [ -f "$f" ] && cron_file "$P" /etc/crontab 1 "" "$f"
      d=$(in_root "$P" /etc/cron.d)
      for f in "$d"/*; do
        [ -f "$f" ] || continue
        # Debian cron skips names with dots (*.dpkg-old, backups), as run-parts does
        case "${f##*/}" in *[!A-Za-z0-9_-]*) continue ;; esac
        cron_file "$P" "/etc/cron.d/${f##*/}" 1 "" "$f"
      done
      for d in /var/spool/cron/crontabs /var/spool/cron; do
        dd=$(in_root "$P" "$d")
        for f in "$dd"/*; do [ -f "$f" ] && cron_file "$P" "$d/${f##*/}" 0 "${f##*/}" "$f"; done
      done ;;
    busybox)
      d=$(printf '%s' "$ARGS" | awk '{ for (i = 1; i < NF; i++) if ($i == "-c") { print $(i + 1); exit } }')
      d=${d:-/var/spool/cron/crontabs}
      dd=$(in_root "$P" "$d")
      for f in "$dd"/*; do [ -f "$f" ] && cron_file "$P" "$d/${f##*/}" 0 "${f##*/}" "$f"; done ;;
    supercronic)
      c=$(printf '%s' "$ARGS" | awk '{ for (i = NF; i > 1; i--) if ($i !~ /^-/) { print $i; exit } }')
      case "$c" in /*) ;; ?*) c="$(readlink "/proc/$P/cwd" 2>/dev/null)/$c" ;; esac
      [ -n "$c" ] && f=$(in_root "$P" "$c") && [ -f "$f" ] && cron_file "$P" "$c" 0 "" "$f" ;;
  esac
done < "$TMPD/tree"

# -- the journal of the host cron: starts of each job and outputs thrown away, by cursor --
[ -f "$CACHE" ] || : > "$CACHE"
: > "$TMPD/j"
JRAN=0
if [ "$HOSTCRON" = 1 ] && command -v journalctl >/dev/null 2>&1; then
  cts=$(awk -F'	' '$1 == "C" { print $2; exit }' "$CACHE")
  cur=$(awk -F'	' '$1 == "C" { print $3; exit }' "$CACHE")
  cts=${cts:-0}
  # after a failed cursor (the journal was wiped) - from the previous read, not a day back again:
  # the starts already counted would be counted twice
  since=$((NOW - FIRST_S)); [ "$cts" -gt "$since" ] && since=$cts
  if [ -n "$cur" ] && [ "$cts" -gt $((NOW - FIRST_S)) ]      && timeout 60 journalctl -t CRON -t CROND --after-cursor="$cur" -o short-unix -q --no-pager --show-cursor           > "$TMPD/j" 2>/dev/null; then
    JRAN=1
  else
    # no entries at all is exit status 1 too; only a timeout means the journal was not read
    timeout 60 journalctl -t CRON -t CROND --since "@$since" -o short-unix -q --no-pager --show-cursor       > "$TMPD/j" 2>/dev/null
    [ $? -ne 124 ] && JRAN=1
  fi
fi

AWKFN='
function e(s,   _jp, _jn, _ji) { gsub(/[[:cntrl:]]/, "", s); _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return s }
function norm(c) { gsub(/[ \t]+/, " ", c); sub(/^ /, "", c); sub(/ $/, "", c); return c }
function unquote(c) { gsub(/["\047]/, "", c); return c }
# the part after an unescaped % is the job stdin (cron does not run or log it), \% is %
function pct(c,   i, ch, o, n) { if (!index(c, "%")) return c; o = ""; n = length(c)
  for (i = 1; i <= n; i++) { ch = substr(c, i, 1)
    if (ch == "\\" && substr(c, i + 1, 1) == "%") { o = o "%"; i++; continue }
    if (ch == "%") break
    o = o ch }
  sub(/ +$/, "", o); return o }
function mask(s,   out, st, ln, m) { s = substr(s, 1, 600); out = ""
  while (match(tolower(s), /(pass(word|wd)?|secret|token|api[_-]?key|authorization)["\047]?[ ]*[=:][ ]*[^ ]+/)) {
    st = RSTART; ln = RLENGTH; m = substr(s, st, ln); match(m, /[=:][ ]*/)
    out = out substr(s, 1, st - 1) substr(m, 1, RSTART + RLENGTH - 1) "***"; s = substr(s, st + ln) }
  s = out s; gsub(/[[:cntrl:]]/, " ", s); gsub(/^ +| +$/, "", s); return s }
function cmask(s,   out, m) { s = mask(s); out = ""
  while (match(s, /:\/\/[^\/:@ ]+:[^@\/ ]+@/)) { m = substr(s, RSTART, RLENGTH); sub(/:[^:]+@$/, ":***@", m); out = out substr(s, 1, RSTART - 1) m; s = substr(s, RSTART + RLENGTH) }
  s = out s; out = ""
  if (s ~ /mysql|mariadb/) while (match(s, / -p[^ ]+/)) { out = out substr(s, 1, RSTART - 1) " -p***"; s = substr(s, RSTART + RLENGTH) }
  s = out s; out = ""
  while (match(s, / (-u|--user) [^ :]+:[^ ]+/)) { m = substr(s, RSTART, RLENGTH); sub(/:[^:]*$/, ":***", m); out = out substr(s, 1, RSTART - 1) m; s = substr(s, RSTART + RLENGTH) }
  s = out s; out = ""
  while (match(s, /(Bearer|Basic) [^ "\047]+/)) { m = substr(s, RSTART, RLENGTH); sub(/ .*/, " ***", m); out = out substr(s, 1, RSTART - 1) m; s = substr(s, RSTART + RLENGTH) }
  return substr(out s, 1, 300) }
# where the output of a command line goes: "out", "err", "both" reach cron, "" - all of it is
# redirected. Quotes, subshells and groups are left out; each command of a list (; && ||) and
# each part of a pipe is followed through its redirections in order. ordbug: stderr was sent to
# cron by 2>&1 written before the redirection of stdout to a file
function reach(c,   s, n, i, cmds, w, nseg, segs, j, out, err, f1, f2, r, k, pre, post, fd, t, tg, ordp) {
  ordbug = 0; s = c
  gsub(/\\./, "_", s); gsub(/\047[^\047]*\047/, "Q", s); gsub(/"[^"]*"/, "Q", s)
  while (s ~ /\([^()]*\)/) gsub(/\([^()]*\)/, "G", s)
  while (s ~ /\{[^{}]*\}/) gsub(/\{[^{}]*\}/, "G", s)
  gsub(/&&|\|\||;/, "\n", s)
  n = split(s, cmds, "\n"); out = 0; err = 0
  for (i = 1; i <= n; i++) {
    w = cmds[i]; sub(/^ +/, "", w); sub(/ +$/, "", w)
    if (w == "") continue
    if (w !~ /[|>]/ && w ~ /^(cd|test|\[|export|sleep|true|false|:|umask|ulimit|set|unset|exit|source|\.)( |$)/) continue
    nseg = split(w, segs, "|")
    for (j = 1; j <= nseg; j++) {
      r = segs[j]; sub(/^&/, "", r); f1 = (j < nseg ? "p" : "c"); f2 = "c"; ordp = 0
      if (j < nseg && substr(segs[j + 1], 1, 1) == "&") f2 = "p"
      if (j == nseg && r ~ /^ *([^ ]*\/)?(logger|systemd-cat)( |$)/) f1 = "f"
      while ((k = index(r, ">")) > 0) {
        pre = substr(r, 1, k - 1); post = substr(r, k + 1)
        if (match(pre, /[0-9]+$/)) fd = substr(pre, RSTART) + 0; else if (pre ~ /&$/) fd = 3; else fd = 1
        if (substr(post, 1, 1) == ">") post = substr(post, 2)
        if (substr(post, 1, 1) == "&") {
          t = substr(post, 2)
          if (t ~ /^[0-9-]/) {
            tg = substr(t, 1, 1); r = substr(t, 2)
            if (fd == 2) { if (tg == "1") { if (f1 == "c") ordp = 1; f2 = f1 } else if (tg == "-") f2 = "f" }
            else if (fd == 1) { if (tg == "2") f1 = f2; else if (tg == "-") f1 = "f" }
            continue
          }
          fd = 3; post = t
        }
        sub(/^ +/, "", post); sub(/^[^ ]+/, "", post); r = post
        if (fd == 3) { f1 = "f"; f2 = "f" } else if (fd == 1) { f1 = "f"; if (ordp && f2 == "c") ordbug = 1 } else if (fd == 2) f2 = "f"
      }
      if (f2 == "c") err = 1
      if (f1 == "c") out = 1
    }
  }
  if (!err) ordbug = 0
  return out && err ? "both" : err ? "err" : out ? "out" : "" }
# "h:n,h:n" counts per hour: the last 25 hours of $1 plus the counts of hour keys in arr for k
function addh(old, k,   a, n, i, p, h, cn, res, nowh) { nowh = int(now / 3600); res = ""
  n = split(old, a, ","); for (i = 1; i <= n; i++) { p = index(a[i], ":"); if (!p) continue; h = substr(a[i], 1, p - 1) + 0
    if (h > nowh - 25) cn[h] += substr(a[i], p + 1) }
  for (h = nowh - 24; h <= nowh; h++) { if ((k SUBSEP h) in hb) cn[h] += hb[k SUBSEP h]; if (cn[h] > 0) res = res (res == "" ? "" : ",") h ":" cn[h] }
  return res }
function sumh(b,   a, n, i, p, s, nowh) { nowh = int(now / 3600); s = 0
  n = split(b, a, ","); for (i = 1; i <= n; i++) { p = index(a[i], ":"); if (p && substr(a[i], 1, p - 1) + 0 > nowh - 24) s += substr(a[i], p + 1) }
  return s }
'

# -- everything together: jobs from the crontabs, copies running now, starts from the journal,
# durations from the cache. Cache rows: C ts cursor | L key last per-hour | X per-hour (outputs
# thrown away) | D key seen durations | R key pid start age (copies seen running) --
awk -F'\t' -v OFS='\t' -v now="$NOW" -v jran="$JRAN" -v maxrun="$MAX_RUN" -v nc="$TMPD/cache" -v jout="$TMPD/jobs" "$AWKFN"'
  t == "cache" {
    if ($1 == "C") { cts = $2; ccur = $3 }
    else if ($1 == "L") { last[$2] = $3; bk[$2] = $4; lk[$2] = 1 }
    else if ($1 == "X") xb = $2
    else if ($1 == "D") { dur[$2] = $4; dseen[$2] = $3 }
    else if ($1 == "R") { pr[$2 SUBSEP $3] = $4 SUBSEP $5; npr++; prk[npr] = $2 SUBSEP $3 }
    next }
  t == "rows" && $1 == "M" { mk[$2] = $3; mw[$2] = $4; mmta[$2] = $5; nm++; mo[nm] = $2; next }
  t == "rows" && $1 == "T" {
    mp = $2; if (!(mp in mw)) next
    c = pct(norm($7)); cm = cmask(c); k = mw[mp] "|" cm
    # the run-parts lines every Debian and Alpine image carries: in a container they are noise,
    # shown only while one of them runs
    if (mw[mp] != "host" && c ~ /run-parts (--report )?\/etc\/(cron\.(hourly|daily|weekly|monthly)|periodic\/)/) stock[k] = 1
    else mj[mp]++
    if (k in job) next
    job[k] = 1; jn++; jk[jn] = k; jw[k] = mw[mp]; jkd[k] = mk[mp]; ju[k] = $4; jsc[k] = $5; jsrc[k] = $3; jc[k] = cm
    bycmd[mp SUBSEP c] = k; byunq[mp SUBSEP unquote(c)] = k
    r = reach(c)
    if (r != "" && mk[mp] != "supercronic" && ($6 == "none" || !mmta[mp])) { jlost[k] = r; jord[k] = ordbug }
    next }
  t == "tree" && $1 == "J" {
    mp = $2; if (!(mp in mw)) next
    c = norm($7)
    if ((mp SUBSEP c) in bycmd) k = bycmd[mp SUBSEP c]
    else if ((mp SUBSEP unquote(c)) in byunq) k = byunq[mp SUBSEP unquote(c)]
    else {
      cm = cmask(c); k = mw[mp] "|" cm
      if (!(k in job)) { job[k] = 1; jn++; jk[jn] = k; jw[k] = mw[mp]; jkd[k] = mk[mp]; ju[k] = ""; jsc[k] = ""; jsrc[k] = ""; jc[k] = cm }
    }
    st = now - $4
    rn[k]++; ri = rn[k]; ra[k, ri] = $4 + 0; rc[k, ri] = $5 + 0; rp[k, ri] = $6 + 0; rpid[k, ri] = $3
    ik = k SUBSEP $3; newi = 1
    if (ik in pr) { split(pr[ik], pv, SUBSEP); if (pv[1] - st <= 3 && st - pv[1] <= 3) newi = 0 }
    if (newi && st > obs[k]) obs[k] = st
    cur[ik] = st SUBSEP $4
    next }
  t == "j" {
    if (index($0, "-- cursor: ") == 1) { ncur = substr($0, 12); next }
    if (!match($0, /^[0-9]+/)) next
    ts = substr($0, 1, RLENGTH) + 0
    if (index($0, "No MTA installed, discarding output")) { hb["X" SUBSEP int(ts / 3600)]++; next }
    p = index($0, "]: ("); if (!p) next
    rest = substr($0, p + 4); q = index(rest, ") CMD ("); if (!q) next
    c = substr(rest, q + 7); sub(/\)[ \t]*$/, "", c); k = "host|" cmask(norm(c))
    if (ts > last[k]) last[k] = ts
    hb[k SUBSEP int(ts / 3600)]++; lk[k] = 1
    next }
  END {
    # the journal counts only once cron has written to it: a cursor appears with the first entry
    jok = jran && (ncur != "" || ccur != "")
    # copies seen last time and gone now ended: how long they ran (at least what was seen)
    for (i = 1; i <= npr; i++) {
      ik = prk[i]; if (ik in cur) continue
      split(ik, kp, SUBSEP); split(pr[ik], pv, SUBSEP)
      k = kp[1]; d = dur[k] (dur[k] == "" ? "" : ",") (pv[2] + 0)
      n = split(d, da, ","); d = ""; for (m = (n > 10 ? n - 9 : 1); m <= n; m++) d = d (d == "" ? "" : ",") da[m]
      dur[k] = d; dseen[k] = now
    }
    printf "C\t%s\t%s\n", (jran ? now : cts + 0), (ncur != "" ? ncur : ccur) > nc
    for (k in lk) {
      if (k in obs && obs[k] > last[k]) last[k] = obs[k]
      b = addh(bk[k], k); bk[k] = b
      if (last[k] + 0 < now - 2 * 86400 && !(k in job)) continue
      printf "L\t%s\t%d\t%s\n", k, last[k], b > nc
    }
    for (k in obs) if (!(k in lk)) { last[k] = obs[k]; printf "L\t%s\t%d\t\n", k, obs[k] > nc }
    xb = addh(xb, "X"); printf "X\t%s\n", xb > nc
    for (k in dur) if (dseen[k] + 0 > now - 30 * 86400) printf "D\t%s\t%d\t%s\n", k, dseen[k], dur[k] > nc
    for (ik in cur) { split(ik, kp, SUBSEP); split(cur[ik], pv, SUBSEP); printf "R\t%s\t%s\t%d\t%d\n", kp[1], kp[2], pv[1], pv[2] > nc }
    # the jobs: running ones first, the host first
    for (i = 1; i <= jn; i++) {
      k = jk[i]
      if ((k in stock) && !rn[k]) continue
      mx = 0; n = split(dur[k], da, ","); for (m = 1; m <= n; m++) if (da[m] + 0 > mx) mx = da[m] + 0
      runs = (jw[k] == "host" && jkd[k] == "cron" && jok) ? sumh(bk[k]) : -1
      rs = ""; nr = rn[k] + 0
      # oldest first, MAX_RUN of them
      for (m = 1; m <= nr; m++) used[m] = 0
      for (q = 1; q <= nr && q <= maxrun; q++) {
        b = 0; for (m = 1; m <= nr; m++) if (!used[m] && (b == 0 || ra[k, m] > ra[k, b])) b = m
        used[b] = 1
        rs = rs (rs == "" ? "" : ",") sprintf("{\"pid\":%d,\"age\":%d,\"cpu\":%d,\"procs\":%d}", rpid[k, b], ra[k, b], rc[k, b], rp[k, b])
      }
      js = sprintf("{\"where\":\"%s\",\"kind\":\"%s\",\"user\":\"%s\",\"sched\":\"%s\",\"src\":\"%s\",\"cmd\":\"%s\",\"last\":%d,\"runs\":%d,\"max\":%d,\"done\":%d,\"n\":%d",
                   e(jw[k]), jkd[k], e(ju[k]), e(jsc[k]), e(jsrc[k]), e(jc[k]), last[k] + 0, runs, mx, n, nr)
      if (k in jlost) js = js sprintf(",\"lost\":\"%s\"%s", jlost[k], jord[k] ? ",\"order\":1" : "")
      js = js ",\"run\":[" rs "]}"
      printf "%d%s\t%d\t%s\t%s\t%s\n", (jw[k] == "host" ? 0 : 1), jw[k], (nr > 0 ? 0 : 1), jsrc[k], jc[k], SUBSEP js > jout
    }
    # the daemons
    ds = ""
    for (i = 1; i <= nm; i++) { p = mo[i]
      ds = ds (ds == "" ? "" : ",") sprintf("{\"where\":\"%s\",\"kind\":\"%s\",\"mta\":%s,\"jobs\":%d}", e(mw[p]), mk[p], (mmta[p] ? "true" : "false"), mj[p] + 0) }
    printf "%s\t%s\t%d\n", (jok ? "true" : "false"), ds, sumh(xb)
  }' t=cache "$CACHE" t=rows "$TMPD/rows" t=tree "$TMPD/tree" t=j "$TMPD/j" > "$TMPD/head" || exit 0
mv -f "$TMPD/cache" "$CACHE" 2>/dev/null || : > "$CACHE"

IFS=$'\t' read -r JRN DS DISC < "$TMPD/head"
JOBS=$( { [ -f "$TMPD/jobs" ] && sort -t "$(printf '\t')" -k1,1 -k2,2n -k3,3 -k4,4 "$TMPD/jobs"; } | head -$MAX_JOBS \
  | awk -F "$(printf '\034')" '{ printf "%s%s", (NR > 1 ? "," : ""), $NF }')
mkdir -p "${OUT%/*}"
printf '{"v":1,"ts":%s,"journal":%s,"disc24":%s,"daemons":[%s],"jobs":[%s]}\n' "$NOW" "${JRN:-false}" "${DISC:-0}" "$DS" "$JOBS" > "$OUT.tmp.$$"
chmod 0644 "$OUT.tmp.$$"
mv -f "$OUT.tmp.$$" "$OUT"
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-cron.service <<EOF
[Unit]
Description=Kervax: cron jobs on the host and in containers (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
TimeoutStartSec=5min
EOF
cat > /etc/systemd/system/kervax-cron.timer <<'EOF'
[Unit]
Description=Kervax: look at the cron jobs every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-cron.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-cron.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/cron-setup.ver"
chmod 0644 "$STATE_DIR/versions/cron-setup.ver"
echo "✓ cron-setup: cron jobs on the host and in containers -> $OUT (every minute)."
