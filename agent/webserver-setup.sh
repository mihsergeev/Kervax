#!/usr/bin/env bash
# Kervax: the domains served by host and container web servers (nginx, apache) for the
# Services section. The unprivileged agent (kervax) cannot read configs, and docker exec is
# blocked by its own proxy, so a root helper dumps `nginx -T` on a timer (plus `docker exec`
# straight into nginx containers, bypassing the proxy; plus apache -S; plus Traefik Host()
# labels) and writes /var/lib/kervax/web-sites.json. The agent ONLY READS that file.
# Installed by the ansible playbook. Route domains only (server_name / namevhost), without
# secrets or config contents.
set -euo pipefail

KERVAX_SETUP_VERSION=0.4  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: the refresh is a no-op without a web server

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-web-sites"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/web-sites.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (otherwise, under an active umask
# 077, the unprivileged agent cannot enter it and never reads the file — the very bug from
# kube-setup).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions"

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
collect_caddy() {
  if command -v docker >/dev/null 2>&1; then
    for cid in $(docker ps -q 2>/dev/null); do
      docker inspect --format '{{index .Config.Labels "caddy"}}' "$cid" 2>/dev/null
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
  for cid in $(docker ps -q 2>/dev/null); do
    docker inspect --format '{{json .Config.Labels}}' "$cid" 2>/dev/null
  done | grep -oE 'Host(SNI)?\(`[^)]*\)' | grep -oE '`[^`]+`' | tr -d '`' | extract_domains
}

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
"$HELPER" || true   # run once immediately so the data appears without waiting for the timer

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/webserver-setup.ver"
chmod 0644 "$STATE_DIR/versions/webserver-setup.ver"
echo "✓ webserver-setup: web server domains -> $OUT (refreshed at boot and every 15 minutes)."
