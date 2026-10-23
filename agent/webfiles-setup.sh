#!/usr/bin/env bash
# Kervax: what changed in the code of the sites. A root helper finds the web roots by itself and
# every 10 minutes lists the code files in them: *.php and its kin, *.js, .htaccess and .user.ini.
# It keeps the previous list on the node and writes only the difference - new, changed and gone
# files - to /var/lib/kervax/report.d/webfiles.json. The panel tells a deploy (many files at once)
# from a single edit and alerts on new php in uploads and on code changed outside a deploy.
#
# Where the roots come from, without any setup:
# - running php-fpm, php, Apache and the like: their working directory (the official php and
#   WordPress images start in /var/www/html);
# - nginx and Apache configs: root and DocumentRoot, with the server_name next to them, so the
#   panel knows which site a root belongs to;
# - /var/www/* on the host.
# A process in a container or a pod is reached through /proc/<pid>/root: the same files the
# container sees, whatever runs it (Docker, containerd, k0s, k3s). A root ending in /public,
# /web or /pub is listed from its parent: the code of the app lives there.
#
# In a container the image is the deploy: only the upper overlay layer is listed - the files
# written in the live container - and a root is keyed by the container, so a new pod starts
# with its own list. For the first 10 minutes after a container starts its list is only taken
# (init scripts, envsubst, generated *-local.php are the start, not an edit). Caches and
# compiled templates are not listed at all, published assets (Yii web/assets and the like) -
# only their php.
#
# A deploy through git (git pull with two files) is told by the reflog: a root inside a checkout
# - in the container or, for a bind mount, on the host - sends the times of the last pull,
# checkout, merge and reset, and the panel takes the files written right before one of them for
# the deploy. The reflog is read as a file.
#
# A change is a change of content, not of the modification time: the list keeps a sha1 of every
# file (taken a part per run, changed files first), and a file written again with the same content
# (an IDE, a deploy, a plugin rewriting its own file) is sent only as "same". A changed file of a
# checkout is compared with its version in HEAD: the same content as in HEAD is not an edit, a
# different one is an uncommitted edit, a file git does not know is new or, by .gitignore, local
# (configs, vendor). From git only "git ls-tree HEAD" and "git check-ignore --no-index" are asked -
# they run no hooks, filters or fsmonitor - with transports off (no lazy fetch of a partial clone),
# no system or global config, and as the owner of the git directory: what the repository config
# could make git run, its owner could run anyway. A git directory of another user inside a
# container is not looked into.
#
# A file sent as changed stays on a watch list for 30 days and is looked at again every run: gone,
# back to the content it had before, now the same as in HEAD (committed, checked out, pulled) or a
# translation with its .mo - the panel then takes its sign off by itself. For every file sent the
# owner goes along, whether the owner is the user the web server or php runs as (that is how a web
# shell gets in) and whether the php looks like a web shell (eval of base64, a call with $_POST and
# the like, names of known shells) - the panel keeps these signs even where people edit the code by
# hand.
#
# WordPress updates itself from the admin or by wp-cron: a language pack, a plugin, a theme or
# the core. To tell that from an edit, a changed translation (languages/*.l10n.php) is sent with
# the time of the .mo next to it - a language pack writes both in the same second - and a run
# carries the versions that changed since the last one: the core from wp-includes/version.php,
# plugins and themes from the Version: line of their headers.
#
# Only paths, sizes, modification times, owners, these versions and the verdicts above leave the
# node, never file contents or their hashes.
# The helper changes nothing in the roots and keeps its own lists in /var/lib/kervax/webfiles
# (root only).
set -euo pipefail

KERVAX_SETUP_VERSION=0.6 # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_WHEN="pgrep -x nginx || pgrep -x apache2 || pgrep -x httpd || pgrep php-fpm || pgrep -x php || pgrep lsphp || pgrep frankenphp"

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-webfiles"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/webfiles.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"
install -d -m 0700 "$STATE_DIR/webfiles"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# Code files of the web roots -> /var/lib/kervax/report.d/webfiles.json. Read only.
set -u
export LC_ALL=C
OUT=/var/lib/kervax/report.d/webfiles.json
SNAP=/var/lib/kervax/webfiles
RUNS="$SNAP/runs"
MAXROOTS=80      # roots per node
MAXFULL=20       # of them listed in full (the host, containers without an overlay upper layer)
MAXLIST=15       # files of each kind per root in one run
START_WIN=600    # a container younger than this is its start: the list is only taken
MAXFILES=200000  # a bigger root is listed only up to this
KEEP=3600        # runs with changes stay in the file for an hour: the agent may have been away
GIT_KEEP=604800  # reflog entries of the last week
GIT_MAX=10       # of them, the newest
MAXOUT=60000     # bytes; the agent takes 256 KB per file and 512 KB for all helpers together
HASH_RUN=20000   # files hashed per root in one run: the list gets its hashes over a few runs
HASH_ALL=40000   # and in all roots: the first run on a big node stays far from the unit timeout
HASH_MAX=4194304 # bigger files are not hashed: their change is told by time and size
WATCH_DAYS=30    # a file sent as changed is looked at again this long
WATCH_MAX=300    # files on the watch list of a root
SEED_DAYS=14     # helper 0.5 had no watch list: files written this recently are put on it
TAB=$(printf '\t')

esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }

host_ns=$(readlink /proc/1/ns/mnt 2>/dev/null || true)
CLK=$(getconf CLK_TCK 2>/dev/null || echo 100)
BTIME=$(awk '$1 == "btime" { print $2 }' /proc/stat 2>/dev/null)

# when the process started, unix time
started() {
  awk -v b="${BTIME:-0}" -v c="$CLK" '{ sub(/.*\) /, ""); print int(b + $20 / c) }' "/proc/$1/stat" 2>/dev/null
}

# the upper overlay layer of the container root as the host sees it: what the live container wrote
upper_of() {
  awk '{ for (i = 7; i <= NF; i++) if ($i == "-") break
         if ($5 != "/" || $(i + 1) != "overlay") next
         n = split($(i + 3), o, ","); for (j = 1; j <= n; j++) if (o[j] ~ /^upperdir=/) u = substr(o[j], 10) }
       END { if (u != "") print u }' "/proc/$1/mountinfo" 2>/dev/null
}

# the git directory of the checkout a directory lies in: .git up the tree, a directory or a
# "gitdir: ..." file (a worktree); base is /proc/<pid>/root for a path inside a container.
# Prints "gitdir TAB top of the checkout"
gitdir_of() {
  local base=$1 d=$2 g i=0
  while [ "$i" -lt 12 ]; do
    if [ -d "$base$d/.git" ]; then printf '%s\t%s\n' "$base$d/.git" "$base$d"; return 0; fi
    if [ -f "$base$d/.git" ]; then
      g=$(head -c 512 "$base$d/.git" 2>/dev/null | sed -n 's/^gitdir: *//p' | head -1)
      case "$g" in
        '') ;;
        /*) printf '%s\t%s\n' "$base$g" "$base$d"; return 0 ;;
        *) printf '%s\t%s\n' "$base$d/$g" "$base$d"; return 0 ;;
      esac
    fi
    [ "$d" = / ] && return 1
    d=${d%/*}
    [ -n "$d" ] || d=/
    i=$((i + 1))
  done
  return 1
}

# a path inside a container as the host sees it, through the bind mount it lies on
host_path() {
  awk -v d="$2" '
    function under(p, m) { return m == "/" || p == m || index(p, m "/") == 1 }
    function rest(p, m) { return m == "/" ? p : substr(p, length(m) + 1) }
    FILENAME == ARGV[1] { if (under(d, $5) && length($5) > length(mp)) { mp = $5; dev = $3; src = $4 }; next }
    mp != "/" && $3 == dev && under(src, $4) && (hm == "" || length($4) > length(hr)) { hr = $4; hm = $5 }
    END {
      if (hm == "") exit
      out = hm "/" rest(src, hr) "/" rest(d, mp); gsub(/\/+/, "/", out)
      if (length(out) > 1) sub(/\/$/, "", out)
      print out
    }' "/proc/$1/mountinfo" /proc/1/mountinfo 2>/dev/null
}

# GEV: [ts,"kind"] of the last reflog entries of HEAD that put code in place, newest last; one
# read per repository
declare -A GITEV=()
git_events() {
  local f=$1/logs/HEAD
  GEV=""
  [ -f "$f" ] || return 0
  if [ -z "${GITEV[$f]+x}" ]; then
    GITEV[$f]=$(tail -c 65536 "$f" 2>/dev/null | awk -F '\t' -v min="$((now - GIT_KEEP))" -v max="$GIT_MAX" '
      { n = split($1, a, " "); ts = a[n - 1]; split($2, m, /[ :(]/); k = m[1] }
      ts ~ /^[0-9]+$/ && ts + 0 >= min && k ~ /^(pull|checkout|merge|reset|rebase|commit|clone|cherry-pick|revert|am)$/ {
        e[++c] = "[" ts ",\"" k "\"]"
      }
      END { s = c > max ? c - max + 1 : 1; for (i = s; i <= c; i++) printf "%s%s", (i > s ? "," : ""), e[i] }')
  fi
  GEV=${GITEV[$f]}
}

# the branch checked out: from HEAD as a file, empty when detached
git_branch() {
  head -c 256 "$1/HEAD" 2>/dev/null | sed -n 's|^ref: *refs/heads/||p' | head -1 | tr -cd 'A-Za-z0-9._/+-' | head -c 80
}

# GST[path from the root]: same (as in HEAD), dirty (not as in HEAD), new (git does not know it)
# or ignored (by .gitignore: local configs, vendor). Arguments: git dir, path prefix of the root
# in the checkout, root, top of the checkout, file with paths
declare -A GST=()
git_check() {
  local gd=$1 pre=$2 root=$3 top=$4 list=$5 cdir=$1 uid gid f h t gcfg
  local -a run=() paths=() untracked=()
  local -A tree=()
  GST=()
  command -v git >/dev/null 2>&1 && command -v sha1sum >/dev/null 2>&1 || return 0
  [ -s "$list" ] && [ -f "$gd/HEAD" ] || return 0
  # a linked worktree keeps the config in the common git directory
  if [ -f "$gd/commondir" ]; then
    f=$(head -c 512 "$gd/commondir" 2>/dev/null | head -1)
    case "$f" in /*) cdir=$f ;; ?*) cdir=$gd/$f ;; esac
    case "$gd" in /proc/[0-9]*/root/*) [ "${f#/}" != "$f" ] && cdir=${gd%%/root/*}/root$f ;; esac
  fi
  # SHA-256 repositories: sha1sum of the file means nothing there
  grep -qi 'objectformat' "$cdir/config" 2>/dev/null && return 0
  uid=$(stat -c %u "$gd" 2>/dev/null) || return 0
  gid=$(stat -c %g "$gd" 2>/dev/null) || return 0
  for f in "$cdir" "$cdir/config"; do
    if [ -e "$f" ] && [ "$(stat -c %u "$f" 2>/dev/null)" != "$uid" ]; then return 0; fi
  done
  if [ "$uid" != 0 ]; then
    case "$gd" in /proc/*) return 0 ;; esac
    command -v setpriv >/dev/null 2>&1 || return 0
    run=(setpriv --reuid="$uid" --regid="$gid" --clear-groups)
  fi
  while IFS= read -r f; do paths+=("$pre$f"); done < "$list"
  printf '[safe]\n\tdirectory = *\n' > "$work/gitconfig"
  gcfg=$([ "$uid" = 0 ] && echo "$work/gitconfig" || echo /dev/null)
  # "mode type blob TAB path", NUL-terminated: paths come as they are
  while IFS="$TAB" read -r -d '' h f; do
    tree[$f]=${h##* }
  done < <(cd / && timeout 30 env -i PATH="$PATH" HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 \
             GIT_CONFIG_GLOBAL="$gcfg" GIT_TERMINAL_PROMPT=0 \
             "${run[@]}" git -c safe.directory='*' -c protocol.allow=never --literal-pathspecs --git-dir="$gd" \
             ls-tree -z --full-tree HEAD -- "${paths[@]}" 2>/dev/null)
  while IFS= read -r f; do
    [ -f "$root/$f" ] || continue
    t=${tree[$pre$f]:-}
    if [ -z "$t" ]; then untracked+=("$pre$f"); continue; fi
    h=$({ printf 'blob %s\0' "$(stat -c %s "$root/$f")"; cat "$root/$f"; } 2>/dev/null | sha1sum | cut -c1-40)
    if [ "$h" = "$t" ]; then GST[$f]=same; else GST[$f]=dirty; fi
  done < "$list"
  [ "${#untracked[@]}" -gt 0 ] && [ -d "$top" ] || return 0
  for f in "${untracked[@]}"; do GST[${f#"$pre"}]=new; done
  # .gitignore of the checkout only: no index (no fsmonitor, no untracked cache), no global excludes.
  # check-ignore takes no literal pathspecs: a path given as "./path" is not read as pathspec magic
  while IFS= read -r -d '' f; do
    f=${f#./}
    [ -n "${GST[${f#"$pre"}]+x}" ] && GST[${f#"$pre"}]=ignored
  done < <(cd "$top" && printf './%s\0' "${untracked[@]}" | timeout 30 env -i PATH="$PATH" HOME=/nonexistent \
             GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$gcfg" GIT_TERMINAL_PROMPT=0 \
             "${run[@]}" git -c safe.directory='*' -c protocol.allow=never -c core.fsmonitor= \
             -c core.excludesFile=/dev/null --git-dir="$gd" --work-tree=. \
             check-ignore --no-index -z --stdin 2>/dev/null)
}

# WEBALL: " uid uid " the web servers and php of the node run as (not root): a file of this owner
# was written by a site itself. One list for the node: php-fpm writes into a directory nginx of
# another container serves
WEBALL=" "
web_uids() {
  local d comm u
  for d in /proc/[0-9]*; do
    comm=$(cat "$d/comm" 2>/dev/null) || continue
    case "$comm" in
      php|php[0-9]*|php-fpm*|php-cgi*|lsphp*|frankenphp|apache2|httpd|nginx|litespeed|openlitespeed) ;;
      *) continue ;;
    esac
    u=$(awk '$1 == "Uid:" { print $2; exit }' "$d/status" 2>/dev/null)
    [ -n "$u" ] && [ "$u" != 0 ] || continue
    case "$WEBALL" in *" $u "*) ;; *) WEBALL="$WEBALL$u " ;; esac
  done
}

# OWN[path from the root]: "owner TAB 1 when the owner is the web user TAB 1 when the php looks
# like a web shell". Arguments: root, passwd to read names from, web uids, file with paths
declare -A OWN=()
owners_of() {
  local root=$1 pw=$2 web=$3 list=$4 f u n w
  local -A name=() sus=()
  OWN=()
  [ -s "$list" ] || return 0
  while IFS=: read -r n _x u _rest; do
    [ -n "$u" ] && [ -z "${name[$u]+x}" ] && name[$u]=$(printf '%s' "$n" | tr -cd 'A-Za-z0-9._-' | head -c 32)
  done < <(head -c 262144 "$pw" 2>/dev/null)
  # web shells: code run from the request, eval of an encoded string, the names of known shells
  while IFS= read -r -d '' f; do sus[$f]=1; done < <(cd "$root" 2>/dev/null && awk '/\.(php[3-8]?|phtml|pht|phar)$/' "$list" \
    | while IFS= read -r f; do [ -f "$f" ] && [ "$(stat -c %s "$f" 2>/dev/null || echo 0)" -le "$HASH_MAX" ] && printf '%s\0' "$f"; done \
    | timeout 60 xargs -0 -r grep -l -Z -a -s -E \
        -e '(eval|assert)[[:space:]]*\([[:space:]]*@?[[:space:]]*(base64_decode|gzinflate|gzuncompress|gzdecode|str_rot13|strrev|rawurldecode|hex2bin)[[:space:]]*\(' \
        -e '(eval|assert|system|passthru|shell_exec|exec|popen|proc_open|pcntl_exec)[[:space:]]*\([[:space:]]*@?[[:space:]]*\$_(GET|POST|REQUEST|COOKIE|SERVER|FILES)' \
        -e '\$_(GET|POST|REQUEST|COOKIE)[[:space:]]*\[[^]]{1,40}\][[:space:]]*\([[:space:]]*@?[[:space:]]*\$_(GET|POST|REQUEST|COOKIE)' \
        -e '(FilesMan|b374k|c99shell|r57shell|IndoXploit|AnonymousFox|Yanz Webshell)' -- 2>/dev/null)
  while IFS= read -r f; do
    u=$(stat -c %u "$root/$f" 2>/dev/null) || continue
    n=${name[$u]:-$u}
    w=0
    case "$web" in *" $u "*) w=1 ;; esac
    OWN[$f]="$n$TAB$w$TAB${sus[$f]:-0}"
  done < "$list"
}

# sha1 of the files on a list (paths from the root) -> "path TAB sha1"
hashes() {
  (cd "$1" 2>/dev/null && tr '\n' '\0' < "$2" | timeout 300 nice -n 15 xargs -0 -r sha1sum -- 2>/dev/null) \
    | awk '!/^\\/ { print substr($0, 43) "\t" substr($0, 1, 40) }'
}

# Docker container id -> name, for "where" in the panel
declare -A DNAME=()
if command -v docker >/dev/null 2>&1; then
  while read -r id name; do
    [ -n "$id" ] && DNAME[$id]=$name
  done < <(timeout 20 docker ps --no-trunc --format '{{.ID}} {{.Names}}' 2>/dev/null || true)
fi

# pid -> host, the Docker name or the container hostname (the pod name in Kubernetes, with its
# namespace when the service account is mounted: "tech1/backend-7fc48994cb-7kqmt")
where_of() {
  local pid=$1 ns id h kns
  ns=$(readlink "/proc/$pid/ns/mnt" 2>/dev/null || true)
  if [ "$ns" = "$host_ns" ]; then echo host; return; fi
  id=$(grep -oE '[0-9a-f]{64}' "/proc/$pid/cgroup" 2>/dev/null | head -1 || true)
  if [ -n "$id" ] && [ -n "${DNAME[$id]:-}" ]; then echo "${DNAME[$id]}"; return; fi
  h=$(head -c 120 "/proc/$pid/root/etc/hostname" 2>/dev/null | tr -d '\n' || true)
  kns=$(head -c 64 "/proc/$pid/root/var/run/secrets/kubernetes.io/serviceaccount/namespace" 2>/dev/null | tr -cd 'a-z0-9-' || true)
  if [ -n "$h" ] && [ -n "$kns" ]; then echo "$kns/$h"; return; fi
  echo "${h:-${id:0:12}}"
}

# "pid comm" of the processes that serve sites, one per mount namespace and name
procs() {
  local d pid comm ns seen=" "
  for d in /proc/[0-9]*; do
    comm=$(cat "$d/comm" 2>/dev/null) || continue
    case "$comm" in
      php|php[0-9]*|php-fpm*|php-cgi*|lsphp*|frankenphp|apache2|httpd|nginx|litespeed|openlitespeed) ;;
      *) continue ;;
    esac
    pid=${d#/proc/}
    ns=$(readlink "$d/ns/mnt" 2>/dev/null) || continue
    case "$seen" in *" $ns/$comm "*) continue ;; esac
    seen="$seen$ns/$comm "
    printf '%s %s\n' "$pid" "$comm"
  done
}

# root directives of nginx or Apache as the process sees them: "pid TAB dir TAB names"
conf_roots() {
  local pid=$1 kind=$2 base=/proc/$1/root d
  shift 2
  for d in "$@"; do
    [ -d "$base$d" ] || continue
    find "$base$d" -maxdepth 4 -type f \( -name '*.conf' -o -path '*/sites-enabled/*' -o -path '*/conf.d/*' \) 2>/dev/null
  done | sort -u | head -500 | while IFS= read -r f; do cat -- "$f" 2>/dev/null; echo; done \
    | awk -v pid="$pid" -v kind="$kind" '
      { sub(/#.*/, ""); gsub(/;/, " "); gsub(/"/, "") }
      kind == "nginx" && ($1 == "server" || $1 == "server{") { names = "" }
      kind == "nginx" && $1 == "server_name" { for (i = 2; i <= NF; i++) names = names " " $i }
      kind == "nginx" && $1 == "root" && NF >= 2 && $2 !~ /\$/ { print pid "\t" $2 "\t" names }
      kind == "apache" && tolower($1) ~ /^<virtualhost/ { names = "" }
      kind == "apache" && (tolower($1) == "servername" || tolower($1) == "serveralias") {
        for (i = 2; i <= NF; i++) names = names " " $i
      }
      kind == "apache" && tolower($1) == "documentroot" && NF >= 2 && $2 !~ /\$/ { print pid "\t" $2 "\t" names }'
}

# candidates "pid TAB dir TAB names": working directories, config roots, /var/www/* on the host
cands() {
  local pid comm cwd d
  while read -r pid comm; do
    case "$comm" in
      nginx) conf_roots "$pid" nginx /etc/nginx || true ;;
      apache2|httpd) conf_roots "$pid" apache /etc/apache2 /etc/httpd || true ;;
    esac
    case "$comm" in nginx) continue ;; esac
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null) || continue
    printf '%s\t%s\t\n' "$pid" "$cwd"
  done < <(procs)
  for d in /var/www/*/ /srv/www/*/; do
    [ -d "$d" ] && printf '1\t%s\t\n' "${d%/}"
  done
}

# the directory to list as the host sees it, or nothing when it is not a web root
scan_path() {
  local pid=$1 d=$2 p
  case "$d" in */public|*/web|*/pub) d=${d%/*} ;; esac
  case "$d" in
    ''|/|/proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/usr|/usr/*|/etc|/etc/*|/bin|/bin/*|/sbin|/sbin/*|/lib*|/root|/tmp|/tmp/*|/run|/run/*|/var|/var/lib|/var/lib/*|/var/log|/var/log/*|/home) return 0 ;;
  esac
  if [ "$(readlink "/proc/$pid/ns/mnt" 2>/dev/null)" = "$host_ns" ]; then p=$d; else p=/proc/$pid/root$d; fi
  [ -d "$p" ] || return 0
  # a web root has php near the top
  find "$p" -maxdepth 2 -type f -name '*.php' -print -quit 2>/dev/null | grep -q . || return 0
  printf '%s\n' "$p"
}

# "path TAB mtime TAB size" of the code files, sorted by path. Caches and compiled templates
# (php too) are skipped, published assets are listed only for php - the app writes them itself
listing() {
  timeout 300 nice -n 15 find "$1" \
    \( -name .git -o -name node_modules -o -name cache -o -name .cache -o -name managed_cache \
       -o -name stack_cache -o -name templates_c -o -path '*/storage/framework' -o -path '*/var/cache' \
       -o -path '*/bootstrap/cache' -o -path '*/runtime' -o -path '*/files/php/twig' \
       -o -path '*/var/view_preprocessed' \) -prune \
    -o -type f \( -name '*.php' -o -name '*.php[3-8]' -o -name '*.phtml' -o -name '*.phar' -o -name '*.pht' \
       -o \( -name '*.js' -o -name .htaccess -o -name .user.ini \) ! -path '*/web/assets/*' \
          ! -path '*/public/assets/*' ! -path '*/pub/static/*' ! -path '*/web/cpresources/*' \) \
    -printf '%P\t%T@\t%s\n' 2>/dev/null \
    | head -n "$MAXFILES" | awk -F '\t' '{ sub(/\..*/, "", $2); printf "%s\t%s\t%s\n", $1, $2, $3 }' \
    | sort -t "$TAB" -k1,1
}

# JSON array of up to MAXLIST lines "kind TAB path TAB mtime TAB size [TAB mtime of the .mo]" of one kind
jlist() {
  awk -F '\t' -v k="$1" -v max="$MAXLIST" '
    function e(s) { gsub(/[\000-\037]/, "", s); gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); return s }
    $1 == k && n < max { printf "%s[\"%s\",%s,%s%s]", (n++ ? "," : ""), e($2), ($3 == "" ? 0 : $3), ($4 == "" ? 0 : $4),
                                ($5 ~ /^[0-9]+$/ ? "," $5 : "") }' "$2"
}

# WordPress in the root: "kind TAB slug TAB version TAB path prefix", kind is core, plugin or
# theme. Only the first lines of plugin and theme headers are looked at, for the Version: line
wp_versions() {
  local p=$1 v w="" c rel
  for v in "$p/wp-includes/version.php" "$p"/*/wp-includes/version.php "$p/web/wp/wp-includes/version.php"; do
    if [ -f "$v" ]; then w=${v%/wp-includes/version.php}; break; fi
  done
  [ -n "$w" ] || return 0
  rel=${w#"$p"}; rel=${rel#/}; rel=${rel:+$rel/}
  v=$(awk -F "'" '/^\$wp_version *=/ { print $2; exit }' "$w/wp-includes/version.php" 2>/dev/null | tr -cd 'A-Za-z0-9._+-' | head -c 30)
  printf 'core\t\t%s\t%s\n' "$v" "$rel"
  c=$w/wp-content
  [ -d "$c" ] || c=${w%/*}/app  # Bedrock: web/wp and web/app
  [ -d "$c" ] || return 0
  rel=${c#"$p"}; rel=${rel#/}/
  { find "$c/plugins" -mindepth 1 -maxdepth 2 -type f -name '*.php' 2>/dev/null | head -2000
    find "$c/themes" -mindepth 2 -maxdepth 2 -type f -name style.css 2>/dev/null | head -500
  } | tr '\n' '\0' | xargs -0 -r awk -v c="$c" -v rel="$rel" '
    FNR == 1 { f = FILENAME }
    FNR <= 60 && /(Plugin|Theme) Name:/ { named[f] = 1 }
    FNR <= 60 && !(f in ver) && /^[ \t\/*#@]*Version:/ {
      v = $0; sub(/.*Version:[ \t]*/, "", v); gsub(/[^A-Za-z0-9._+-]/, "", v); ver[f] = substr(v, 1, 30)
    }
    END {
      for (f in ver) {
        if (!(f in named) || ver[f] == "") continue
        r = substr(f, length(c) + 2); n = split(r, a, "/")
        if (a[1] == "themes") print "theme\t" a[2] "\t" ver[f] "\t" rel "themes/" a[2] "/"
        else if (n == 3) print "plugin\t" a[2] "\t" ver[f] "\t" rel "plugins/" a[2] "/"
        else { s = a[2]; sub(/\.php$/, "", s); print "plugin\t" s "\t" ver[f] "\t" rel r }
      }
    }' 2>/dev/null | sort -t "$TAB" -k1,2 | awk -F '\t' '!seen[$1 FS $2]++'
}

now=$(date +%s)
web_uids
find "${OUT%/*}" -maxdepth 1 -name "${OUT##*/}.??????" -mmin +30 -delete 2>/dev/null || true
work=$(mktemp -d /tmp/kervax-webfiles.XXXXXX)
trap 'rm -rf "$work"' EXIT

roots="" deltas="" n=0 nfull=0 nhashed=0 seen=" "
while IFS="$TAB" read -r pid dir names; do
  [ "$n" -lt "$MAXROOTS" ] || break
  p=$(scan_path "$pid" "$dir") || continue
  [ -n "$p" ] || continue
  inner=${p#/proc/"$pid"/root}
  # in a container: its id for the key, the upper layer to list and whether it has just started
  cid="" upper="" young=false
  if [ "$p" != "$inner" ]; then
    cid=$(grep -oE '[0-9a-f]{64}' "/proc/$pid/cgroup" 2>/dev/null | head -1 || true)
    up=$(upper_of "$pid")
    if [ -n "$up" ] && [ -d "$up" ] && [ "$(stat -c %d "$p" 2>/dev/null)" = "$(stat -c %d "/proc/$pid/root/" 2>/dev/null)" ]; then
      upper=$up$inner
    fi
    st=$(started "$pid")
    [ -n "$st" ] && [ "$((now - st))" -lt "$START_WIN" ] && young=true
  fi
  if [ -n "$cid" ]; then
    key="c${cid:0:12}-$(stat -L -c %i "$p" 2>/dev/null)" || continue
  else
    key=$(stat -L -c '%d-%i' "$p" 2>/dev/null) || continue
  fi
  case "$seen" in *" $key "*) continue ;; esac
  if [ -z "$upper" ]; then
    [ "$nfull" -lt "$MAXFULL" ] || continue
    nfull=$((nfull + 1))
  fi
  seen="$seen$key "
  n=$((n + 1))
  where=$(where_of "$pid")
  [ "$pid" = 1 ] && where=host
  layer=false
  if [ -n "$upper" ]; then
    layer=true
    if [ -d "$upper" ]; then listing "$upper" > "$work/new"; else : > "$work/new"; fi
  else
    listing "$p" > "$work/new"
  fi
  files=$(wc -l < "$work/new")
  big=false
  [ "$files" -ge "$MAXFILES" ] && big=true
  # the reflog of the checkout: in the container, else on the host behind the bind mount
  # gbase - the root as seen next to the checkout top, for paths in the checkout
  gbase=$p
  if [ "$p" = "$inner" ]; then
    gt=$(gitdir_of "" "$p" || true)
  else
    gt=$(gitdir_of "/proc/$pid/root" "$inner" || true)
    if [ -z "$gt" ]; then
      hp=$(host_path "$pid" "$inner")
      [ -n "$hp" ] && gt=$(gitdir_of "" "$hp" || true) && gbase=$hp
    fi
  fi
  gd=${gt%%"$TAB"*} gtop=${gt#*"$TAB"} gpre="" branch=""
  case "$gbase/" in
    "${gtop%/}/"*) gpre=${gbase#"${gtop%/}"}; gpre=${gpre#/}; gpre=${gpre:+$gpre/} ;;
    *) gtop="" ;;
  esac
  GEV=""
  [ -n "$gd" ] && git_events "$gd" && branch=$(git_branch "$gd")
  # the same directory mounted into several containers (php-fpm and a worker of one project)
  src=""
  [ "$layer" = false ] && src=$(stat -L -c '%d-%i' "$p" 2>/dev/null || true)
  # WordPress: versions now, the core one goes to the panel with the root
  wp_versions "$p" > "$work/wp"
  wpv=$(awk -F '\t' '$1 == "core" { print $3; exit }' "$work/wp")
  doms=$(printf '%s\n' "$names" | tr ' ' '\n' | grep -E '^[A-Za-z0-9*][A-Za-z0-9.*-]*\.[A-Za-z]{2,}$' | sort -u | head -20 \
    | awk '{ printf "%s\"%s\"", (NR > 1 ? "," : ""), $0 }')
  snap="$SNAP/$key.lst" wf="$SNAP/$key.watch" lroot=$p
  [ -n "$upper" ] && lroot=$upper
  old=$snap
  [ -e "$snap" ] || { : > "$work/none"; old=$work/none; }
  # hashes of the files unchanged since the last run are carried over; the changed and new ones
  # are hashed first, then a part of those without a hash yet (helper 0.5 lists had none)
  awk -F '\t' -v OFS='\t' 'FILENAME == ARGV[1] { o[$1] = $2 FS $3; h[$1] = $4; next }
    { if (($1 in o) && o[$1] == $2 FS $3) print $1, $2, $3, h[$1], ""; else print $1, $2, $3, "", 1 }' \
    "$old" "$work/new" > "$work/cur0"
  # the changed ones always (up to HASH_RUN), the rest while the run has HASH_ALL left
  left=$((HASH_ALL - nhashed))
  awk -F '\t' -v max="$HASH_MAX" -v left="$left" '$4 == "" && $3 <= max && ($5 == 1 || left > 0) {
      print ($5 == 1 ? 0 : 1) "\t" $1; if ($5 != 1) left--
    }' "$work/cur0" | sort -s -t "$TAB" -k1,1n | head -n "$HASH_RUN" | cut -f2- > "$work/tohash"
  nhashed=$((nhashed + $(wc -l < "$work/tohash")))
  hashes "$lroot" "$work/tohash" > "$work/hashed"
  awk -F '\t' -v OFS='\t' 'FILENAME == ARGV[1] { h[$1] = $2; next } { print $1, $2, $3, ($1 in h ? h[$1] : $4) }' \
    "$work/hashed" "$work/cur0" > "$work/cur"
  : > "$work/diff"
  : > "$work/listed"
  nn=0 nc=0 ng=0 ns=0 wpup=""
  if [ ! -e "$snap" ] || [ "$young" = true ]; then
    deltas="$deltas${deltas:+,}{\"key\":\"$key\",\"first\":true}"
  else
    # versions changed since the last run: ["plugin","slug","old","new","path prefix"]
    wpup=""
    if [ -s "$work/wp" ] && [ -f "$SNAP/$key.wp" ]; then
      wpup=$(awk -F '\t' '
        function e(s) { gsub(/[\000-\037"\\]/, "", s); return s }
        FILENAME == ARGV[1] { o[$1 FS $2] = $3; next }
        ($1 FS $2) in o && o[$1 FS $2] != $3 && o[$1 FS $2] != "" && $3 != "" {
          printf "%s[\"%s\",\"%s\",\"%s\",\"%s\",\"%s\"]", (n++ ? "," : ""), $1, e($2), o[$1 FS $2], $3, e($4)
        }' "$SNAP/$key.wp" "$work/wp" | head -c 4000)
    fi
    # N new, C changed, S written again with the same content, G gone
    awk -F '\t' 'FILENAME == ARGV[1] { o[$1] = $2 FS $3; oh[$1] = $4; next }
      { if (!($1 in o)) print "N\t" $1 "\t" $2 "\t" $3
        else { if (o[$1] != $2 FS $3) print ((oh[$1] != "" && $4 == oh[$1]) ? "S" : "C") "\t" $1 "\t" $2 "\t" $3; delete o[$1] } }
      END { for (k in o) print "G\t" k }' "$snap" "$work/cur" > "$work/diff"
    nn=$(grep -c '^N' "$work/diff" || true)
    nc=$(grep -c '^C' "$work/diff" || true)
    ng=$(grep -c '^G' "$work/diff" || true)
    ns=$(grep -c '^S' "$work/diff" || true)
    # a translation goes with the time of its .mo: a language pack writes them in one second
    if grep -q "^[NC]$TAB.*languages/.*\.l10n\.php$TAB" "$work/diff"; then
      while IFS= read -r line; do
        f=${line#?"$TAB"}; f=${f%%"$TAB"*}
        case "$line" in
          [NC]"$TAB"*languages/*.l10n.php"$TAB"*)
            printf '%s\t%s\n' "$line" "$(stat -c %Y "$p/${f%.l10n.php}.mo" 2>/dev/null || true)" ;;
          *) printf '%s\n' "$line" ;;
        esac
      done < "$work/diff" > "$work/diff2" && mv -f "$work/diff2" "$work/diff"
    fi
    # the files sent to the panel, as jlist takes them
    awk -F '\t' -v max="$MAXLIST" '($1 == "N" || $1 == "C") && c[$1]++ < max { print $2 }' "$work/diff" > "$work/listed"
  fi
  # the watch list "path TAB hash before TAB added TAB verdict TAB when": the files sent go on it
  # with the hash they had before, a file sent again keeps its first one
  clean=""
  if [ -e "$snap" ]; then
    if [ -e "$wf" ]; then
      cp -f "$wf" "$work/watch0"
    elif [ "$young" = true ]; then
      : > "$work/watch0"
    else
      # signs of helper 0.5 are files written apart: five and more in one second are a deploy or an
      # update (2500 files of plugin updates in two weeks are not rare), they would crowd them out
      awk -F '\t' -v min="$((now - SEED_DAYS * 86400))" 'NR == FNR { if ($2 >= min) c[$2]++; next }
        $2 >= min && c[$2] < 5' "$work/cur" "$work/cur" | sort -t "$TAB" -k2,2nr | head -n "$WATCH_MAX" \
        | awk -F '\t' -v now="$now" '{ print $1 "\t\t" now "\t\t" }' > "$work/watch0"
    fi
    awk -F '\t' -v OFS='\t' -v now="$now" '
      FILENAME == ARGV[1] { oh[$1] = $4; next }
      FILENAME == ARGV[2] { want[$1] = 1; next }
      { if (($1 in want) && $4 != "") next; if ($1 in want) has[$1] = 1; print }
      END { for (f in want) if (!(f in has)) print f, oh[f], now, "", "" }' "$snap" "$work/listed" "$work/watch0" > "$work/watch1"
    # open entries with the hash and time now ("-" - not in the list any more, "." - no hash: read
    # splits on runs of tabs)
    awk -F '\t' -v OFS='\t' 'FILENAME == ARGV[1] { if ($4 == "") w[$1] = $2; next }
      ($1 in w) { print $1, (w[$1] == "" ? "." : w[$1]), ($4 == "" ? "." : $4), $2; seen[$1] = 1 }
      END { for (f in w) if (!(f in seen)) print f, (w[f] == "" ? "." : w[f]), "-", 0 }' "$work/watch1" "$work/cur" > "$work/wcur"
  else
    : > "$work/watch1"
    : > "$work/wcur"
  fi
  # git: the files sent and those watched - as in HEAD or not, new or in .gitignore
  GST=()
  if [ -n "$gd" ] && [ -n "$gtop" ]; then
    cut -f1 "$work/wcur" | cat "$work/listed" - | sort -u > "$work/gitfiles"
    git_check "$gd" "$gpre" "$p" "$gtop" "$work/gitfiles"
  fi
  # a watched file is done with: gone, back as it was, as in HEAD, a translation with its .mo
  : > "$work/verd"
  while IFS="$TAB" read -r f h0 h t; do
    v=""
    # a file removed in a container leaves a whiteout in the upper layer: not a regular file
    if [ "$h" = - ] && [ ! -f "$lroot/$f" ]; then v=gone
    elif [ "$h0" != . ] && [ "$h" = "$h0" ]; then v=back
    elif [ "${GST[$f]:-}" = same ]; then v=head
    else
      case "$f" in
        *languages/*.l10n.php)
          mo=$(stat -c %Y "$p/${f%.l10n.php}.mo" 2>/dev/null || echo 0)
          [ "$t" -gt 0 ] && [ "$((mo - t))" -le 2 ] && [ "$((t - mo))" -le 2 ] && v=wplang ;;
      esac
    fi
    [ -n "$v" ] && printf '%s\t%s\n' "$f" "$v" >> "$work/verd"
  done < "$work/wcur"
  if [ -e "$snap" ]; then
    awk -F '\t' -v OFS='\t' -v now="$now" -v wd="$((WATCH_DAYS * 86400))" 'FILENAME == ARGV[1] { v[$1] = $2; next }
      $4 == "" && ($1 in v) { $4 = v[$1]; $5 = now }
      ($4 != "" && now - $5 > 86400) || ($4 == "" && now - $3 > wd) { next }
      { print }' "$work/verd" "$work/watch1" | sort -t "$TAB" -k3,3nr | head -n "$WATCH_MAX" > "$wf.tmp" && mv -f "$wf.tmp" "$wf"
    # done with in the last day: the panel takes their signs off
    clean=$(awk -F '\t' 'function e(s) { gsub(/[\000-\037]/, "", s); gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); return s }
      $4 != "" && n < 40 { printf "%s[\"%s\",\"%s\"]", (n++ ? "," : ""), e($1), $4 }' "$wf")
  fi
  if [ "$((nn + nc + ng + ns))" -gt 0 ] || [ -n "$wpup" ]; then
    # owner of each file sent, whether the site wrote it, whether it looks like a web shell
    pw=/etc/passwd
    [ "$p" != "$inner" ] && pw=/proc/$pid/root/etc/passwd
    owners_of "$lroot" "$pw" "$WEBALL" "$work/listed"
    finfo=""
    while IFS= read -r f; do
      o=${OWN[$f]:-} g=${GST[$f]:-}
      [ -n "$o$g" ] || continue
      on="" ow=0 ox=0
      [ -n "$o" ] && IFS="$TAB" read -r on ow ox <<< "$o"
      finfo="$finfo${finfo:+,}\"$(esc "$f")\":[\"$g\",\"$(esc "$on")\",${ow:-0},${ox:-0}]"
    done < "$work/listed"
    [ -n "$finfo" ] && finfo=",\"f\":{$finfo}"
    deltas="$deltas${deltas:+,}{\"key\":\"$key\",\"n_new\":$nn,\"n_changed\":$nc,\"n_gone\":$ng,\"n_same\":$ns,\"new\":[$(jlist N "$work/diff")],\"changed\":[$(jlist C "$work/diff")],\"gone\":[$(jlist G "$work/diff")],\"same\":[$(jlist S "$work/diff")]${wpup:+,\"wp\":[$wpup]}$finfo}"
  fi
  roots="$roots${roots:+,}{\"key\":\"$key\",\"where\":\"$(esc "$where")\",\"path\":\"$(esc "$inner")\",\"domains\":[$doms],\"files\":$files,\"big\":$big,\"layer\":$layer${src:+,\"src\":\"$src\"}${GEV:+,\"git\":[$GEV]}${branch:+,\"branch\":\"$branch\"}${wpv:+,\"wp\":\"$wpv\"}${clean:+,\"clean\":[$clean]}}"
  cp -f "$work/cur" "$snap.tmp" && mv -f "$snap.tmp" "$snap"
  [ -e "$wf" ] || : > "$wf"
  cp -f "$work/wp" "$SNAP/$key.wp.tmp" && mv -f "$SNAP/$key.wp.tmp" "$SNAP/$key.wp"
done < <(cands | awk -F '\t' '!seen[$1 FS $2]++')

# lists of roots that are gone for a week, of containers - for a day
find "$SNAP" -maxdepth 1 \( -name '*.lst' -o -name '*.wp' -o -name '*.watch' \) -mtime +7 -delete 2>/dev/null || true
find "$SNAP" -maxdepth 1 \( -name 'c*.lst' -o -name 'c*.wp' -o -name 'c*.watch' \) -mtime +0 -delete 2>/dev/null || true

# runs with changes of the last hour, newest last
touch "$RUNS"
if [ -n "$deltas" ]; then printf '%s\t{"ts":%s,"roots":[%s]}\n' "$now" "$now" "$deltas" >> "$RUNS"; fi
awk -F '\t' -v min="$((now - KEEP))" '$1 >= min' "$RUNS" | tail -6 > "$RUNS.tmp" && mv -f "$RUNS.tmp" "$RUNS"
while :; do
  runs=$(cut -f2- "$RUNS" | paste -sd, -)
  json=$(printf '{"v":1,"ts":%s,"roots":[%s],"runs":[%s]}' "$now" "$roots" "$runs")
  [ "${#json}" -le "$MAXOUT" ] && break
  [ "$(wc -l < "$RUNS")" -le 1 ] && { json=$(printf '{"v":1,"ts":%s,"roots":[%s],"runs":[]}' "$now" "$roots"); break; }
  tail -n +2 "$RUNS" > "$RUNS.tmp" && mv -f "$RUNS.tmp" "$RUNS"
done
tmp=$(mktemp "${OUT}.XXXXXX")
printf '%s\n' "$json" > "$tmp"
chmod 0644 "$tmp"
mv -f "$tmp" "$OUT"
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-webfiles.service <<EOF
[Unit]
Description=Kervax: new and changed code files in the web roots (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=15
IOSchedulingClass=idle
TimeoutStartSec=10min
IgnoreSIGPIPE=no
EOF
cat > /etc/systemd/system/kervax-webfiles.timer <<'EOF'
[Unit]
Description=Kervax: code files of the web roots every 10 minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-webfiles.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-webfiles.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/webfiles-setup.ver"
chmod 0644 "$STATE_DIR/versions/webfiles-setup.ver"
echo "webfiles-setup: code files of the web roots (new, changed, gone) -> $OUT (every 10 minutes)."
