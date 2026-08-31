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
# Russian version of this installer: agent/install-ru.sh
set -eu

# Argument parsing: positional <url> <token> [instance] plus flags.
# By default (auto mode) install.sh enables EVERYTHING applicable on the node: bounded
# Docker access (socket-proxy), read-only Kubernetes (a narrow SA), backup server
# statistics, restic backup control. Each step self-detects. --no-auto installs the
# agent only.
URL=""; TOKEN=""; INSTANCE=""; WANT_DOCKER=0; NO_AUTO=0; UNINSTALL=0
for a in "$@"; do
  case "$a" in
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
  # helper units and timers (panel requests, inventory, dumps, domains)
  if [ -z "$INSTANCE" ]; then
    # .path as well: helpers catch panel requests with a path unit, and without this
    # kervax-*-req.path files were left behind in /etc/systemd/system
    for u in kervax-backup-req kervax-bsrv-req kervax-tsync-req \
             kervax-db-stats kervax-dumps kervax-web-sites; do
      for kind in timer path service; do
        systemctl disable --now "$u.$kind" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$u.$kind"
      done
    done
    rm -f /etc/systemd/system/kervax-agent.service /etc/systemd/system/kervax-agent@.service
    # unit drop-ins (spool, watchdog) live in a directory next to it — otherwise it stays
    rm -rf /etc/systemd/system/kervax-agent.service.d /etc/systemd/system/kervax-agent@.service.d
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true

  rm -f $CONFS
  if [ -z "$INSTANCE" ]; then
    command -v docker >/dev/null 2>&1 && docker rm -f kervax-docker-proxy >/dev/null 2>&1 || true
    rm -rf /opt/kervax-agent /opt/kervax/bin /lib65/kervax /etc/kervax
    rmdir /opt/kervax /lib65 2>/dev/null || true   # only if empty (the panel directory survives)
    rm -f /etc/sudoers.d/kervax-backup /etc/sudoers.d/kervax-backupserver
    id kervax >/dev/null 2>&1 && { userdel kervax >/dev/null 2>&1 || deluser kervax >/dev/null 2>&1 || true; }
    echo "✓ Agent removed: service, helper units, binary, configs, sudoers, user kervax."
    echo "  The Kubernetes account (if it was enabled) stays in the cluster:"
    echo "    kubectl -n kervax delete serviceaccount kervax    # if you want it gone"
    echo "  Delete the server in the panel separately — that revokes its token."
  else
    echo "✓ Instance $INSTANCE removed (the shared binary and other instances are untouched)."
  fi
  exit 0
fi

if [ -z "$URL" ] || [ -z "$TOKEN" ]; then
  echo "Usage: install.sh <panel_url> <token> [instance] [--no-auto]" >&2
  echo "       install.sh --uninstall [instance]" >&2
  exit 1
fi
if [ -n "$INSTANCE" ] && ! printf '%s' "$INSTANCE" | grep -Eq '^[A-Za-z0-9_-]+$'; then
  echo "Instance name may contain only letters, digits, hyphen and underscore." >&2
  exit 1
fi

if [ "$(id -u)" != "0" ]; then
  echo "Root required (sudo)." >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) echo "Architecture $(uname -m) is not supported (amd64/arm64 required)." >&2; exit 1 ;;
esac

# unprivileged system user
id kervax >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin kervax 2>/dev/null || \
  adduser --system --no-create-home --shell /usr/sbin/nologin kervax 2>/dev/null || true

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
# to a temp file plus mv: atomic, and it does not trip over "Text file busy" when
# another agent instance on the server already runs this binary.
# --connect-timeout: if the panel is behind a firewall this fails fast and clearly
# instead of hanging for minutes (check "server IP" in the panel and its firewall)
curl -fsSL --connect-timeout 15 "${URL%/}/api/agent/download/$ARCH" -o "$BIN.new" || {
  echo "✗ The panel is unreachable from this server. If it sits behind a firewall," >&2
  echo "  add the server IP in its settings, wait about 2 minutes and retry." >&2
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
chown kervax "$CONF" 2>/dev/null || true
chmod 0600 "$CONF"

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
systemctl restart "$UNIT"
echo "✓ $UNIT installed and started. Logs: journalctl -u $UNIT -f"

# Docker access for the agent WITHOUT granting it root. A tiny socket-proxy (wollomatic)
# exposes ONLY a per-method allowlist: GET version/list/logs plus POST
# restart/stop/start for a specific container. Everything else (exec, create, images,
# build, volumes, host mounts) returns 403. No RCE, no host root. Binds to 127.0.0.1.
setup_docker_proxy() {
  command -v docker >/dev/null 2>&1 || { echo "· Docker not found - skipping the proxy." >&2; return; }
  echo "→ Setting up bounded Docker access (socket-proxy)..."
  DGID=$(getent group docker 2>/dev/null | cut -d: -f3)
  docker rm -f kervax-docker-proxy >/dev/null 2>&1 || true
  if docker run -d --name kervax-docker-proxy --restart unless-stopped \
      --user "65534:${DGID:-999}" \
      -v /var/run/docker.sock:/var/run/docker.sock:ro \
      -p 127.0.0.1:2375:2375 \
      wollomatic/socket-proxy:1 \
        -loglevel warn -listenip 0.0.0.0 -allowfrom 0.0.0.0/0 -shutdowngracetime 1 \
        -allowGET '^/(v[0-9.]+/)?(version|info|_ping|containers/json|containers/[a-zA-Z0-9_.-]+/(json|logs))' \
        -allowPOST '^/(v[0-9.]+/)?containers/[a-zA-Z0-9_.-]+/(restart|stop|start)$' >/dev/null; then
    grep -q '^docker_host=' "$CONF" || printf 'docker_host=tcp://127.0.0.1:2375\n' >> "$CONF"
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
  curl -fsSL --connect-timeout 15 "${URL%/}/api/agent/setup/$1.sh" | bash \
    || echo "· $1: could not run it (skipping)." >&2
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
  idx=$(curl -fsSL --connect-timeout 15 "${URL%/}/api/agent/setup/index" 2>/dev/null) || {
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
