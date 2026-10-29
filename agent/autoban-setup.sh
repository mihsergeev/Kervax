#!/usr/bin/env bash
# Kervax: automatic ban of web scanners. A root helper reads the access logs of the node's web
# servers every minute (the same logs webserver-setup counts: host files, docker json logs, pod
# logs of ingress-nginx), and an address that asks for 3 or more DIFFERENT trap paths within 10
# minutes - /.env, /.git/config, dumps *.sql, /actuator/*, /@vite/env, wp-config.php copies and the
# like, what no normal client ever asks for - is banned on 80/443 (tcp, and udp 443 for HTTP/3) in
# its own nftables table. The ban is the node's own: it works without the panel, and the panel
# gets no right to change the firewall - only to unban, to switch the mode and to hand over its
# allow list, through the spool like the other helpers.
#
# A ban lasts a day, a repeat within 30 days a week, the third and later a month. Never banned:
# loopback and the node's own addresses, private networks (pods, services, docker, the mesh in
# 100.64.0.0/10), Cloudflare (its edge carries everyone's traffic), an address that gives more
# than 20% of a log's requests over an hour (a proxy or balancer in front), one that asked for
# /.well-known/acme-challenge/ in the last day, the node's own list /etc/kervax/autoban-allow and
# the panel's list. A trap hit from an allowed address is only reported.
#
# Modes (/etc/kervax/autoban.conf, mode=): watch (the default: counts and reports whom it would
# ban, touches no firewall), on, off. 100+ bans within an hour switch "on" back to "watch" by
# itself: either a trap matches real traffic or a proxy hides the clients.
#
# Writes /var/lib/kervax/report.d/autoban.json; the agent hands report.d to the panel as is.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
# Where nginx runs (on the host, in a container or a pod: pgrep sees them all) - the same sign
# webserver-setup builds its log map by, and caddy:* lines have no log to read anyway. Not the map
# itself: its timer writes it only minutes after the install, and a node installed in one pass got
# no autoban and then a panel asking for it.
KERVAX_SETUP_WHEN="pgrep -x nginx >/dev/null 2>&1"

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-autoban"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/autoban.json"
AGENT_USER=kervax
REQ_DIR="$STATE_DIR/autoban-req"
RES_DIR="$STATE_DIR/autoban-res"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d" /etc/kervax
install -d -m 0700 "$STATE_DIR/autoban"
# a fresh node starts in watch mode: it shows whom it would ban before it bans anybody
[ -f /etc/kervax/autoban.conf ] || printf 'mode=watch\n' > /etc/kervax/autoban.conf

# Written next to the old one and swapped in with mv: bash reads a running script as it goes.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# Web scanners -> bans in nftables (table inet kervax_autoban) and
# /var/lib/kervax/report.d/autoban.json. Reads the access logs listed by webserver-setup.
set -u
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}
D=${KERVAX_AUTOBAN_DIR:-/var/lib/kervax/autoban}
OUT=${KERVAX_AUTOBAN_OUT:-/var/lib/kervax/report.d/autoban.json}
MAP=${KERVAX_AUTOBAN_MAP:-/var/lib/kervax/web-logs.tsv}
CONF=${KERVAX_AUTOBAN_CONF:-/etc/kervax/autoban.conf}
NODE_ALLOW=${KERVAX_AUTOBAN_ALLOW:-/etc/kervax/autoban-allow}
REQ_DIR=${KERVAX_AUTOBAN_REQ:-/var/lib/kervax/autoban-req}
RES_DIR=${KERVAX_AUTOBAN_RES:-/var/lib/kervax/autoban-res}
LOCK=${KERVAX_AUTOBAN_LOCK:-/run/kervax-autoban.lock}
NFT=${KERVAX_AUTOBAN_NFT:-nft}
TABLE="inet kervax_autoban"
CAP=$((16 * 1024 * 1024))   # more than this per log and run is skipped to the end
WINDOW=600                  # trap hits are counted over 10 minutes
TRAPS=3                     # different trap paths within the window -> ban
GRACE=90                    # a banned address seen this long after the ban: the ban does not hold
AVALANCHE=100               # bans within an hour that switch "on" back to "watch"
TAB=$(printf '\t')
mkdir -p "$D" "${OUT%/*}"

# Cloudflare: its edge carries everyone's traffic, a ban of an edge address takes the sites down
# for all its visitors (https://www.cloudflare.com/ips/)
CF_NETS="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18
190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14
172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32
2a06:98c0::/29 2c0f:f248::/32"
LOCAL_NETS="127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 0.0.0.0/8
::1/128 ::/128 fc00::/7 fe80::/10"

# Addresses and networks for awk: v4 as a number, v6 as 8 numbers of 16 bits. awk numbers are
# doubles: 2^32 fits. No strtonum, gensub or {n} - the same code runs in mawk.
LIB='
function hexv(h,   i, v, c) { v = 0; h = tolower(h)
  for (i = 1; i <= length(h); i++) { c = index("0123456789abcdef", substr(h, i, 1)); if (!c) return -1; v = v * 16 + c - 1 }
  return v }
function v4(s,   a, n, i, v) { n = split(s, a, "."); if (n != 4) return -1; v = 0
  for (i = 1; i <= 4; i++) { if (a[i] !~ /^[0-9]+$/ || a[i] + 0 > 255) return -1; v = v * 256 + a[i] }
  return v }
# v6 into g[1..8]; 1 - ok. ::ffff:a.b.c.d (v4 mapped) is left to the caller
function v6(s, g,   h, t, nh, nt, ht, tt, i, k, x) {
  if (s !~ /^[0-9A-Fa-f:]+$/ || index(s, ":") == 0) return 0
  k = index(s, "::")
  if (k) { h = substr(s, 1, k - 1); t = substr(s, k + 2) } else { h = s; t = "" }
  nh = (h == "") ? 0 : split(h, ht, ":"); nt = (t == "") ? 0 : split(t, tt, ":")
  if (k == 0 && nh != 8) return 0
  if (nh + nt > (k ? 7 : 8)) return 0
  for (i = 1; i <= 8; i++) g[i] = 0
  for (i = 1; i <= nh; i++) { x = hexv(ht[i]); if (x < 0 || length(ht[i]) > 4 || ht[i] == "") return 0; g[i] = x }
  for (i = 1; i <= nt; i++) { x = hexv(tt[i]); if (x < 0 || length(tt[i]) > 4 || tt[i] == "") return 0; g[8 - nt + i] = x }
  return 1 }
# a client address as written in a log: v4, v6 or v4 mapped into v6; "" - not an address
function addr(s,   g) { sub(/^\[/, "", s); sub(/\]$/, "", s); sub(/^::ffff:/, "", s)
  if (v4(s) >= 0) return s
  if (v6(s, g)) return tolower(s)
  return "" }
function net_add(c, lab,   p, b, n, g, i) {
  p = index(c, "/"); if (p) { b = substr(c, p + 1); c = substr(c, 1, p - 1) } else b = ""
  if (b != "" && b !~ /^[0-9]+$/) return 0
  if (v4(c) >= 0) { if (b == "") b = 32; if (b + 0 > 32) return 0
    n = ++NN; NT[n] = 4; NB[n] = b + 0; NV[n] = int(v4(c) / 2 ^ (32 - b)); NL[n] = lab; return 1 }
  if (v6(c, g)) { if (b == "") b = 128; if (b + 0 > 128) return 0
    n = ++NN; NT[n] = 6; NB[n] = b + 0; NL[n] = lab; for (i = 1; i <= 8; i++) NG[n, i] = g[i]; return 1 }
  return 0 }
# label of the allow list the address falls into, "" - none
function allowed(s,   x, g, n, i, b, full, r) {
  if (s in ALC) return ALC[s]
  x = v4(s); r = ""
  if (x < 0 && !v6(s, g)) { ALC[s] = ""; return "" }
  for (n = 1; n <= NN && r == ""; n++) {
    if (x >= 0) { if (NT[n] == 4 && int(x / 2 ^ (32 - NB[n])) == NV[n]) r = NL[n]; continue }
    if (NT[n] != 6) continue
    b = NB[n]; full = int(b / 16)
    for (i = 1; i <= full; i++) if (g[i] != NG[n, i]) break
    if (i <= full) continue
    if (b % 16 && int(g[full + 1] / 2 ^ (16 - b % 16)) != int(NG[n, full + 1] / 2 ^ (16 - b % 16))) continue
    r = NL[n] }
  ALC[s] = r; return r }
function load_allow(f,   l, t) { while ((getline l < f) > 0) { split(l, t, "\t"); net_add(t[1], t[2]) } close(f) }
'

conf_get() { sed -n "s/^$1=//p" "$CONF" 2>/dev/null | tail -1; }
mode_now() { case "$(conf_get mode)" in on) echo on ;; off) echo off ;; *) echo watch ;; esac; }
conf_set() { # mode auto
  local t; t=$(mktemp "$CONF.XXXXXX") || return 1
  printf 'mode=%s\nauto=%s\n' "$1" "${2:-}" > "$t"; chmod 0644 "$t"; mv -f "$t" "$CONF"
}

# Allow list of this run: built in, the node's own addresses, the node's file and the panel's
# list (cidr TAB label). A label tells the panel why an address was spared.
build_allow() {
  local n
  {
    for n in $LOCAL_NETS; do printf '%s\tlocal\n' "$n"; done
    for n in $CF_NETS; do printf '%s\tcloudflare\n' "$n"; done
    ip -o addr show 2>/dev/null | awk '{ split($4, a, "/"); if (a[1] != "") print a[1] "\tself" }'
    [ -f "$NODE_ALLOW" ] && awk '{ c = $1; if (c == "" || c ~ /^#/) next; lab = "node"
        if (match($0, /#.*/)) { lab = substr($0, RSTART + 1); gsub(/^[ \t]+|[ \t]+$/, "", lab) }
        gsub(/[\\"\t\001-\037]/, "", lab); if (lab == "") lab = "node"; print c "\t" substr(lab, 1, 40) }' "$NODE_ALLOW"
    [ -f "$D/panel-allow" ] && cat "$D/panel-allow"
  } > "$D/allow.cur"
}

# -- nftables: own table, a set per family, drops before docker and kube-proxy DNAT (raw) --
nft_ok() { command -v "$NFT" >/dev/null 2>&1; }
nft_ensure() {
  "$NFT" list table $TABLE >/dev/null 2>&1 && return 0
  "$NFT" -f - <<'NFT_EOF' 2>"$D/nft.err" || return 1
table inet kervax_autoban {
  set ban4 { type ipv4_addr; flags timeout; }
  set ban6 { type ipv6_addr; flags timeout; }
  chain pre {
    type filter hook prerouting priority -300; policy accept;
    ip saddr @ban4 tcp dport { 80, 443 } counter drop
    ip saddr @ban4 udp dport 443 counter drop
    ip6 saddr @ban6 tcp dport { 80, 443 } counter drop
    ip6 saddr @ban6 udp dport 443 counter drop
  }
}
NFT_EOF
  # restored with the time that is left: after a reboot or a switch back from watch
  awk -F'\t' -v now="$now" '$9 == 0 && $3 > now { print $1 "\t" ($3 - now) }' "$D/bans.tsv" 2>/dev/null \
    | while IFS="$TAB" read -r ip left; do nft_add "$ip" "$left"; done
  return 0
}
nft_set() { case "$1" in *:*) echo ban6 ;; *) echo ban4 ;; esac; }
nft_add() { "$NFT" add element $TABLE "$(nft_set "$1")" "{ $1 timeout ${2}s }" 2>>"$D/nft.err"; }
nft_del() { "$NFT" delete element $TABLE "$(nft_set "$1")" "{ $1 }" >/dev/null 2>&1; }
nft_drop_table() { "$NFT" list table $TABLE >/dev/null 2>&1 && "$NFT" delete table $TABLE >/dev/null 2>&1; return 0; }
# addresses in the sets right now
nft_members() {
  "$NFT" list set $TABLE ban4 2>/dev/null | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}'
  "$NFT" list set $TABLE ban6 2>/dev/null | tr ',{}' '\n\n\n' | grep -oE '^[[:space:]]*[0-9a-f:]*:[0-9a-f:]+' | tr -d ' \t'
}
nft_drops() { "$NFT" list chain $TABLE pre 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "packets") s += $(i + 1) } END { printf "%.0f", s + 0 }'; }

# -- new lines of every access log of webserver-setup's map --
# Map columns: log, domains, name, format, error log. docker:<name> -> the json log of the
# container; caddy:* has no log (Caddy counts by its metrics) and is skipped here.
collect() {
  local line lg rest names name src ino size prev pino psize from dpaths dnames where
  local first=0
  [ -s "$D/offsets" ] || first=1
  : > "$D/offsets.new"
  LOGS=0
  [ -s "$MAP" ] || return 0
  dpaths=""
  dnames=$(awk -F"$TAB" '$1 ~ /^docker:/ { sub(/^docker:/, "", $1); print $1 }' "$MAP" | sort -u)
  if [ -n "$dnames" ] && command -v docker >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    dpaths=$(docker inspect -f '{{.Name}}|{{.LogPath}}' $dnames 2>/dev/null | sed 's#^/##')
  fi
  while IFS= read -r line; do
    lg=${line%%"$TAB"*}; rest=${line#*"$TAB"}; [ "$rest" = "$line" ] && rest=""
    names=${rest%%"$TAB"*}; name=${rest#*"$TAB"}; [ "$name" = "$rest" ] && name=""; name=${name%%"$TAB"*}
    case "$lg" in ''|caddy:*) continue ;; esac
    src=$lg
    case "$lg" in docker:*) src=$(printf '%s\n' "$dpaths" | awk -F'|' -v n="${lg#docker:}" '$1 == n { print $2; exit }') ;; esac
    [ -n "$src" ] && [ -f "$src" ] || continue
    ino=$(stat -c %i "$src" 2>/dev/null) || continue
    size=$(stat -c %s "$src" 2>/dev/null) || continue
    prev=$(awk -F"$TAB" -v p="$lg" '$1 == p { print $2, $3; exit }' "$D/offsets" 2>/dev/null)
    # shellcheck disable=SC2086
    set -- $prev
    pino=${1:-}; psize=${2:-}
    printf '%s\t%s\t%s\n' "$lg" "$ino" "$size" >> "$D/offsets.new"
    LOGS=$((LOGS + 1))
    [ "${SKIP:-0}" = 1 ] && continue
    # The first run starts at the end: old lines would ban for what was done days ago. A log
    # that shows up later (a pod rolled out, a container recreated) is new and read whole
    if [ -z "$pino" ]; then
      [ "$first" = 1 ] && continue
      pino=$ino; psize=0
    fi
    if [ "$pino" = "$ino" ] && [ "$size" -ge "$psize" ]; then from=$psize
    elif [ "$pino" != "$ino" ]; then from=0   # rotated: the new file holds only new lines
    else from=0; fi                         # truncated in place (copytruncate)
    [ "$size" -gt "$from" ] || continue
    [ $((size - from)) -le "$CAP" ] || from=$((size - CAP))
    where=${names%% *}; [ -n "$where" ] || where=${name:-${lg##*/}}
    printf '\001L\t%s\t%s\n' "$lg" "$where"
    tail -c "+$((from + 1))" "$src" 2>/dev/null | head -c "$CAP"
    printf '\n'
  done < "$MAP"
}

# Log lines -> facts for the decision. Output lines:
#   T ip path where label   - a trap hit (label: the allow list it falls into, "" - none)
#   A ip                    - asked for an ACME challenge
#   S ip n                  - lines of an address that is banned right now
#   C log ip n / N log n    - requests per address and per log (a proxy in front gives a big share)
#   K pub cf priv           - whom the logs show: clients themselves, Cloudflare edges, private
PARSE='
function trap(p) {
  if (p ~ /\/\.well-known\//) return 0
  if (p ~ /(^|\/)\.(env|git|git-credentials|gitconfig|svn|hg|bzr|aws|ssh|docker|dockercfg|ds_store|htpasswd|npmrc|yarnrc|pypirc|netrc|bash_history|zsh_history|mysql_history|vscode|idea|terraform|kube)([\/._~-]|$)/) return 1
  if (p ~ /\.(sql|sql\.gz|sql\.zip|sql\.bz2|sql\.tar\.gz|dump|bak)$/) return 1
  if (p ~ /(^|\/)(backup|backups|db|database|dump|site|www|wwwroot|htdocs|public_html)\.(zip|tar|tar\.gz|tgz|rar|7z)$/) return 1
  if (p ~ /wp-config\.php/) return 1
  if (p ~ /(^|\/)actuator(\/|$)/) return 1
  if (p ~ /\/@vite\/env|\/@fs\//) return 1
  if (p ~ /\/vendor\/phpunit\//) return 1
  if (p ~ /\/etc\/passwd|\/proc\/self\//) return 1
  if (p ~ /\.\.\/|\.\.%2f|%2e%2e|\.%2e|%2e\.|%252e/) return 1
  if (p ~ /\/(trace|elmah)\.axd$/) return 1
  if (p ~ /\/server-status$|\/phpinfo\.php$|\/info\.php$/) return 1
  return 0 }
function safe(s, n) { s = substr(s, 1, n); gsub(/[^A-Za-z0-9._~\/%@:+=,!-]/, "?", s); return s }
function cls(ip,   l) { if (ip in CL) return CL[ip]; l = allowed(ip)
  CL[ip] = (l == "cloudflare") ? "cf" : (l == "local" || l == "self") ? "priv" : "pub"; return CL[ip] }
BEGIN { load_allow(allowf)
  while ((getline l < bansf) > 0) { split(l, t, "\t"); if (t[9] == 0 && t[2] + grace <= now) BANNED[t[1]] = 1 }
  close(bansf) }
substr($0, 1, 1) == "\001" { split($0, t, "\t"); lg = t[2]; where = t[3]; next }
{
  l = $0
  # CRI (containerd): "2026-10-27T11:59:40.1Z stdout F <line>"
  if (l ~ /^[0-9][0-9-]*T[0-9:.]+[^ ]* (stdout|stderr) [FP] /) sub(/^[^ ]+ [^ ]+ [FP] /, "", l)
  # docker json-file: {"log":"<line>\n","stream":...}
  else if (substr(l, 1, 8) == "{\"log\":\"") { l = substr(l, 9); sub(/(\\n)?","stream":.*$/, "", l); gsub(/\\"/, "\"", l); gsub(/\\\\/, "\\", l) }
  if (!match(l, /"(GET|POST|HEAD|PUT|DELETE|OPTIONS|PATCH|PROPFIND|CONNECT|TRACE) [^ "]+/)) next
  req = substr(l, RSTART, RLENGTH); sub(/^"[A-Z]+ /, "", req)
  sp = index(l, " "); ip = addr(sp ? substr(l, 1, sp - 1) : l)
  if (ip == "") next
  c = cls(ip)
  if (c == "priv" && match(l, /"[0-9A-Fa-f.:, ]+"$/)) {
    # nginx behind a proxy on this node: the last X-Forwarded-For hop is the one the proxy saw
    x = substr(l, RSTART + 1, RLENGTH - 2); n = split(x, xs, ","); y = xs[n]; gsub(/ /, "", y); y = addr(y)
    if (y != "") { ip = y; c = cls(ip) } }
  K[c]++
  if (c == "priv") next
  CNT[lg, ip]++; TOT[lg]++
  if (ip in BANNED) SEEN[ip]++
  q = index(req, "?"); p = q ? substr(req, 1, q - 1) : req
  if (p ~ /\/\.well-known\/acme-challenge\// || l ~ /cert-manager/) { if (!(ip in ACME)) { ACME[ip] = 1; print "A\t" ip } next }
  lp = tolower(p)
  if (!trap(lp)) next
  w = where
  if (match(l, / \[[a-z0-9][a-z0-9-]*\] \[/)) { w = substr(l, RSTART + 2, RLENGTH - 5) }
  # one request seen twice (the ingress log and the log of the pod behind it) is one hit
  tm = ""; if (match(l, /\[[0-9][0-9]\/[A-Za-z]+\/[0-9]+:[0-9:]+/)) tm = substr(l, RSTART, RLENGTH)
  k = ip SUBSEP lp SUBSEP tm
  if (tm != "" && (k in DUP)) next
  DUP[k] = 1
  print "T\t" ip "\t" safe(p, 100) "\t" safe(w, 80) "\t" allowed(ip)
}
END {
  for (x in SEEN) print "S\t" x "\t" SEEN[x]
  for (x in CNT) if (CNT[x] >= 3) { split(x, kk, SUBSEP); print "C\t" kk[1] "\t" kk[2] "\t" CNT[x] }
  for (x in TOT) print "N\t" x "\t" TOT[x]
  print "K\t" (K["pub"] + 0) "\t" (K["cf"] + 0) "\t" (K["priv"] + 0)
}'

# Hits within the window -> bans. Files: bans.tsv (ip since until n hits paths more where w seen
# applied), history.tsv (ip ts of every ban, 30 days, for the repeat count), hits.tsv (ts ip path
# where), proxy.tsv / acme.tsv / immune.tsv (ip until), new facts on stdin.
DECIDE='
function dur(n) { return n <= 1 ? 86400 : n == 2 ? 604800 : 2592000 }
BEGIN { FS = "\t"
  while ((getline l < proxyf) > 0) { split(l, t, "\t"); if (t[2] > now) SPARE[t[1]] = "proxy" } close(proxyf)
  while ((getline l < acmef) > 0) { split(l, t, "\t"); if (t[2] > now) SPARE[t[1]] = "acme" } close(acmef)
  while ((getline l < immunef) > 0) { split(l, t, "\t"); if (t[2] > now) SPARE[t[1]] = "unbanned" } close(immunef)
  while ((getline l < histf) > 0) { split(l, t, "\t"); if (t[2] > now - 2592000) { HIST[t[1]]++; HL[++nh] = l } } close(histf)
  while ((getline l < bansf) > 0) { split(l, t, "\t"); if (t[3] > now) { B[t[1]] = l; BO[++nb] = t[1] } } close(bansf)
  while ((getline l < hitsf) > 0) { split(l, t, "\t"); if (t[1] > now - window) { H[++nhit] = l } } close(hitsf) }
$1 == "S" { SEEN[$2] += $3; next }
$1 == "T" { if ($5 != "") { print now "\t" $2 "\t" $5 "\t" $3 >> ownf; next }
            if ($2 in SPARE) { SP[$2] = SPARE[$2]; next }
            H[++nhit] = now "\t" $2 "\t" $3 "\t" $4; next }
END {
  printf "" > hitsf
  for (i = 1; i <= nhit; i++) { split(H[i], t, "\t"); ip = t[2]
    if (!((ip, t[3]) in PD)) { PD[ip, t[3]] = 1; DIST[ip]++ }
    PN[ip, t[3]]++; HITS[ip]++; WN[ip, t[4]]++
    if (!(ip in BI)) { BI[ip] = 1; IPS[++nips] = ip }
    print H[i] > hitsf }
  close(hitsf)
  for (j = 1; j <= nips; j++) { ip = IPS[j]
    if ((ip in B) || DIST[ip] < traps) continue
    n = HIST[ip] + 1; paths = ""; best = ""; bw = 0
    # the three paths it asked for most, the rest by count
    for (r = 1; r <= 3; r++) { top = ""; tn = 0
      for (x in PN) { split(x, kk, SUBSEP); if (kk[1] == ip && PN[x] > tn && !((x) in USED)) { top = kk[2]; tn = PN[x]; tk = x } }
      if (top == "") break
      USED[tk] = 1; paths = paths (paths == "" ? "" : " ") top }
    for (x in WN) { split(x, kk, SUBSEP); if (kk[1] == ip && WN[x] > bw) { best = kk[2]; bw = WN[x] } }
    B[ip] = ip "\t" now "\t" (now + dur(n)) "\t" n "\t" HITS[ip] "\t" paths "\t" (DIST[ip] - 3 > 0 ? DIST[ip] - 3 : 0) "\t" best "\t" watch "\t0"
    BO[++nb] = ip; HL[++nh] = ip "\t" now
    print "B\t" ip "\t" dur(n) }
  printf "" > bansf
  for (j = 1; j <= nb; j++) { ip = BO[j]; if (!(ip in B) || (ip in DONE)) continue; DONE[ip] = 1
    split(B[ip], t, "\t")
    if (ip in SEEN) t[10] += SEEN[ip]
    print t[1] "\t" t[2] "\t" t[3] "\t" t[4] "\t" t[5] "\t" t[6] "\t" t[7] "\t" t[8] "\t" t[9] "\t" (t[10] + 0) > bansf }
  close(bansf)
  printf "" > histf
  for (i = 1; i <= nh; i++) print HL[i] > histf
  close(histf)
  for (x in SP) print "P\t" x "\t" SP[x]
}'

# The panel report: bans with what for and until when, allowed addresses that hit traps,
# addresses spared as a proxy, the mode and whether the firewall takes the bans.
REPORT='
function jl(s) { gsub(/[\\"]/, "", s); gsub(/[\001-\037]/, "", s); return s }
function arr(s,   a, n, i, o) { n = split(s, a, " "); o = ""; for (i = 1; i <= n; i++) o = o (i > 1 ? "," : "") "\"" jl(a[i]) "\""; return "[" o "]" }
BEGIN { FS = "\t"
  while ((getline l < bansf) > 0) { split(l, t, "\t"); if (t[3] <= now) continue
    nb++; if (t[2] > now - 3600) hour++; if (t[2] > now - 86400) day++
    if (nb > 500) continue
    b = b (b == "" ? "" : ",") "{\"ip\":\"" jl(t[1]) "\",\"since\":" t[2] ",\"until\":" t[3] ",\"n\":" t[4] ",\"hits\":" t[5] \
      ",\"paths\":" arr(t[6]) ",\"more\":" (t[7] + 0) ",\"where\":\"" jl(t[8]) "\",\"w\":" (t[9] + 0) ",\"seen\":" (t[10] + 0) "}" }
  close(bansf)
  # allowed addresses that hit traps in the last day: one line per address
  while ((getline l < ownf) > 0) { split(l, t, "\t"); if (t[1] <= now - 86400) continue
    ip = t[2]; if (!(ip in OL)) { OL[ip] = t[3]; ON[++no] = ip }
    OH[ip]++; OT[ip] = t[1]; if (!((ip, t[4]) in OPD)) { OPD[ip, t[4]] = 1; OD[ip]++; if (OD[ip] <= 3) OP[ip] = OP[ip] (OP[ip] == "" ? "" : " ") t[4] } }
  close(ownf)
  for (i = 1; i <= no && i <= 50; i++) { ip = ON[i]
    o = o (o == "" ? "" : ",") "{\"ip\":\"" jl(ip) "\",\"label\":\"" jl(OL[ip]) "\",\"hits\":" OH[ip] ",\"paths\":" arr(OP[ip]) ",\"more\":" (OD[ip] > 3 ? OD[ip] - 3 : 0) ",\"last\":" OT[ip] "}" }
  while ((getline l < proxyf) > 0) { split(l, t, "\t"); if (t[2] <= now || np >= 20) continue; np++
    pr = pr (pr == "" ? "" : ",") "{\"ip\":\"" jl(t[1]) "\",\"n\":" (t[3] + 0) ",\"share\":" (t[4] + 0) ",\"log\":\"" jl(t[5]) "\"}" }
  close(proxyf)
  while ((getline l < selff) > 0) { split(l, t, "\t"); if (t[1] <= now - 86400 || ns >= 20) continue; ns++
    sf = sf (sf == "" ? "" : ",") "{\"ip\":\"" jl(t[2]) "\",\"label\":\"" jl(t[3]) "\",\"ts\":" t[1] "}" }
  close(selff)
  while ((getline l < clsf) > 0) { split(l, t, "\t"); if (t[1] <= now - 3600) continue; cp += t[2]; cc += t[3]; cv += t[4] }
  close(clsf)
  printf "{\"v\":1,\"ts\":%s,\"mode\":\"%s\",\"auto\":\"%s\",\"nft\":%s,\"can\":%s,\"err\":\"%s\",\"err_since\":%s,\"logs\":%s,", now, mode, jl(auto), nft, can, jl(err), es, logs
  printf "\"cls\":{\"pub\":%.0f,\"cf\":%.0f,\"priv\":%.0f},\"active\":%.0f,\"hour\":%.0f,\"day\":%.0f,\"drops\":%s,\"allow_sha\":\"%s\",", cp, cc, cv, nb, hour, day, drops, sha
  printf "\"bans\":[%s],\"own\":[%s],\"proxy\":[%s],\"self\":[%s]}\n", b, o, pr, sf
}'

# keep the lines of a ts-first file newer than $2 seconds
prune() { [ -f "$1" ] || return 0; awk -F'\t' -v c=$(( now - $2 )) '$1 > c' "$1" > "$1.tmp" && mv -f "$1.tmp" "$1"; }

run() {
  now=$(date +%s)
  local mode auto can=false nft=false err="" drops=0 facts ip left sha lab
  mode=$(mode_now); auto=$(conf_get auto)
  touch "$D/bans.tsv" "$D/history.tsv" "$D/hits.tsv" "$D/proxy.tsv" "$D/acme.tsv" "$D/immune.tsv" "$D/own.tsv" \
    "$D/self.tsv" "$D/cls.tsv" "$D/ipcnt.tsv" "$D/offsets"
  nft_ok && nft=true
  if [ "$mode" = off ]; then
    nft_ok && nft_drop_table
    SKIP=1 collect
    LOGS=$(wc -l < "$D/offsets.new")
    mv -f "$D/offsets.new" "$D/offsets"
  else
    # "on" only bans when the table is there: otherwise the bans are recorded as watch ones,
    # and the panel says why the firewall does not take them
    if [ "$mode" = on ]; then
      : > "$D/nft.err"
      if nft_ok && nft_ensure; then can=true; else
        err=$(head -c 300 "$D/nft.err" 2>/dev/null | tr '\n' ' ')
        nft_ok || err="nft not found"
      fi
    fi
    build_allow
    facts=$(mktemp "$D/facts.XXXXXX")
    collect | awk -v allowf="$D/allow.cur" -v bansf="$D/bans.tsv" -v now="$now" -v grace="$GRACE" "$LIB$PARSE" > "$facts"
    LOGS=$(wc -l < "$D/offsets.new")
    mv -f "$D/offsets.new" "$D/offsets"
    # proxy in front: more than 1000 requests an hour and over 20% of its log
    awk -F'\t' -v now="$now" '$1 == "C" { print now "\t" $2 "\t" $3 "\t" $4 } $1 == "N" { print now "\t" $2 "\t\t" $3 }' "$facts" >> "$D/ipcnt.tsv"
    prune "$D/ipcnt.tsv" 3600
    awk -F'\t' -v now="$now" '$3 == "" { tot[$2] += $4; next } { n[$2 SUBSEP $3] += $4 }
      END { for (k in n) { split(k, a, SUBSEP); if (n[k] >= 1000 && n[k] * 5 > tot[a[1]]) printf "%s\t%s\t%.0f\t%.0f\t%s\n", a[2], now + 86400, n[k], n[k] * 100 / tot[a[1]], a[1] } }' \
      "$D/ipcnt.tsv" > "$D/proxy.new"
    awk -F'\t' -v now="$now" 'FNR == NR { seen[$1] = 1; print; next } !($1 in seen) && $2 > now' "$D/proxy.new" "$D/proxy.tsv" > "$D/proxy.tmp"
    mv -f "$D/proxy.tmp" "$D/proxy.tsv"; rm -f "$D/proxy.new"
    awk -F'\t' -v now="$now" '$1 == "A" { print $2 "\t" (now + 86400) }' "$facts" >> "$D/acme.tsv"
    awk -F'\t' -v now="$now" '$2 > now && !s[$1]++' "$D/acme.tsv" > "$D/acme.tmp" && mv -f "$D/acme.tmp" "$D/acme.tsv"
    awk -F'\t' -v now="$now" '$1 == "K" { print now "\t" $2 "\t" $3 "\t" $4 }' "$facts" >> "$D/cls.tsv"
    prune "$D/cls.tsv" 3600; prune "$D/own.tsv" 86400; prune "$D/self.tsv" 86400
    awk -F'\t' -v now="$now" '$2 > now' "$D/immune.tsv" > "$D/immune.tmp" && mv -f "$D/immune.tmp" "$D/immune.tsv"
    grep -E '^(T|S)' "$facts" | awk -v now="$now" -v window="$WINDOW" -v traps="$TRAPS" -v watch=$([ "$can" = true ] && echo 0 || echo 1) \
      -v proxyf="$D/proxy.tsv" -v acmef="$D/acme.tsv" -v immunef="$D/immune.tsv" -v histf="$D/history.tsv" \
      -v bansf="$D/bans.tsv" -v hitsf="$D/hits.tsv" -v ownf="$D/own.tsv" "$DECIDE" > "$facts.out"
    if [ "$can" = true ]; then
      while IFS="$TAB" read -r k ip left; do
          [ "$k" = B ] && { nft_add "$ip" "$left" || { can=false; err=$(head -c 300 "$D/nft.err" | tr '\n' ' '); }; }
        done < "$facts.out"
        # an address of the allow list in the sets (the list grew after the ban): out, and the panel is told
        nft_members | awk -v allowf="$D/allow.cur" "$LIB"'BEGIN { load_allow(allowf) } { l = allowed($0); if (l != "") print $0 "\t" l }' \
          | while IFS="$TAB" read -r ip lab; do
              nft_del "$ip"; printf '%s\t%s\t%s\n' "$now" "$ip" "$lab" >> "$D/self.tsv"
              awk -F'\t' -v ip="$ip" '$1 != ip' "$D/bans.tsv" > "$D/bans.tmp" && mv -f "$D/bans.tmp" "$D/bans.tsv"
            done
        drops=$(nft_drops)
        # avalanche: back to watch, bans stay recorded but leave the firewall
        if [ "$(awk -F'\t' -v c=$((now - 3600)) '$2 > c && $9 == 0' "$D/bans.tsv" | wc -l)" -ge "$AVALANCHE" ]; then
          conf_set watch "avalanche:$now"; mode=watch; auto="avalanche:$now"; nft_drop_table
        fi
    elif [ "$mode" != on ]; then
      nft_ok && nft_drop_table
    fi
    rm -f "$facts" "$facts.out"
  fi
  [ "$mode" = on ] || can=false
  # since when "on" cannot ban: the panel alerts after 10 minutes of it, not on one bad run
  if [ "$mode" = on ] && [ "$can" = false ]; then [ -s "$D/err.since" ] || echo "$now" > "$D/err.since"
  else rm -f "$D/err.since"; fi
  err_since=$(cat "$D/err.since" 2>/dev/null); err_since=${err_since:-0}
  sha=$(awk -F'\t' '{ print $1 }' "$D/panel-allow" 2>/dev/null | sort | sha256sum | cut -c1-16)
  [ -s "$D/panel-allow" ] || sha=""
  awk -v now="$now" -v mode="$mode" -v auto="$auto" -v nft="$nft" -v can="$can" -v err="$err" -v es="$err_since" -v logs="${LOGS:-0}" \
    -v drops="${drops:-0}" -v sha="$sha" -v bansf="$D/bans.tsv" -v ownf="$D/own.tsv" -v proxyf="$D/proxy.tsv" \
    -v selff="$D/self.tsv" -v clsf="$D/cls.tsv" "$REPORT" < /dev/null > "$OUT.tmp.$$" && mv -f "$OUT.tmp.$$" "$OUT"
  rm -f "$OUT.tmp.$$"
  chmod 0644 "$OUT" 2>/dev/null || true
}

# -- requests from the panel: mode, unban, its allow list --
put_res() { # id ok output
  local t
  t=$(mktemp "$RES_DIR/.res.XXXXXX") || return 0
  printf 'ok=%s\noutput=%s\n' "$2" "$3" > "$t"
  chmod 0644 "$t"; mv -fT "$t" "$RES_DIR/$1.res"
}

unban() {
  local ip=$1 v
  v=$(awk -v s="$ip" "$LIB"'BEGIN { print addr(s) }')
  [ -n "$v" ] || return 1
  nft_ok && nft_del "$v"
  awk -F'\t' -v ip="$v" '$1 != ip' "$D/bans.tsv" > "$D/bans.tmp" && mv -f "$D/bans.tmp" "$D/bans.tsv"
  # unbanned by a person: likely not a scanner - no repeat count and a day without a new ban
  awk -F'\t' -v ip="$v" '$1 != ip' "$D/history.tsv" > "$D/history.tmp" && mv -f "$D/history.tmp" "$D/history.tsv"
  printf '%s\t%s\n' "$v" "$(( $(date +%s) + 86400 ))" >> "$D/immune.tsv"
  awk -F'\t' -v ip="$v" '$2 != ip' "$D/hits.tsv" > "$D/hits.tmp" && mv -f "$D/hits.tmp" "$D/hits.tsv"
}

process_spool() {
  local req id line k v op mode ip nets out ok
  find "$RES_DIR" -maxdepth 1 -type f -mmin +10 -delete 2>/dev/null || true
  for req in "$REQ_DIR"/*.req; do
    [ -e "$req" ] || [ -L "$req" ] || continue
    id=$(basename "$req" .req)
    if [ -L "$req" ] || [ ! -f "$req" ]; then rm -f "$req"; continue; fi
    case "$id" in ''|*[!0-9-]*) rm -f "$req"; continue ;; esac
    op=""; mode=""; ip=""; nets=$(mktemp "$D/nets.XXXXXX")
    while IFS= read -r line; do
      k=${line%%=*}; v=${line#*=}
      case "$k" in
        op) op=$v ;; mode) mode=$v ;; ip) ip=$v ;;
        # net=<cidr> <label>: checked again here, the agent checked it first
        net) printf '%s\n' "$v" >> "$nets" ;;
      esac
    done < <(head -c 65536 "$req")
    rm -f "$req"
    ok=false; out="bad request"
    case "$op" in
      mode)
        case "$mode" in on|watch|off) conf_set "$mode" ""; ok=true; out="mode=$mode" ;; esac ;;
      unban)
        if unban "$ip"; then ok=true; out="unbanned $ip"; fi ;;
      allow)
        awk -v allowf=/dev/null "$LIB"'{ c = $1; lab = $0; sub(/^[^ ]+ ?/, "", lab); gsub(/[\\"\t\001-\037]/, "", lab)
            if (lab == "") lab = "panel"; if (net_add(c, "x") && n++ < 2000) print c "\t" substr(lab, 1, 40) }' "$nets" > "$D/panel-allow.tmp"
        mv -f "$D/panel-allow.tmp" "$D/panel-allow"; ok=true; out="allow=$(wc -l < "$D/panel-allow")" ;;
    esac
    rm -f "$nets"
    put_res "$id" "$ok" "$out"
  done
  run
}

exec 9>"$LOCK"
case "${1:-run}" in
  run) flock -n 9 || exit 0; run ;;
  process-spool) flock -w 60 9 || exit 0; process_spool ;;
  stop) flock -w 60 9 || true; nft_ok && nft_drop_table ;;
  *) echo "usage: $0 [run|process-spool|stop]" >&2; exit 2 ;;
esac
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-autoban.service <<EOF
[Unit]
Description=Kervax: ban web scanners on 80/443 (nftables)
[Service]
Type=oneshot
ExecStart=$HELPER run
Nice=10
TimeoutStartSec=3min
EOF
cat > /etc/systemd/system/kervax-autoban.timer <<'EOF'
[Unit]
Description=Kervax: read new web log lines for scanners every minute
[Timer]
OnBootSec=30s
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

# -- mode, unban and the allow list from the panel through the spool --
if getent group "$AGENT_USER" >/dev/null 2>&1; then
  install -d -o root -g "$AGENT_USER" -m 0730 "$REQ_DIR"
  install -d -o root -g "$AGENT_USER" -m 0750 "$RES_DIR"
  find "$RES_DIR" -mindepth 1 \( ! -type f -o ! -name '*.res' \) -exec rm -rf {} + 2>/dev/null || true
  # the agent runs under ProtectSystem=strict: let it write the spool in /var/lib/kervax
  if systemctl cat kervax-agent >/dev/null 2>&1 \
     && [ ! -f /etc/systemd/system/kervax-agent.service.d/kervax-spool.conf ]; then
    install -d -m 0755 /etc/systemd/system/kervax-agent.service.d
    printf '[Service]\nReadWritePaths=/var/lib/kervax\n' \
      > /etc/systemd/system/kervax-agent.service.d/kervax-spool.conf
    systemctl daemon-reload 2>/dev/null || true
    systemctl try-restart kervax-agent 2>/dev/null || true
  fi
  cat > /etc/systemd/system/kervax-autoban-req.service <<EOF
[Unit]
Description=Kervax: autoban mode, unban and allow list from the panel
[Service]
Type=oneshot
ExecStart=$HELPER process-spool
TimeoutStartSec=3min
EOF
  cat > /etc/systemd/system/kervax-autoban-req.path <<EOF
[Unit]
Description=Kervax: watch the autoban request spool
[Path]
DirectoryNotEmpty=$REQ_DIR
Unit=kervax-autoban-req.service
[Install]
WantedBy=multi-user.target
EOF
else
  echo "· no Kervax agent group on this node: the panel's unban and mode switch are not installed." >&2
fi

systemctl daemon-reload
systemctl enable --now kervax-autoban.timer >/dev/null 2>&1 || true
[ -d "$REQ_DIR" ] && systemctl enable --now kervax-autoban-req.path >/dev/null 2>&1 || true
systemctl start --no-block kervax-autoban.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/autoban-setup.ver"
chmod 0644 "$STATE_DIR/versions/autoban-setup.ver"
echo "✓ autoban-setup: web scanners -> nftables bans on 80/443 (mode: $(sed -n 's/^mode=//p' /etc/kervax/autoban.conf | tail -1)); report $OUT."
