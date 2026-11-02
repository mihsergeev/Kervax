#!/bin/sh
# Optional host helper: existing installations without the guard stay unchanged.
set -eu
PREPARE=0
if [ "${1:-}" = --prepare ]; then PREPARE=1; shift; fi
PANEL_DIR=${1:-$(pwd)}
if [ "$#" -gt 0 ]; then shift; fi
SOURCE_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
guard=$(python3 "$SOURCE_DIR/agent-source-guard-enabled.py" --dir "$PANEL_DIR" "$@")
[ "$guard" = 1 ] || [ "$PREPARE" = 1 ] || exit 0
[ "$(id -u)" = 0 ] || { echo "proxy peer setup requires root" >&2; exit 1; }
[ -f /etc/kervax/agent-proxy-peers.env ] || { echo "configure /etc/kervax/agent-proxy-peers.env before enabling the guard" >&2; exit 1; }
python3 - <<'PY'
import os, stat
path = '/etc/kervax/agent-proxy-peers.env'
info = os.lstat(path)
if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
    raise SystemExit('proxy peer configuration must be root-owned and not writable by group/others')
PY
install -d -m 0755 /lib65/kervax
install -o root -g root -m 0700 "$SOURCE_DIR/agent-proxy-peers-sync.py" /lib65/kervax/agent-proxy-peers-sync.py
install -o root -g root -m 0700 "$SOURCE_DIR/agent-source-guard-enabled.py" /lib65/kervax/agent-source-guard-enabled.py
install -o root -g root -m 0644 "$SOURCE_DIR/kervax-agent-proxy-peers.service" /etc/systemd/system/kervax-agent-proxy-peers.service
install -o root -g root -m 0644 "$SOURCE_DIR/kervax-agent-proxy-peers.timer" /etc/systemd/system/kervax-agent-proxy-peers.timer
systemctl daemon-reload
systemctl restart kervax-agent-proxy-peers.service
systemctl enable --now kervax-agent-proxy-peers.timer
