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
# A changed file of a checkout is compared with its version in HEAD: the same content (an IDE or
# a tool in the container wrote it again) is not an edit, a different one is an uncommitted edit
# in the working copy, a file git does not know stays a file outside git. The hash of the file is
# taken by sha1sum, from git only "git ls-tree HEAD" is asked - it runs no hooks, filters or
# fsmonitor - with transports off (no lazy fetch of a partial clone), no system or global config,
# and as the owner of the git directory: what the repository config could make git run, its
# owner could run anyway. A git directory of another user inside a container is not looked into.
#
# WordPress updates itself from the admin or by wp-cron: a language pack, a plugin, a theme or
# the core. To tell that from an edit, a changed translation (languages/*.l10n.php) is sent with
# the time of the .mo next to it - a language pack writes both in the same second - and a run
# carries the versions that changed since the last one: the core from wp-includes/version.php,
# plugins and themes from the Version: line of their headers.
#
# Only paths, sizes, modification times and these versions leave the node, never file contents.
# The helper changes nothing in the roots and keeps its own lists in /var/lib/kervax/webfiles
# (root only).
set -euo pipefail

KERVAX_SETUP_VERSION=0.5 # MAJOR.MINOR; compared component-wise
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

# GSAME, GDIRTY: JSON lists of the files (paths from the root) whose content is the same as in
# HEAD or not. Arguments: git dir, path prefix of the root in the checkout, root, file with paths
git_check() {
  local gd=$1 pre=$2 root=$3 list=$4 cdir=$1 uid gid f h t e
  local -a run=() paths=()
  local -A tree=()
  GSAME="" GDIRTY=""
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
  # "mode type blob TAB path", NUL-terminated: paths come as they are
  while IFS="$TAB" read -r -d '' h f; do
    tree[$f]=${h##* }
  done < <(cd / && timeout 30 env -i PATH="$PATH" HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 \
             GIT_CONFIG_GLOBAL="$([ "$uid" = 0 ] && echo "$work/gitconfig" || echo /dev/null)" GIT_TERMINAL_PROMPT=0 \
             "${run[@]}" git -c safe.directory='*' -c protocol.allow=never --literal-pathspecs --git-dir="$gd" \
             ls-tree -z --full-tree HEAD -- "${paths[@]}" 2>/dev/null)
  while IFS= read -r f; do
    t=${tree[$pre$f]:-}
    [ -n "$t" ] && [ -f "$root/$f" ] || continue
    h=$({ printf 'blob %s\0' "$(stat -c %s "$root/$f")"; cat "$root/$f"; } 2>/dev/null | sha1sum | cut -c1-40)
    e="\"$(esc "$f")\""
    if [ "$h" = "$t" ]; then GSAME="$GSAME${GSAME:+,}$e"; else GDIRTY="$GDIRTY${GDIRTY:+,}$e"; fi
  done < "$list"
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
find "${OUT%/*}" -maxdepth 1 -name "${OUT##*/}.??????" -mmin +30 -delete 2>/dev/null || true
work=$(mktemp -d /tmp/kervax-webfiles.XXXXXX)
trap 'rm -rf "$work"' EXIT

roots="" deltas="" n=0 nfull=0 seen=" "
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
  roots="$roots${roots:+,}{\"key\":\"$key\",\"where\":\"$(esc "$where")\",\"path\":\"$(esc "$inner")\",\"domains\":[$doms],\"files\":$files,\"big\":$big,\"layer\":$layer${src:+,\"src\":\"$src\"}${GEV:+,\"git\":[$GEV]}${branch:+,\"branch\":\"$branch\"}${wpv:+,\"wp\":\"$wpv\"}}"
  snap="$SNAP/$key.lst"
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
    # N new, C changed (time or size), G gone
    awk -F '\t' 'FILENAME == ARGV[1] { o[$1] = $2 "\t" $3; next }
      { if (!($1 in o)) print "N\t" $0; else { if (o[$1] != $2 "\t" $3) print "C\t" $0; delete o[$1] } }
      END { for (k in o) print "G\t" k }' "$snap" "$work/new" > "$work/diff"
    nn=$(grep -c '^N' "$work/diff" || true)
    nc=$(grep -c '^C' "$work/diff" || true)
    ng=$(grep -c '^G' "$work/diff" || true)
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
    # the files sent: the same content as in HEAD or not
    GSAME="" GDIRTY=""
    if [ -n "$gd" ] && [ -n "$gtop" ] && [ "$((nn + nc))" -gt 0 ]; then
      awk -F '\t' -v max="$MAXLIST" '($1 == "N" || $1 == "C") && c[$1]++ < max { print $2 }' "$work/diff" > "$work/gitfiles"
      git_check "$gd" "$gpre" "$p" "$work/gitfiles"
    fi
    if [ "$((nn + nc + ng))" -gt 0 ] || [ -n "$wpup" ]; then
      deltas="$deltas${deltas:+,}{\"key\":\"$key\",\"n_new\":$nn,\"n_changed\":$nc,\"n_gone\":$ng,\"new\":[$(jlist N "$work/diff")],\"changed\":[$(jlist C "$work/diff")],\"gone\":[$(jlist G "$work/diff")]${wpup:+,\"wp\":[$wpup]}${GSAME:+,\"git_same\":[$GSAME]}${GDIRTY:+,\"git_dirty\":[$GDIRTY]}}"
    fi
  fi
  cp -f "$work/new" "$snap.tmp" && mv -f "$snap.tmp" "$snap"
  cp -f "$work/wp" "$SNAP/$key.wp.tmp" && mv -f "$SNAP/$key.wp.tmp" "$SNAP/$key.wp"
done < <(cands | awk -F '\t' '!seen[$1 FS $2]++')

# lists of roots that are gone for a week, of containers - for a day
find "$SNAP" -maxdepth 1 \( -name '*.lst' -o -name '*.wp' \) -mtime +7 -delete 2>/dev/null || true
find "$SNAP" -maxdepth 1 \( -name 'c*.lst' -o -name 'c*.wp' \) -mtime +0 -delete 2>/dev/null || true

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
