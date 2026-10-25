#!/bin/sh
# Kervax agent installer. Usage: install.sh <panel_url> <token> [instance]
# Places a static binary in /opt/kervax-agent/bin (owned by kervax, so the agent can
# safely replace itself WITHOUT root), a config in /etc and a systemd unit.
# The agent runs as the unprivileged user kervax (no shell), outbound connections only.
#
# [instance] — for a SECOND agent on the same server (metrics to another panel):
#   install.sh https://panel2.example.com <token2> panel2
# → config /etc/kervax-agent-panel2.conf + unit kervax-agent@panel2 (template).
# The binary is shared; instances are fully independent (own url/token/process).
#
# Removing the agent from a node:
#   install.sh --uninstall            everything: service, helpers, binary, configs, user
#   install.sh --uninstall panel2     only that instance
#
# A panel whose certificate is not from a public CA (quickstart --local-ca: caddy's
# internal CA in a local network) shows the command with --ca-file <root.pem>: the
# root is added to the system trust store, and the agent, the helpers and curl then
# verify the panel with it. Certificate checks are never switched off.
#
# A node whose address is cut off on the way to the panel (its IP is on a block list
# of the panel's provider) installs through a proxy: --proxy http://user:pass@host:port
# (also https:// or socks5://). The installer downloads through it, and the agent gets
# proxy= in its config and sends everything to the panel through it as well.
#
# Russian version of this installer: agent/install-ru.sh
#
# The whole script is one { ... } block: sh reads a compound command to its end before
# running any of it. Piped from curl over a link that stalls or cuts the transfer midway
# (a DPI in the path), a truncated installer then does not run at all, instead of running
# its first half and waiting for the rest forever.
{
set -eu

# Argument parsing: positional <url> <token> [instance] plus flags.
# By default (auto mode) install.sh enables EVERYTHING applicable on the node: bounded
# Docker access (socket-proxy), read-only Kubernetes (a narrow SA), backup server
# statistics, restic backup control. Each step self-detects. --no-auto installs the
# agent only.
# --ca-file <path>: the root that signed the panel's certificate (see the trust step below)
# --proxy <url>: proxy to the panel for this installer and the agent (see the header)
URL=""; TOKEN=""; INSTANCE=""; WANT_DOCKER=0; NO_AUTO=0; UNINSTALL=0; CA_FILE=""; WANT_CA=0
# KERVAX_PROXY in the environment is the same as --proxy: the panel's install command passes it so
PROXY="${KERVAX_PROXY:-}"; WANT_PROXY=0
for a in "$@"; do
  if [ "$WANT_CA" = 1 ]; then CA_FILE="$a"; WANT_CA=0; continue; fi
  if [ "$WANT_PROXY" = 1 ]; then PROXY="$a"; WANT_PROXY=0; continue; fi
  case "$a" in
    --ca-file) WANT_CA=1 ;;
    --ca-file=*) CA_FILE="${a#--ca-file=}" ;;
    --proxy) WANT_PROXY=1 ;;
    --proxy=*) PROXY="${a#--proxy=}" ;;
    --docker) WANT_DOCKER=1 ;;
    --no-auto) NO_AUTO=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *)
      if [ -z "$URL" ]; then URL="$a"
      elif [ -z "$TOKEN" ]; then TOKEN="$a"
      elif [ -z "$INSTANCE" ]; then INSTANCE="$a"
      fi ;;
  esac
done
# The panel command passes the token in KERVAX_TOKEN rather than as an argument:
# arguments are visible to every user in ps for the minutes the install takes.
# Then the second positional argument is the instance name, not the token
if [ -n "${KERVAX_TOKEN:-}" ]; then
  if [ -n "$TOKEN" ] && [ -z "$INSTANCE" ]; then INSTANCE="$TOKEN"; fi
  TOKEN="$KERVAX_TOKEN"
fi
unset KERVAX_TOKEN

# ── Removal ──────────────────────────────────────────────────────────────────
# There used to be no reverse operation at all: the server is deleted in the panel
# while the node keeps a running service knocking with a revoked token, plus the user,
# the binary, helper units, sudoers and the socket-proxy. What exactly to clean up was
# known only to whoever had read this file.
#   install.sh --uninstall            remove the agent
#   install.sh --uninstall <instance> remove a single instance (suffixed config/unit)
if [ "$UNINSTALL" = 1 ]; then
  [ "$(id -u)" = "0" ] || { echo "Root required (sudo)." >&2; exit 1; }
  # with --uninstall the first positional argument is the instance, not a url
  [ -z "$INSTANCE" ] && [ -n "$URL" ] && INSTANCE="$URL"
  if [ -n "$INSTANCE" ]; then
    UNITS="kervax-agent@$INSTANCE"
    CONFS="/etc/kervax-agent-$INSTANCE.conf"
    echo "→ Removing instance $INSTANCE"
  else
    UNITS="kervax-agent"
    CONFS="/etc/kervax-agent.conf"
    # template-based instances go too — otherwise they are left without the main unit
    # and keep knocking on their panels
    for u in $(systemctl list-units --all --plain --no-legend 'kervax-agent@*' 2>/dev/null | awk '{print $1}'); do
      UNITS="$UNITS ${u%.service}"
      n=${u#kervax-agent@}; n=${n%.service}
      CONFS="$CONFS /etc/kervax-agent-$n.conf"
    done
    echo "→ Removing the agent completely"
  fi

  for u in $UNITS; do
    systemctl disable --now "$u" >/dev/null 2>&1 || true
  done
  # helper units, timers and path units - by the kervax-* mask, not by a list: the list
  # here lagged behind the helpers, and after --uninstall a dozen timers kept firing for
  # nothing (changes, cron, disk-*, fpm, logerr, units, web-rate, workers...).
  # Timers and path units go first, otherwise they start their service again mid-removal.
  if [ -z "$INSTANCE" ]; then
    KUNITS=$({
      systemctl list-unit-files --no-legend --plain 'kervax-*' 2>/dev/null
      systemctl list-units --all --no-legend --plain 'kervax-*' 2>/dev/null
      ls /etc/systemd/system 2>/dev/null
    } | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^kervax-[A-Za-z0-9@_.-]*\.(timer|path|socket|service)$/) { print $i; break } }' | sort -u)
    for kind in timer path socket service; do
      for u in $KUNITS; do
        case "$u" in *."$kind") systemctl disable --now "$u" >/dev/null 2>&1 || true ;; esac
      done
    done
    # unit files, drop-ins (spool, watchdog) and enable links left by a failed disable
    rm -rf /etc/systemd/system/kervax-*
    find /etc/systemd/system -name 'kervax-*' \( -type l -o -type f \) -delete 2>/dev/null || true
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true
  [ -z "$INSTANCE" ] && { systemctl reset-failed 'kervax-*' >/dev/null 2>&1 || true; }

  rm -f $CONFS
  # the panel CA added by --ca-file (only ours: kervax-panel*.crt), then the store is rebuilt
  if [ -n "$INSTANCE" ]; then CA_GLOB="kervax-panel-$INSTANCE.crt"; else CA_GLOB="kervax-panel*.crt"; fi
  CA_GONE=0
  for d in /usr/local/share/ca-certificates /etc/pki/ca-trust/source/anchors /etc/pki/trust/anchors; do
    for f in $d/$CA_GLOB; do
      [ -e "$f" ] || continue
      rm -f "$f"; CA_GONE=1
    done
  done
  if [ "$CA_GONE" = 1 ]; then
    if command -v update-ca-trust >/dev/null 2>&1 && [ -d /etc/pki/ca-trust/source/anchors ]; then
      update-ca-trust extract >/dev/null 2>&1 || true
    else
      update-ca-certificates --fresh >/dev/null 2>&1 || update-ca-certificates >/dev/null 2>&1 || true
    fi
    echo "✓ The panel CA is no longer trusted on this node."
  fi
  if [ -z "$INSTANCE" ]; then
    # only our own proxy (and its copy from a helper update): other socket-proxy containers
    # on the node belong to someone else and stay
    command -v docker >/dev/null 2>&1 && docker rm -f kervax-docker-proxy kervax-docker-proxy-old >/dev/null 2>&1 || true
    # cron of the helpers (backup client/server statistics). kervax-prune-* is not ours to
    # remove: it is the nightly cleanup of client repositories on the backup server, moved
    # there from the root crontab, and without it the repositories grow without limit
    KEEP_PRUNE=""
    for f in /etc/cron.d/kervax-*; do
      [ -e "$f" ] || continue
      case "$f" in
        /etc/cron.d/kervax-prune-*) KEEP_PRUNE="$KEEP_PRUNE $f" ;;
        *) rm -f "$f" ;;
      esac
    done
    # the prune scripts may run our restic from /lib65/kervax - it stays with them
    if [ -n "$KEEP_PRUNE" ] && [ -d /lib65/kervax ]; then
      find /lib65/kervax -mindepth 1 -maxdepth 1 ! -name restic -exec rm -rf {} + 2>/dev/null || true
    else
      rm -rf /lib65/kervax
    fi
    # state and panel request spools (*-req/*-res), reports, caches, locks
    rm -rf /opt/kervax-agent /opt/kervax/bin /etc/kervax /var/lib/kervax /run/kervax-*
    rmdir /opt/kervax /lib65 2>/dev/null || true   # only if empty (the panel directory survives)
    rm -f /etc/sudoers.d/kervax-*
    id kervax >/dev/null 2>&1 && { userdel kervax >/dev/null 2>&1 || deluser kervax >/dev/null 2>&1 || true; }
    echo "✓ Agent removed: service, helper units and timers, cron, state, binary, configs, sudoers, user kervax."
    if [ -n "$KEEP_PRUNE" ]; then
      echo "  The nightly cleanup of backup repositories stays (it is not monitoring):"
      for f in $KEEP_PRUNE; do echo "    $f"; done
      echo "    remove it only together with the backup server itself: rm -f /etc/cron.d/kervax-prune-* /lib65/kervax/restic"
    fi
    echo "  The Kubernetes account (if it was enabled) stays in the cluster:"
    echo "    kubectl -n kervax delete serviceaccount kervax    # if you want it gone"
    echo "  Delete the server in the panel separately — that revokes its token."
  else
    echo "✓ Instance $INSTANCE removed (the shared binary and other instances are untouched)."
  fi
  exit 0
fi

if [ -z "$URL" ] || [ -z "$TOKEN" ]; then
  echo "Usage: install.sh [--ca-file <root.pem>] [--proxy <url>] <panel_url> <token> [instance] [--no-auto]" >&2
  echo "       install.sh --uninstall [instance]" >&2
  exit 1
fi
if [ -n "$INSTANCE" ] && ! printf '%s' "$INSTANCE" | grep -Eq '^[A-Za-z0-9_-]+$'; then
  echo "Instance name may contain only letters, digits, hyphen and underscore." >&2
  exit 1
fi

case "$PROXY" in
  *[[:space:]]*) PROXY_OK=0 ;;
  "" | http://?* | https://?* | socks5://?*) PROXY_OK=1 ;;
  *) PROXY_OK=0 ;;
esac
if [ "$PROXY_OK" = 0 ]; then
  echo "--proxy must look like http://user:pass@host:port (https:// and socks5:// work too)." >&2
  exit 1
fi
# Every request to the panel goes through pcurl: with --proxy it adds the proxy, and
# only to these requests. Helpers piped from the panel run without it (their own
# downloads, the local Kubernetes API) and are not affected.
pcurl() {
  if [ -n "$PROXY" ]; then curl --proxy "$PROXY" "$@"; else curl "$@"; fi
}

if [ "$(id -u)" != "0" ]; then
  echo "Root required (sudo)." >&2
  exit 1
fi

# The umask is set here and does not come from whoever runs the script: sudo combines its
# own 022 with the caller's umask, so for a user with umask 077 every directory below was
# created 0700. /opt/kervax-agent then let nobody but root in, and the agent (user kervax)
# died at start with "Permission denied" (status=203/EXEC).
umask 022

case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) echo "Architecture $(uname -m) is not supported (amd64/arm64 required)." >&2; exit 1 ;;
esac

# unprivileged system user
id kervax >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin kervax 2>/dev/null || \
  adduser --system --no-create-home --shell /usr/sbin/nologin kervax 2>/dev/null || true

# -- Panel CA. The panel command passes --ca-file when its certificate comes from a CA
#    the node does not know (caddy's internal CA in a local network): without it the
#    very first download fails certificate verification, and so would the agent. The
#    root goes into the system trust store rather than into the agent config: the Go
#    agent reads that store, and so do the helpers and their curl to the panel. The
#    file name carries the instance, so a second panel's root does not replace the
#    first one, and --uninstall removes exactly these files. --
CA_NAME="kervax-panel${INSTANCE:+-$INSTANCE}.crt"
if [ -n "$CA_FILE" ]; then
  # The file comes from /tmp, where anybody may have made it first or swap it: it is read once
  # into a file of our own, and only if it is not a symlink, belongs to root or to whoever ran
  # sudo, and nobody else can write it (an appended CA would be trusted too)
  CA_OWN=$(stat -c '%u %a' "$CA_FILE" 2>/dev/null || echo "x 777")
  if [ -L "$CA_FILE" ] || [ ! -f "$CA_FILE" ] || [ $((0${CA_OWN#* } & 022)) != 0 ] \
     || { [ "${CA_OWN% *}" != 0 ] && [ "${CA_OWN% *}" != "${SUDO_UID:-0}" ]; }; then
    echo "✗ --ca-file $CA_FILE: not a regular file of root or of the user who ran sudo, or others can write it." >&2; exit 1
  fi
  CA_TMP=$(mktemp)
  cat "$CA_FILE" > "$CA_TMP"
  grep -q -- '-----BEGIN CERTIFICATE-----' "$CA_TMP" || {
    rm -f "$CA_TMP"; echo "✗ --ca-file $CA_FILE is not a PEM certificate." >&2; exit 1; }
  if command -v update-ca-certificates >/dev/null 2>&1 && [ -d /usr/local/share/ca-certificates ]; then
    CA_DST=/usr/local/share/ca-certificates/$CA_NAME            # Debian, Ubuntu, Alpine
    CA_UPDATE="update-ca-certificates"
  elif command -v update-ca-trust >/dev/null 2>&1 && [ -d /etc/pki/ca-trust/source/anchors ]; then
    CA_DST=/etc/pki/ca-trust/source/anchors/$CA_NAME             # RHEL, Fedora, Alma, Rocky
    CA_UPDATE="update-ca-trust extract"
  elif command -v update-ca-certificates >/dev/null 2>&1 && [ -d /etc/pki/trust/anchors ]; then
    CA_DST=/etc/pki/trust/anchors/$CA_NAME                       # openSUSE, SLES
    CA_UPDATE="update-ca-certificates"
  else
    echo "✗ No system trust store found to add the panel CA to (install ca-certificates)." >&2
    exit 1
  fi
  cp "$CA_TMP" "$CA_DST"
  rm -f "$CA_TMP"
  chmod 0644 "$CA_DST"
  $CA_UPDATE >/dev/null 2>&1 || { echo "✗ $CA_UPDATE failed." >&2; exit 1; }
  # checked WITHOUT --cacert, through the system store: exactly how the agent connects.
  # /api/agent/* and not /api/health: the panel's IP allow-list does not apply there.
  CA_ERR=$(mktemp)
  if ! pcurl -fsS --connect-timeout 15 -m 60 --retry 3 --retry-delay 5 -o /dev/null "${URL%/}/api/agent/install.sh" 2>"$CA_ERR"; then
    echo "✗ The panel CA is in $CA_DST, but https to the panel still fails:" >&2
    cat "$CA_ERR" >&2
    echo "  The certificate has to be issued for the exact host in $URL (an IP or a name)." >&2
    rm -f "$CA_ERR"
    exit 1
  fi
  rm -f "$CA_ERR"
  echo "✓ The panel CA is trusted: $CA_DST"
fi

echo "→ Downloading the agent ($ARCH)..."
# The agent directory is /opt/kervax-agent, NOT /opt/kervax. The panel installs into
# /opt/kervax by default (that is what quickstart.sh does), and the agent used to
# settle inside it and then run chown -R kervax across the whole directory. On a
# machine where the panel itself is monitored - an entirely ordinary case - that took
# over the panel's own ./data along with the postgres cluster: the running database
# instantly lost access to its files ("could not open file ... Permission denied"),
# connections stopped opening and the panel returned 500. It only recovered after a
# manual container restart.
# It also moves the agent out of the way of the documented panel removal
# (`rm -rf /opt/kervax`), which used to wipe the binary of a running agent.
BIN_DIR=/opt/kervax-agent/bin
BIN=$BIN_DIR/kervax-agent
LEGACY_BIN_DIR=/opt/kervax/bin
mkdir -p "$BIN_DIR"
# chmod too: mkdir -p leaves an existing directory as it is, and a reinstall has to fix
# the one an earlier run left at 0700.
chmod 0755 /opt/kervax-agent "$BIN_DIR"
# to a temp file plus mv: atomic, and it does not trip over "Text file busy" when
# another agent instance on the server already runs this binary.
# --connect-timeout: if the panel is behind a firewall this fails fast and clearly
# instead of hanging for minutes (check "server IP" in the panel and its firewall)
# -C - plus --retry: on a link that dies partway through a long transfer (a DPI in the
# path, a flaky uplink) a single-shot download never finishes - it dies at the same
# place every time and starts over. Resuming picks up where the previous attempt
# stopped, so the binary arrives across several passes.
# --retry-all-errors came in curl 7.71: on Ubuntu 20.04 (curl 7.68) the option was unknown, curl
# refused to start, and the installer blamed the panel's firewall. Only where curl knows it.
RETRY_ALL=""
{ curl --help all; curl --help; } 2>/dev/null | grep -q -- '--retry-all-errors' && RETRY_ALL="--retry-all-errors"
# --speed-limit/--speed-time: a transfer that stalls (the DPI drops the packets but keeps the
# connection) becomes an error after a minute and is retried from where it stopped; before,
# curl waited for it forever. The gzipped binary is 2.4 times smaller, which on a slow link
# is 15 minutes instead of 40; gzip checks it while unpacking (CRC). A node without gzip gets
# the plain binary.
fetch() {
  pcurl -fsSL --connect-timeout 15 --speed-limit 512 --speed-time 60 --retry 10 --retry-delay 5 \
       $RETRY_ALL -C - "$1" -o "$2"
}
rm -f "$BIN.new" "$BIN.new.gz"
if command -v gzip >/dev/null 2>&1 \
   && pcurl -fsI --connect-timeout 15 -m 60 "${URL%/}/api/agent/download-gz/$ARCH" >/dev/null 2>&1 \
   && fetch "${URL%/}/api/agent/download-gz/$ARCH" "$BIN.new.gz" \
   && gzip -dc "$BIN.new.gz" > "$BIN.new"; then
  rm -f "$BIN.new.gz"
else
  rm -f "$BIN.new" "$BIN.new.gz"
  fetch "${URL%/}/api/agent/download/$ARCH" "$BIN.new"
fi || {
  echo "✗ Could not download the agent from the panel: the transfer kept failing." >&2
  echo "  Run the command again. If the panel sits behind a firewall, check the server IP in its settings." >&2
  [ -n "$PROXY" ] && echo "  The download went through the proxy: check that it is reachable and lets this server in." >&2
  exit 1
}
chmod 0755 "$BIN.new"
mv -f "$BIN.new" "$BIN"
# ONLY its own directory: the agent needs the right to replace itself on update and
# nothing more. A recursive chown over the parent directory is exactly how it once
# took the panel's database away.
chown -R kervax "$BIN_DIR"
# clean up legacy paths from earlier installs, if any
rm -f /usr/local/bin/kervax-agent 2>/dev/null || true
if [ -d "$LEGACY_BIN_DIR" ] && [ "$LEGACY_BIN_DIR" != "$BIN_DIR" ]; then
  rm -rf "$LEGACY_BIN_DIR"
  # the panel directory is left alone: rmdir succeeds only if it became empty
  rmdir /opt/kervax 2>/dev/null || true
fi

if [ -n "$INSTANCE" ]; then
  CONF=/etc/kervax-agent-$INSTANCE.conf
  UNIT=kervax-agent@$INSTANCE
else
  CONF=/etc/kervax-agent.conf
  UNIT=kervax-agent
fi

umask 077
cat > "$CONF" <<EOF
url=$URL
token=$TOKEN
EOF
[ -n "$PROXY" ] && printf 'proxy=%s\n' "$PROXY" >> "$CONF"
chown kervax "$CONF" 2>/dev/null || true
chmod 0600 "$CONF"
umask 022

# single unit (default)
cat > /etc/systemd/system/kervax-agent.service <<'EOF'
[Unit]
Description=Kervax monitoring agent
After=network-online.target
Wants=network-online.target

[Service]
User=kervax
ExecStart=BIN_PLACEHOLDER/kervax-agent /etc/kervax-agent.conf
Restart=always
RestartSec=10
# systemd watchdog: the agent sends WATCHDOG=1 every cycle; if it goes quiet for longer
# than WatchdogSec (stuck collection or network), systemd restarts it. Independent of
# the Go watchdog, which once failed to fire. Type=notify requires READY=1 at startup
# (agent 1.77+ sends it).
Type=notify
NotifyAccess=main
WatchdogSec=180
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
# the agent updates itself by atomically replacing the binary - writes allowed ONLY here
ReadWritePaths=BIN_PLACEHOLDER
# read /dev/kmsg for the OOM victim name (dmesg_restrict=1 blocks unprivileged users)
AmbientCapabilities=CAP_SYSLOG
CapabilityBoundingSet=CAP_SYSLOG
[Install]
WantedBy=multi-user.target
EOF

# template unit for additional instances (kervax-agent@<name>)
cat > /etc/systemd/system/kervax-agent@.service <<'EOF'
[Unit]
Description=Kervax monitoring agent (%i)
After=network-online.target
Wants=network-online.target

[Service]
User=kervax
ExecStart=BIN_PLACEHOLDER/kervax-agent /etc/kervax-agent-%i.conf
Restart=always
RestartSec=10
Type=notify
NotifyAccess=main
WatchdogSec=180
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=BIN_PLACEHOLDER
# read /dev/kmsg for the OOM victim name (dmesg_restrict=1 blocks unprivileged users)
AmbientCapabilities=CAP_SYSLOG
CapabilityBoundingSet=CAP_SYSLOG
[Install]
WantedBy=multi-user.target
EOF

# The binary path is substituted here: the units are written with a quoted heredoc so
# the shell does not eat systemd specifiers such as %i.
sed -i "s|BIN_PLACEHOLDER|$BIN_DIR|g"     /etc/systemd/system/kervax-agent.service /etc/systemd/system/kervax-agent@.service

systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1 || true
# restart (not just --now): reinstalling over a running agent has to restart the
# process, otherwise it keeps running the old binary and unit.
if ! systemctl restart "$UNIT"; then
  # set -e used to end the script right here with nothing but systemd's own message
  echo "✗ $UNIT did not start. The end of its log:" >&2
  journalctl -u "$UNIT" -n 5 --no-pager >&2 2>/dev/null || true
  if findmnt -no OPTIONS -T "$BIN_DIR" 2>/dev/null | grep -qw noexec; then
    echo "  $BIN_DIR is on a noexec mount: nothing can be executed from there." >&2
  fi
  exit 1
fi
echo "✓ $UNIT installed and started. Logs: journalctl -u $UNIT -f"

# Docker access for the agent WITHOUT granting it root. A tiny socket-proxy (wollomatic)
# exposes ONLY a per-method allowlist: GET version/list/logs and the image list (build dates
# of internet-facing proxy images) plus POST restart/stop/start for a specific container.
# Everything else (exec, create, pull, build, volumes, host mounts) returns 403. No RCE, no host root. It has no
# network and listens on a unix socket in /var/lib/kervax/docker, a directory only root and the
# kervax group enter: neither containers nor other users of the node reach it (on 127.0.0.1:2375
# any of them could read the env of every container and stop it). No capabilities, read-only
# root, the image pinned by digest. The same command lives in dockerproxy-setup.sh and in the
# Docker section of the panel.
setup_docker_proxy() {
  command -v docker >/dev/null 2>&1 || { echo "· Docker not found - skipping the proxy." >&2; return; }
  echo "→ Setting up bounded Docker access (socket-proxy)..."
  DGID=$(stat -c %g /var/run/docker.sock 2>/dev/null || getent group docker 2>/dev/null | cut -d: -f3)
  KGID=$(getent group kervax 2>/dev/null | cut -d: -f3)
  install -d -o root -g kervax -m 2770 /var/lib/kervax/docker
  docker rm -f kervax-docker-proxy >/dev/null 2>&1 || true
  if [ -n "$KGID" ] && docker run -d --name kervax-docker-proxy --restart unless-stopped \
      --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
      --user "65534:${DGID:-999}" --group-add "$KGID" \
      -v /var/run/docker.sock:/var/run/docker.sock:ro -v /var/lib/kervax/docker:/run/kervax \
      wollomatic/socket-proxy:1.13.2@sha256:ff95d6ff6c27853005ef85eb8f4ab5923dc3d2f1e055f98fb6aeeaceec3fe848 \
        -loglevel warn -proxysocketendpoint /run/kervax/docker.sock -proxysocketendpointfilemode 0660 -shutdowngracetime 1 \
        -allowGET '^/(v[0-9.]+/)?(version|_ping|containers/json|images/json|containers/[a-zA-Z0-9_.-]+/(json|logs))$' \
        -allowPOST '^/(v[0-9.]+/)?containers/[a-zA-Z0-9_.-]+/(restart|stop|start)$' >/dev/null; then
    # the same file is rewritten: it stays the agent's (kervax, 0600)
    printf '%s\n' "$(grep -v '^docker_host=' "$CONF")" > "$CONF"
    printf 'docker_host=/var/lib/kervax/docker/docker.sock\n' >> "$CONF"
    echo "✓ Docker enabled (proxy: view plus restart/stop/start, no exec/create/root)."
  else
    echo "✗ Could not start the socket-proxy (is the docker daemon reachable?)." >&2
  fi
}

# download and run a setup helper from the panel (kube-setup/backupserver-setup/
# backup-setup). They self-detect and silently skip what does not apply. Needs bash.
run_remote_setup() {
  if ! command -v bash >/dev/null 2>&1; then echo "· $1: bash is required - skipping." >&2; return; fi
  # a shared route serving ANY helper from the directory. This used to be
  # /api/agent/$1.sh - only kube/backup/backupserver have personal URLs, so on a fresh
  # node webserver-setup silently got a 404 and was never installed.
  # to a file first, then bash: a helper piped straight into bash ran its first half when the
  # transfer broke off midway. </dev/null - as under the playbook, nobody answers questions
  f=$(mktemp) || return 0
  if pcurl -fsSL --compressed --connect-timeout 15 --speed-limit 512 --speed-time 60 --retry 3 \
       --retry-delay 5 $RETRY_ALL "${URL%/}/api/agent/setup/$1.sh" -o "$f"; then
    # at most 3 minutes for one helper: the rest wait behind it in a row, and the panel already
    # shows them as "not installed". One that did not make it is finished by the playbook later.
    rc=0
    if timeout -k 10 1 true 2>/dev/null; then
      timeout -k 10 180 bash "$f" </dev/null || rc=$?
    else
      bash "$f" </dev/null || rc=$?
    fi
    case $rc in
      0) ;;
      124|137) echo "· $1: did not finish in 3 minutes (skipping, the playbook will finish it)." >&2 ;;
      *) echo "· $1: could not run it (skipping)." >&2 ;;
    esac
  else
    echo "· $1: could not download it (skipping)." >&2
  fi
  rm -f "$f"
}

# Which helpers this node needs is decided BY THE HELPERS THEMSELVES, and the panel's
# index just carries their answers: KERVAX_SETUP_ALWAYS (safe anywhere) and
# KERVAX_SETUP_WHEN (a shell condition checked here, on the node). Nothing about any
# specific helper is written in this installer - the previous version had a hardcoded
# list, it was forgotten every time a helper was added, and a freshly installed node
# immediately asked for a manual step.
#
# The condition is executed as root, and it arrives from the panel - which this script
# already trusts completely: it downloads the helpers from there and pipes them into
# bash. (The ansible playbook is a different matter: it reads the same conditions from
# the repository, not over the network.)
install_applicable_setups() {
  idx=$(pcurl -fsSL --compressed --connect-timeout 15 -m 60 --retry 3 --retry-delay 5 "${URL%/}/api/agent/setup/index" 2>/dev/null) || {
    echo "· the helper index is unavailable - skipping the helpers." >&2; return; }
  # split BETWEEN entries, not on every '{': a condition may contain braces of its
  # own, and splitting on them tore an entry in half - the helper after it vanished
  # from the list entirely.
  # '%s\n', not '%s': without a trailing newline `read` never yields the last
  # entry, so the last helper in the index was silently not installed.
  printf '%s\n' "$idx" | sed 's/},{/}\n{/g' | while IFS= read -r row; do
    n=$(printf '%s' "$row" | sed -n 's/.*"name": *"\([^"]*\)".*/\1/p')
    [ -n "$n" ] || continue
    if ! printf '%s' "$row" | grep -q '"always": *true'; then
      cond=$(printf '%s' "$row" | sed -n 's/.*"when": *"\([^"]*\)".*/\1/p')
      [ -n "$cond" ] || continue                    # no condition, not safe everywhere
      ( eval "$cond" ) >/dev/null 2>&1 || continue  # this node is not it
    fi
    echo "→ Helper $n..."
    run_remote_setup "$n"
  done
}

# -- auto integrations: enable everything applicable so nothing has to be finished by
#    hand after adding a server. All privileges are NARROW (proxy/SA/sudoers). --
if [ "$NO_AUTO" = 0 ]; then
  # Docker
  command -v docker >/dev/null 2>&1 && setup_docker_proxy
  # everything that applies: helpers safe anywhere (watchdog, clock, domains, database
  # inventory) plus the ones whose own condition matches this node (Kubernetes access,
  # backup-server statistics, ...). Adding a node IS the explicit act, so helpers marked
  # "manual only" - the ones that grant the panel new access - are installed here too;
  # a fleet-wide playbook run deliberately leaves those alone.
  install_applicable_setups
  # restart so the agent picks up docker_host/kube.json/helpers
  systemctl restart "$UNIT" 2>/dev/null || true
elif [ "$WANT_DOCKER" = 1 ]; then
  setup_docker_proxy
  systemctl restart "$UNIT" 2>/dev/null || true
fi
}
