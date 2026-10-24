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
# A plugin writes files of its own in wp-content: drop-ins (advanced-cache.php, object-cache.php,
# db.php, autoptimize_404_handler.php) from a file it ships, configs of cache plugins, .htaccess
# that closes a directory. Such a file sent or watched goes with a verdict when it is that and
# nothing else, and does not look like a web shell:
# - plugin:<slug> - a file right in wp-content made from a file of that installed plugin of the
#   same name (or name-sample.php): its lines of code are lines of that file in the same order,
#   some left out, a placeholder ({{NAME}}, <!--name-->) filled with a path or a version, a
#   constant set to another literal or to a path from a directory of WordPress; a literal into a
#   variable may be anywhere. An absolute path stays in the site and none goes up with "..": a
#   drop-in pointed elsewhere would load code from there;
# - data - php in wp-content that runs nothing: exit first, or only literal values (a string, a
#   number, true, false, null, an array of them) into variables and constants;
# - deny - .htaccess that only closes access.
# The panel takes such a change for the plugin's, not an edit, and takes off a sign of such a file.
#
# WordPress updates itself from the admin or by wp-cron: a language pack, a plugin, a theme or
# the core. To tell that from an edit, a changed translation (languages/*.l10n.php) is sent with
# the time of the .mo next to it - a language pack writes both in the same second - and a run
# carries the versions that changed since the last one: the core from wp-includes/version.php,
# plugins and themes from the Version: line of their headers.
#
# What is evidence of a break-in and nothing else (the panel alerts only on that):
# - php that looks like a web shell (eval of base64, a call with $_POST and the like, names of
#   known shells) and php in uploads (wp-content/uploads, upload(s) or tmp at the top of the site or
#   under public/web, sites/*/files);
# - .htaccess or .user.ini that runs php for pictures or text, runs php in uploads or prepends a
#   file to every request from uploads, tmp or not a .php;
# - a file of the WordPress core or of a plugin that is not as in the official release of its
#   version: for these files the md5 goes to the panel with the plugin and its version, the panel
#   takes the official checksums from wordpress.org. It is public code, the md5 tells nothing else.
# Any other change - a deploy, an edit, an update - the panel only shows.
#
# The owner of a file is named as the host knows the user for a directory mounted into a
# container (a deploy from the host writes it), as the container knows it for its own layer.
#
# Only paths, sizes, modification times, owners, these versions, the verdicts above and md5 of
# WordPress core and plugin files leave the node, never file contents or other hashes.
# The helper changes nothing in the roots and keeps its own lists in /var/lib/kervax/webfiles
# (root only).
set -euo pipefail

KERVAX_SETUP_VERSION=0.8 # MAJOR.MINOR; compared component-wise
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
GEN_TPL=1048576  # a plugin file bigger than this is not a template
GEN_DATA=32768   # php or .htaccess bigger than this is not a config

# The checks of gen_check: mode=tpl - a template and the file, data or deny - the file
IFS= read -r -d '' GEN_AWK <<'GEN_AWK_EOF' || true
function safe(v) { return v ~ /^[A-Za-z0-9._\/:~+@-]*$/ && !index(v, "..") }
# an absolute path in a value points into the site: a drop-in pointed elsewhere would load
# code from there
function inbase(v) { return substr(v, 1, 1) != "/" || v == "/" || index(v "/", base "/") == 1 }
# php without whitespace and comments, strings as S, the rest in lower case; "!" where it may
# hide code: a comment that ends php mode, an attribute, a string with $, { or \ in double quotes
function strip(s,   out, i, n, c, d, e, j, t) {
  n = length(s); out = ""; i = 1
  while (i <= n) {
    c = substr(s, i, 1); d = substr(s, i, 2)
    if (d == "#[") return "!"
    if (d == "//" || c == "#") {
      t = substr(s, i); j = index(t, "\n")
      if (j) t = substr(t, 1, j - 1)
      if (index(t, "?>")) return "!"
      i += length(t); continue
    }
    if (d == "/*") {
      j = index(substr(s, i + 2), "*/")
      if (!j) return "!"
      i += j + 3; continue
    }
    if (c == "'") {
      for (j = i + 1; j <= n; j++) { e = substr(s, j, 1); if (e == "\\") j++; else if (e == "'") break }
      if (j > n) return "!"
      out = out "S"; i = j + 1; continue
    }
    if (c == "\"") {
      for (j = i + 1; j <= n; j++) {
        e = substr(s, j, 1)
        if (e == "\\" || e == "$" || e == "{") return "!"
        if (e == "\"") break
      }
      if (j > n) return "!"
      out = out "S"; i = j + 1; continue
    }
    if (c != " " && c != "\t" && c != "\r" && c != "\n") out = out tolower(c)
    i++
  }
  return out
}
# a literal value at P of V: a string, a number, true, false, null or an array of them
function val(   t) {
  if (substr(V, P, 6) == "array(") { P += 6; return arr(")") }
  t = substr(V, P, 1)
  if (t == "[") { P++; return arr("]") }
  if (t == "S") { P++; return 1 }
  t = substr(V, P)
  if (match(t, /^-?[0-9][0-9.]*/) || match(t, /^(true|false|null)/)) { P += RLENGTH; return 1 }
  return 0
}
function arr(cl) {
  while (1) {
    if (substr(V, P, 1) == cl) { P++; return 1 }
    if (!val()) return 0
    if (substr(V, P, 2) == "=>") { P += 2; if (!val()) return 0 }
    if (substr(V, P, 1) == ",") P++
    else if (substr(V, P, 1) == cl) { P++; return 1 }
    else return 0
  }
}
function literal(v) { V = v; P = 1; return v != "" && val() && P == length(V) + 1 }
# a statement of a file that runs nothing: the ABSPATH guard, a literal into a variable or a
# constant, return of a literal
function stmt(x) {
  if (br && substr(x, 1, 1) == "}") { br = 0; x = substr(x, 2) }
  if (x == "") return 1
  if (x ~ /^(defined\(S\)(\|\||or)|if\(!defined\(S\)\))(exit|die)(\((S|[0-9]+)?\))?$/) return 1
  if (x ~ /^if\(!defined\(S\)\)[{](exit|die)(\((S|[0-9]+)?\))?$/) { br = 1; return 1 }
  if (substr(x, 1, 9) == "define(S," && substr(x, length(x)) == ")") return literal(substr(x, 10, length(x) - 10))
  if (substr(x, 1, 6) == "return") return literal(substr(x, 7))
  if (match(x, /^\$[a-z_][a-z0-9_]*(\[(S|-?[0-9]+)?\])*=/)) return literal(substr(x, RLENGTH + 1))
  return 0
}
# strings of a line that start with / stay in the site, none goes up with ..
function paths(s,   c, j, p) {
  if (index(s, "..")) return 0
  while (match(s, /['"]\//)) {
    c = substr(s, RSTART, 1); s = substr(s, RSTART + 1)
    j = index(s, c); p = j ? substr(s, 1, j - 1) : s
    if (!inbase(p)) return 0
    s = j ? substr(s, j + 1) : ""
  }
  return 1
}
# a line of the template with placeholders ({{NAME}}, <!--name-->) filled with a path or a version
function ph(tl, s,   k, lit, rest, i, pos, j, v) {
  k = 0; rest = tl
  while (match(rest, /[{][{][A-Za-z0-9_\/.-]+[}][}]|<!--[A-Za-z0-9_.-]+-->/)) {
    lit[k++] = substr(rest, 1, RSTART - 1); rest = substr(rest, RSTART + RLENGTH)
  }
  if (!k) return 0
  lit[k] = rest
  if (substr(s, 1, length(lit[0])) != lit[0]) return 0
  pos = length(lit[0]) + 1
  for (i = 1; i < k; i++) {
    if (lit[i] == "") continue
    j = index(substr(s, pos), lit[i])
    if (!j) return 0
    v = substr(s, pos, j - 1)
    if (!safe(v) || !inbase(v)) return 0
    pos += j - 1 + length(lit[i])
  }
  if (length(s) - pos + 1 < length(lit[k]) || substr(s, length(s) - length(lit[k]) + 1) != lit[k]) return 0
  v = substr(s, pos, length(s) - pos + 1 - length(lit[k]))
  return safe(v) && inbase(v)
}
# a line of the file is this line of the template: the same, filled in, or another value of the
# same constant - a literal or a path from a directory of WordPress
function like(tl, s,   a, b, n, v) {
  if (tl == s || s == "<?php" && substr(tl, 1, 5) == "<?php") return 1
  if (ph(tl, s)) return 1
  a = strip(tl); b = strip(s)
  if (a == "!" || b == "!" || substr(b, length(b) - 1) != ");" || index(s, "..")) return 0
  if (substr(a, 1, 9) != "define(S," || substr(b, 1, 9) != "define(S,") return 0
  a = tl; n = s; gsub(/[ \t]/, "", a); gsub(/[ \t]/, "", n)
  a = substr(a, 1, index(a, ","))
  if (a == "" || substr(n, 1, length(a)) != a) return 0
  v = substr(b, 10, length(b) - 11)
  return v ~ /^(wp_content_dir|wp_plugin_dir|abspath)\.S$/ || literal(v) && paths(s)
}
# a literal into a variable runs nothing, in any place: a cache plugin keeps its settings so and
# adds new ones where it likes
function extra(s,   b) {
  b = strip(s)
  if (b == "!" || substr(b, length(b)) != ";" || !paths(s)) return 0
  b = substr(b, 1, length(b) - 1)
  return match(b, /^\$[a-z_][a-z0-9_]*(\[(S|-?[0-9]+)?\])*=/) && literal(substr(b, RLENGTH + 1))
}
function norm(s) { sub(/\r$/, "", s); sub(/[ \t]+$/, "", s); return s ~ /^[ \t]*$/ ? "" : s }
BEGIN { xre = "^<[?]php[ \t]+(exit|die)[ \t]*([(][ \t]*('[^'\\\\]*'|\"[^\"$\\\\]*\"|[0-9]+)?[ \t]*[)])?[ \t]*;" }
mode == "deny" {
  l = tolower($0); sub(/\r$/, "", l); sub(/^[ \t]+/, "", l); sub(/[ \t]+$/, "", l)
  if (l == "" || l ~ /^#/ || l ~ /^<\/?(ifmodule|files|filesmatch)([ \t>]|$)/ || l ~ /^order[ \t]+(deny|allow)[ \t]*,[ \t]*(deny|allow)$/ || l ~ /^options[ \t]+-indexes$/) next
  if (l ~ /^require[ \t]+all[ \t]+denied$/ || l ~ /^deny[ \t]+from[ \t]+all$/) { deny = 1; next }
  bad = 1; exit
}
mode == "data" {
  sub(/\r$/, "")
  if (NR == 1 && $0 ~ xre) { ok = 1; exit }
  buf = buf $0 "\n"; next
}
mode == "tpl" && FILENAME == ARGV[1] { s = norm($0); if (s != "") t[++tn] = s; next }
mode == "tpl" {
  s = norm($0)
  if (s == "") next
  if (extra(s)) next
  while (++ti <= tn) if (like(t[ti], s)) break
  if (ti > tn) { bad = 1; exit }
}
END {
  if (mode == "deny") exit !(deny && !bad)
  if (mode == "tpl") exit bad || !tn
  if (ok) exit 0
  x = strip(buf)
  if (x == "!" || substr(x, 1, 5) != "<?php") exit 1
  x = substr(x, 6)
  if (substr(x, length(x) - 1) == "?>") x = substr(x, 1, length(x) - 2)
  if (index(x, "?>") || index(x, "<?")) exit 1
  n = split(x, st, ";")
  for (i = 1; i <= n; i++) if (!stmt(st[i])) exit 1
  exit 0
}
GEN_AWK_EOF

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

# NUL-separated paths of the list (from the root) whose php looks like a web shell: code run from
# the request, eval of an encoded string, the names of known shells. A method ($db->exec($_POST))
# or a function of another name (curl_exec) is not one
shells() {
  (cd "$1" 2>/dev/null && awk '/\.(php[3-8]?|phtml|pht|phar)$/' "$2" \
    | while IFS= read -r f; do [ -f "$f" ] && [ "$(stat -c %s "$f" 2>/dev/null || echo 0)" -le "$HASH_MAX" ] && printf '%s\0' "$f"; done \
    | timeout 60 xargs -0 -r grep -l -Z -a -s -E \
        -e '(^|[^A-Za-z0-9_>:$\])(eval|assert)[[:space:]]*\([[:space:]]*@?[[:space:]]*(base64_decode|gzinflate|gzuncompress|gzdecode|str_rot13|strrev|rawurldecode|hex2bin)[[:space:]]*\(' \
        -e '(^|[^A-Za-z0-9_>:$\])(eval|assert|system|passthru|shell_exec|exec|popen|proc_open|pcntl_exec)[[:space:]]*\([[:space:]]*@?[[:space:]]*\$_(GET|POST|REQUEST|COOKIE|FILES)' \
        -e '\$_(GET|POST|REQUEST|COOKIE)[[:space:]]*\[[^]]{1,40}\][[:space:]]*\([[:space:]]*@?[[:space:]]*\$_(GET|POST|REQUEST|COOKIE)' \
        -e '(FilesMan|b374k|c99shell|r57shell|IndoXploit|AnonymousFox|Yanz Webshell)' -- 2>/dev/null)
}

# OWN[path from the root]: "owner TAB 1 when the owner is the web user TAB 1 when the php looks
# like a web shell". Arguments: root, passwd to read names from, web uids, file with paths, passwd
# for the uids the first one does not know
declare -A OWN=()
owners_of() {
  local root=$1 pw=$2 web=$3 list=$4 pw2=${5:-} f u n w
  local -A name=() sus=()
  OWN=()
  [ -s "$list" ] || return 0
  while IFS=: read -r n _x u _rest; do
    [ -n "$u" ] && [ -z "${name[$u]+x}" ] && name[$u]=$(printf '%s' "$n" | tr -cd 'A-Za-z0-9._-' | head -c 32)
  done < <(head -c 262144 "$pw" 2>/dev/null; [ -n "$pw2" ] && head -c 262144 "$pw2" 2>/dev/null)
  while IFS= read -r -d '' f; do sus[$f]=1; done < <(shells "$root" "$list")
  while IFS= read -r f; do
    u=$(stat -c %u "$root/$f" 2>/dev/null) || continue
    n=${name[$u]:-$u}
    w=0
    case "$web" in *" $u "*) w=1 ;; esac
    OWN[$f]="$n$TAB$w$TAB${sus[$f]:-0}"
  done < "$list"
}

# GEN[path from the root]: plugin:<slug>, data or deny - a file a plugin keeps by itself (see the
# top). Arguments: root, the root as the site sees it (for paths in drop-ins), wp-content from the
# root ("" - not WordPress), file with paths
declare -A GEN=()
gen_check() {
  local root=$1 base=$2 wpc=$3 list=$4 f b t v
  local -A sus=()
  GEN=()
  [ -s "$list" ] || return 0
  # only what such a file can be: .htaccess, php in wp-content out of plugins and themes
  while IFS= read -r f; do
    case "$f" in
      .htaccess|*/.htaccess) printf '%s\n' "$f" ;;
      "$wpc"plugins/*|"$wpc"themes/*|"$wpc"mu-plugins/*) ;;
      "$wpc"*.php) [ -n "$wpc" ] && printf '%s\n' "$f" ;;
    esac
  done < "$list" > "$work/gencand"
  [ -s "$work/gencand" ] || return 0
  while IFS= read -r -d '' f; do sus[$f]=1; done < <(shells "$root" "$work/gencand")
  while IFS= read -r f; do
    [ -z "${sus[$f]:-}" ] && [ -f "$root/$f" ] && [ ! -L "$root/$f" ] || continue
    v=""
    case "$f" in
      .htaccess|*/.htaccess)
        [ "$(stat -c %s "$root/$f" 2>/dev/null || echo 0)" -le "$GEN_DATA" ] && awk -v mode=deny "$GEN_AWK" "$root/$f" 2>/dev/null && v=deny ;;
      *)
        # a drop-in lies right in wp-content
        b=${f#"$wpc"}
        if [ "${b%/*}" = "$b" ] && [ -d "$root/${wpc}plugins" ]; then
          while IFS= read -r t; do
            if awk -v mode=tpl -v base="$base" "$GEN_AWK" "$t" "$root/$f" 2>/dev/null; then
              t=${t#"$root/${wpc}plugins/"}
              v=plugin:$(printf '%s' "${t%%/*}" | tr -cd 'A-Za-z0-9._-' | head -c 60)
              break
            fi
          done < <(find "$root/${wpc}plugins" -mindepth 2 -maxdepth 5 -type f \( -name "$b" -o -name "${b%.php}-sample.php" \) \
                     -size -"$((GEN_TPL / 1024))"k 2>/dev/null | head -5)
        fi
        if [ -z "$v" ] && [ "$(stat -c %s "$root/$f" 2>/dev/null || echo 0)" -le "$GEN_DATA" ] && awk -v mode=data "$GEN_AWK" "$root/$f" 2>/dev/null; then
          v=data
        fi ;;
    esac
    [ -n "$v" ] && GEN[$f]=$v
  done < "$work/gencand"
}

# A directory of uploads, as in_uploads of the panel: wp-content/uploads, upload(s), userfiles or
# tmp at the top of the site or right under public/web (backend/web/uploads of Yii),
# sites/*/files. files, images or media in code (src/Files, .../Upload/) are classes, not uploads.
# Reads paths from the root, prints those in one
UP_AWK='{ n = split($0, s, "/"); u = 0
  for (i = 1; i < n; i++) if (s[i] == "wp-content") for (j = i + 1; j < n; j++) if (s[j] == "uploads") u = 1
  if (n > 3 && s[1] == "sites" && s[3] == "files") u = 1
  for (i = 1; i < n; i++)
    if (s[i] ~ /^(upload|uploads|userfiles|tmp|temp)$/ && (i == 1 || s[i - 1] ~ /^(public|web|htdocs|pub|public_html|www)$/)) u = 1
  if (u) print }'

# Exit 0 when .htaccess or .user.ini runs php where it must not: a php handler or type for other
# extensions (pictures, text), a php handler for a whole directory of uploads or for pictures in
# <Files>, php switched on in uploads, a file prepended to every request from uploads, tmp, with
# .. or not a .php. up=1 - the file lies in a directory of uploads
IFS= read -r -d '' EXEC_AWK <<'EXEC_AWK_EOF' || true
function phpext(e) { sub(/^\./, "", e); return e ~ /^(php[3-8]?|phtml|pht|phar)$/ }
function bad_path(v) {
  gsub(/["']/, "", v)
  return v != "" && v != "none" && (v !~ /\.php$/ || index(v, "..") || v ~ /\/(uploads?|tmp|temp|shm)\//)
}
{ l = tolower($0); sub(/\r$/, "", l); sub(/^[ \t]+/, "", l); sub(/[ \t]+$/, "", l) }
l == "" || l ~ /^[#;]/ { next }
l ~ /^<files(match)?[ \t]/ { pic = l ~ /(jpe?g|png|gif|ico|bmp|webp|svg|txt)/; next }
l ~ /^<\/files/ { pic = 0; next }
{ n = split(l, w, /[ \t=]+/) }
(w[1] == "addhandler" || w[1] == "addtype") && w[2] ~ /php/ {
  for (i = 3; i <= n; i++) if (!phpext(w[i])) { hit = 1; exit }
}
(w[1] == "sethandler" || w[1] == "forcetype") && w[2] ~ /php|fcgi/ && (up || pic) { hit = 1; exit }
(w[1] == "php_flag" || w[1] == "php_admin_flag") && w[2] == "engine" && w[3] ~ /^(on|1)$/ && up { hit = 1; exit }
(w[1] == "php_value" || w[1] == "php_admin_value") && w[2] ~ /^auto_(prepend|append)_file$/ && bad_path(w[3]) { hit = 1; exit }
w[1] ~ /^auto_(prepend|append)_file$/ && bad_path(w[2]) { hit = 1; exit }
END { exit !hit }
EXEC_AWK_EOF

# EXEC[path from the root]=1: .htaccess or .user.ini of the list that runs php where it must not.
# Arguments: root, file with paths
declare -A EXEC=()
exec_check() {
  local root=$1 list=$2 f up
  local -A ups=()
  EXEC=()
  [ -s "$list" ] || return 0
  while IFS= read -r f; do ups[$f]=1; done < <(awk "$UP_AWK" "$list")
  while IFS= read -r f; do
    case "$f" in .htaccess|*/.htaccess|.user.ini|*/.user.ini) ;; *) continue ;; esac
    [ -f "$root/$f" ] && [ ! -L "$root/$f" ] && [ "$(stat -c %s "$root/$f" 2>/dev/null || echo 0)" -le "$GEN_DATA" ] || continue
    up=0
    [ -n "${ups[$f]:-}" ] && up=1
    awk -v up="$up" "$EXEC_AWK" "$root/$f" 2>/dev/null && EXEC[$f]=1
  done < "$list"
}

# SUMS[path from the root]: "md5 TAB core or plugin:<slug> TAB path in the release TAB version" for
# the files of the list that come in a release on wordpress.org: wp-admin, wp-includes and wp-*.php
# of the core, the files of a plugin in its directory. Arguments: root, versions (wp_versions), file
# with paths
declare -A SUMS=()
wp_sums() {
  local root=$1 wp=$2 list=$3 f ref rel ver m
  SUMS=()
  [ -s "$list" ] && [ -s "$wp" ] || return 0
  while IFS="$TAB" read -r f ref rel ver; do
    [ -f "$root/$f" ] && [ ! -L "$root/$f" ] && [ "$(stat -c %s "$root/$f" 2>/dev/null || echo 0)" -le "$HASH_MAX" ] || continue
    m=$(md5sum < "$root/$f" 2>/dev/null | cut -c1-32)
    [ "${#m}" = 32 ] && SUMS[$f]="$m$TAB$ref$TAB$rel$TAB$ver"
  done < <(awk -F '\t' -v OFS='\t' '
    FILENAME == ARGV[1] {
      if ($1 == "core") { core = $4; cv = $3; has = 1 }
      else if ($1 == "plugin" && $4 ~ /\/$/) { pp[$4] = $2 FS $3 }
      next
    }
    has && substr($0, 1, length(core)) == core {
      r = substr($0, length(core) + 1)
      if (r ~ /^wp-(admin|includes)\// || r ~ /^(index|xmlrpc|wp-[^\/]+)\.php$/ && r != "wp-config.php") { print $0, "core", r, cv; next }
    }
    { for (p in pp) if (substr($0, 1, length(p)) == p) { split(pp[p], a, FS); print $0, "plugin:" a[1], substr($0, length(p) + 1), a[2]; next } }
  ' "$wp" "$list" | head -n 40)
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
  local p=$1 v w="" c rel loc
  for v in "$p/wp-includes/version.php" "$p"/*/wp-includes/version.php "$p/web/wp/wp-includes/version.php"; do
    if [ -f "$v" ]; then w=${v%/wp-includes/version.php}; break; fi
  done
  [ -n "$w" ] || return 0
  rel=${w#"$p"}; rel=${rel#/}; rel=${rel:+$rel/}
  v=$(awk -F "'" '/^\$wp_version *=/ { print $2; exit }' "$w/wp-includes/version.php" 2>/dev/null | tr -cd 'A-Za-z0-9._+-' | head -c 30)
  # a localized build has its own version.php: the checksums of the release go by the locale
  loc=$(awk -F "'" '/^\$wp_local_package *=/ { print $2; exit }' "$w/wp-includes/version.php" 2>/dev/null | tr -cd 'A-Za-z0-9_' | head -c 20)
  printf 'core\t\t%s\t%s\t%s\n' "$v" "$rel" "$loc"
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
  wploc=$(awk -F '\t' '$1 == "core" { print $5; exit }' "$work/wp")
  # wp-content from the root: where the plugins are, else next to the core
  wpc=$(awk -F '\t' '$1 == "plugin" && (i = index($4, "plugins/")) { print substr($4, 1, i - 1); exit }' "$work/wp")
  if [ -z "$wpc" ] && [ -n "$wpv" ]; then
    wpc=$(awk -F '\t' '$1 == "core" { print $4 "wp-content/"; exit }' "$work/wp")
    [ -d "$p/$wpc" ] || wpc=""
  fi
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
  # files a plugin keeps by itself: the ones sent and those watched
  cut -f1 "$work/wcur" | cat "$work/listed" - | sort -u > "$work/genfiles"
  gen_check "$p" "$inner" "$wpc" "$work/genfiles"
  # a watched file is done with: gone, back as it was, as in HEAD, a translation with its .mo, a
  # file the plugin keeps
  : > "$work/verd"
  while IFS="$TAB" read -r f h0 h t; do
    v=""
    # a file removed in a container leaves a whiteout in the upper layer: not a regular file
    if [ "$h" = - ] && [ ! -f "$lroot/$f" ]; then v=gone
    elif [ "$h0" != . ] && [ "$h" = "$h0" ]; then v=back
    elif [ "${GST[$f]:-}" = same ]; then v=head
    elif [ -n "${GEN[$f]:-}" ]; then v=${GEN[$f]}
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
    # owner of each file sent, whether the site wrote it, whether it looks like a web shell. A
    # directory mounted into a container is written from the host (a deploy) or by the container:
    # the host names the user first, the container the uids the host does not know
    pw=/etc/passwd pw2=""
    if [ "$p" != "$inner" ]; then
      if [ "$layer" = true ]; then pw=/proc/$pid/root/etc/passwd; else pw2=/proc/$pid/root/etc/passwd; fi
    fi
    owners_of "$lroot" "$pw" "$WEBALL" "$work/listed" "$pw2"
    exec_check "$p" "$work/listed"
    wp_sums "$p" "$work/wp" "$work/listed"
    finfo="" fsums=""
    while IFS= read -r f; do
      o=${OWN[$f]:-} g=${GST[$f]:-} d=${GEN[$f]:-} e=${EXEC[$f]:-0}
      if [ -n "${SUMS[$f]:-}" ]; then
        IFS="$TAB" read -r sm sref srel sver <<< "${SUMS[$f]}"
        fsums="$fsums${fsums:+,}\"$(esc "$f")\":[\"$sm\",\"$(esc "$sref")\",\"$(esc "$srel")\",\"$(esc "$sver")\"]"
      fi
      [ -n "$o$g$d" ] || [ "$e" = 1 ] || continue
      on="" ow=0 ox=0
      [ -n "$o" ] && IFS="$TAB" read -r on ow ox <<< "$o"
      finfo="$finfo${finfo:+,}\"$(esc "$f")\":[\"$g\",\"$(esc "$on")\",${ow:-0},${ox:-0},\"$d\",$e]"
    done < "$work/listed"
    [ -n "$finfo" ] && finfo=",\"f\":{$finfo}"
    [ -n "$fsums" ] && finfo="$finfo,\"sums\":{$fsums}"
    deltas="$deltas${deltas:+,}{\"key\":\"$key\",\"n_new\":$nn,\"n_changed\":$nc,\"n_gone\":$ng,\"n_same\":$ns,\"new\":[$(jlist N "$work/diff")],\"changed\":[$(jlist C "$work/diff")],\"gone\":[$(jlist G "$work/diff")],\"same\":[$(jlist S "$work/diff")]${wpup:+,\"wp\":[$wpup]}$finfo}"
  fi
  roots="$roots${roots:+,}{\"key\":\"$key\",\"where\":\"$(esc "$where")\",\"path\":\"$(esc "$inner")\",\"domains\":[$doms],\"files\":$files,\"big\":$big,\"layer\":$layer${src:+,\"src\":\"$src\"}${GEV:+,\"git\":[$GEV]}${branch:+,\"branch\":\"$branch\"}${wpv:+,\"wp\":\"$wpv\"}${wploc:+,\"wploc\":\"$wploc\"}${clean:+,\"clean\":[$clean]}}"
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
