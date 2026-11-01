#!/usr/bin/env bash
# Fresh read-only collectors for the home page. No arbitrary command, URL or
# path crosses the agent/root boundary; this dispatcher never runs backup refresh.
set -euo pipefail
KERVAX_SETUP_VERSION=0.1
KERVAX_SETUP_ALWAYS=1
[ "$(id -u)" = 0 ] || { echo 'Root required.' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo 'Install python3, then run recheck-setup again.' >&2; exit 1; }
id kervax >/dev/null 2>&1 || { echo 'Install the agent account first.' >&2; exit 1; }
install -d -m 0755 /lib65/scripts /var/lib/kervax /var/lib/kervax/versions
install -d -o root -g kervax -m 0730 /var/lib/kervax/recheck-req
install -d -o root -g kervax -m 0750 /var/lib/kervax/recheck-res
install -d -o kervax -g kervax -m 0700 /var/lib/kervax/recheck-work
# Setup may update the agent's sandbox; a verification request never does.
# This helper is standalone and does not rely on backup-setup's broader spool.
if systemctl cat kervax-agent >/dev/null 2>&1; then
  install -d -m 0755 /etc/systemd/system/kervax-agent.service.d
  cat > /etc/systemd/system/kervax-agent.service.d/kervax-recheck.conf <<'DROPIN'
[Service]
ReadWritePaths=/var/lib/kervax/recheck-req /var/lib/kervax/recheck-work
DROPIN
  systemctl daemon-reload
  systemctl try-restart kervax-agent
fi
cat > /lib65/scripts/kervax-recheck.new <<'PY'
#!/usr/bin/env python3
"""Untrusted agent requests -> fixed read-only collectors -> root-owned receipts."""
import fcntl
import grp
import json
import os
import pwd
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path('/var/lib/kervax')
REQUESTS = ROOT / 'recheck-req'
RESULTS = ROOT / 'recheck-res'
NAME = re.compile(r'([0-9a-f]{32})\.json\Z')
UID = pwd.getpwnam('kervax').pw_uid
GID = grp.getgrnam('kervax').gr_gid
MAX = 4_000_000


def run(argv):
    # No shell and no inherited request-supplied environment.
    with tempfile.TemporaryFile() as output:
        subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=output,
                       stderr=subprocess.DEVNULL, timeout=110,
                       env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root'}, check=True)
        output.seek(0)
        data = output.read(MAX + 1)
        if len(data) > MAX:
            raise ValueError('oversized collector')
        return data


def read_root_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022 or st.st_size > MAX:
            raise ValueError('unsafe collector file')
        return json.loads(os.read(fd, MAX + 1))
    finally:
        os.close(fd)


def collect_file(helper, path):
    before = os.stat(path, follow_symlinks=False).st_mtime_ns if path.exists() else 0
    start = time.time_ns()
    run([helper])
    st = os.stat(path, follow_symlinks=False)
    if st.st_mtime_ns <= before or st.st_mtime_ns < start:
        raise ValueError('collector returned an old block')
    return read_root_json(path)


def write_cache(path, doc):
    # The paths below are fixed root-owned report caches, never request fields.
    # Ordinary reports must continue the fresh observation instead of replaying
    # the pre-inspection cache until its normal timer next runs.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.recheck-', delete=False) as f:
        tmp = Path(f.name)
        f.write(json.dumps(doc).encode())
    try:
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def collect(req):
    src = req['source']
    out = {}
    if src == 'kube_expiry':
        doc = collect_file('/lib65/kervax/kervax-kube-expiry', ROOT / 'kube-expiry.json')
        if not isinstance(doc.get('items'), list) or not isinstance(doc.get('flux'), list):
            raise ValueError('incomplete cluster collector')
        out = {'kube_expiry': doc['items'], 'flux': doc['flux']}
    elif src == 'changes':
        doc = collect_file('/lib65/kervax/kervax-changes', ROOT / 'report.d/changes.json')
        out = {'extras': {'changes': doc}}
    elif src in ('software', 'backup'):
        if src == 'backup':
            # inspect is a separate action in backup-helper 0.50. refresh repairs
            # configuration and can start retries: it is NEVER acceptable here.
            doc = json.loads(run(['/lib65/kervax/kervax-backup-helper', 'inspect']))
            if doc.get('v') != 1 or doc.get('complete') is not True or not isinstance(doc.get('backup_config'), dict) or not isinstance(doc.get('custom_backups'), dict):
                raise ValueError('incomplete backup inspection')
            out['inspect'] = doc
        if req.get('db_stats'):
            doc = collect_file('/lib65/kervax/kervax-db-stats', ROOT / 'db-stats.json')
            if not isinstance(doc.get('items'), list):
                raise ValueError('incomplete database collector')
            out['db_stats'] = doc['items']
    else:
        raise ValueError('unsupported collector')
    if src == 'backup':
        doc = out['inspect']
        write_cache(ROOT / 'backup-config.json', doc['backup_config'])
        write_cache(ROOT / 'report.d/custom-backups.json', doc['custom_backups'])
    return out


def request(path, request_id):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != UID or st.st_nlink != 1 or st.st_size > 2048 or time.time() - st.st_mtime > 180:
            raise ValueError('unsafe request')
        req = json.loads(os.read(fd, 2049))
    finally:
        os.close(fd)
    if set(req) != {'id', 'source', 'db_stats'} or req['id'] != request_id or type(req['db_stats']) is not bool or req['source'] not in {'kube_expiry', 'changes', 'software', 'backup'}:
        raise ValueError('invalid request')
    return req


def receipt(request_id, ok, snapshot):
    data = json.dumps({'id': request_id, 'ok': ok, 'snapshot': snapshot}).encode()
    if len(data) > MAX:
        data = json.dumps({'id': request_id, 'ok': False, 'snapshot': {}}).encode()
    path = RESULTS / (request_id + '.json')
    tmp = RESULTS / (request_id + '.tmp')
    tmp.unlink(missing_ok=True)  # interrupted receipt; directory is root-writable only
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o640)
    try:
        os.fchown(fd, 0, GID)
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def main():
    with open('/run/kervax-recheck.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # .pending/.ready files belong to an agent still publishing a request.
        # Dispatch another completed request without touching those writers.
        for path in list(REQUESTS.glob('*.json'))[:32]:
            match = NAME.fullmatch(path.name)
            if not match:
                # Unknown names cannot trigger a path-unit loop indefinitely.
                if not stat.S_ISDIR(path.lstat().st_mode):
                    path.unlink(missing_ok=True)
                else:
                    try:
                        path.rmdir()
                    except OSError:
                        pass
                continue
            request_id = match[1]
            try:
                req = request(path, request_id)
                path.unlink(missing_ok=True)
                snapshot = collect(req)
                receipt(request_id, True, snapshot)
            except Exception:
                path.unlink(missing_ok=True)
                receipt(request_id, False, {})
        for path in RESULTS.iterdir():
            if NAME.fullmatch(path.name) and time.time() - path.lstat().st_mtime > 3600:
                path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
PY
chmod 0755 /lib65/scripts/kervax-recheck.new
chown root:root /lib65/scripts/kervax-recheck.new
mv -f /lib65/scripts/kervax-recheck.new /lib65/scripts/kervax-recheck
cat > /etc/systemd/system/kervax-recheck.service <<'UNIT'
[Unit]
Description=Kervax fresh read-only collectors
[Service]
Type=oneshot
ExecStart=/lib65/scripts/kervax-recheck
TimeoutStartSec=250
UNIT
cat > /etc/systemd/system/kervax-recheck.path <<'UNIT'
[Unit]
Description=Kervax fresh collector requests
[Path]
PathExistsGlob=/var/lib/kervax/recheck-req/*.json
Unit=kervax-recheck.service
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now kervax-recheck.path >/dev/null
printf '%s\n' "$KERVAX_SETUP_VERSION" > /var/lib/kervax/versions/recheck-setup.ver
chmod 0644 /var/lib/kervax/versions/recheck-setup.ver
echo 'Fresh read-only collectors installed.'
