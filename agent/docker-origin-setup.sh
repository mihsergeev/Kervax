#!/usr/bin/env bash
# Read-only local image ancestry and restricted socket-proxy role evidence.
# Optional: unknown images stay warnings until fresh proof is available.
set -euo pipefail
KERVAX_SETUP_VERSION=0.1
KERVAX_SETUP_MANUAL=1
KERVAX_SETUP_WHEN="[ -S /var/run/docker.sock ]"
[ "$(id -u)" = 0 ] || { echo 'Root required.' >&2; exit 1; }
[ -x /usr/bin/python3 ] || { echo 'Install python3 first.' >&2; exit 1; }
umask 077
payload=$(mktemp)
trap 'rm -f -- "$payload"' EXIT
cat > "$payload" <<'PYTHON_COLLECTOR'
"""Read-only, bounded proof for unchanged socket-proxy image descendants."""
import gzip
import hashlib
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import tarfile
import tempfile
import time
from urllib.parse import quote

ROOT = Path('/var/lib/kervax')
REPORT = ROOT / 'report.d/docker-origin.json'
CACHE = ROOT / 'docker-origin/proofs.json'
ID = re.compile(r'sha256:[0-9a-f]{64}\Z')
CID = re.compile(r'[0-9a-f]{64}\Z')
PROFILE = 'wollomatic-socket-proxy-v1'
REPOSITORIES = {'wollomatic/socket-proxy', 'docker.io/wollomatic/socket-proxy', 'ghcr.io/wollomatic/socket-proxy'}
SOCKETS = {'/run/docker.sock', '/var/run/docker.sock'}
# Closed grammar: these are reviewed read-only endpoint sets, not a regex heuristic.
READ_PATTERNS = {
    '/_ping', '/version', '/info', '/events', '/containers/json', '/networks',
    '/(v[0-9]+\\.[0-9]+/)?(_ping|version|info|events|containers/json|networks(/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?)',
    '/(v[0-9]+\\.[0-9]+/)?(_ping|version|info|events|containers/json)',
}
MAX_EXPORT = 64 * 1024 * 1024
MAX_SMALL = 1024 * 1024


class Unknown(Exception):
    pass


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


class Docker:
    def __init__(self, seconds=35):
        self.deadline = time.monotonic() + seconds
        self.exported = 0

    def response(self, path):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise Unknown()
        connection = UnixHTTP('localhost', timeout=min(4, left))
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            if response.status != 200:
                raise Unknown()
            return connection, response
        except Exception:
            connection.close()
            raise

    def get(self, path):
        connection, response = self.response(path)
        try:
            value = response.read(4 * 1024 * 1024 + 1)
            if len(value) > 4 * 1024 * 1024:
                raise Unknown()
            return json.loads(value)
        finally:
            connection.close()

    def image(self, image):
        if not isinstance(image, str) or not ID.fullmatch(image):
            raise Unknown()
        value = self.get('/images/' + quote(image, safe='') + '/json')
        if not isinstance(value, dict) or value.get('Id') != image:
            raise Unknown()
        return value

    def export(self, image):
        connection, response = self.response('/images/' + quote(image, safe='') + '/get')
        try:
            # Full export must fit. A bounded BytesIO keeps the tar parser away
            # from the host filesystem; neither tar nor gzip is ever extracted.
            chunks = []
            size = 0
            while True:
                if time.monotonic() >= self.deadline:
                    raise Unknown()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                self.exported += len(chunk)
                if size > MAX_EXPORT or self.exported > MAX_EXPORT:
                    raise Unknown()
                chunks.append(chunk)
            return b''.join(chunks)
        finally:
            connection.close()


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def layers(image):
    value = (image.get('RootFS') or {}).get('Layers')
    if not isinstance(value, list) or not 1 <= len(value) <= 128 or any(not isinstance(x, str) or not ID.fullmatch(x) for x in value):
        raise Unknown()
    return value


def anchor(image):
    for value in image.get('RepoDigests') or []:
        if not isinstance(value, str):
            continue
        repository, separator, sha = value.partition('@')
        if separator and repository in REPOSITORIES and ID.fullmatch(sha):
            return value
    return None


def safe_layer(data, diff_id):
    if len(data) > MAX_SMALL:
        raise Unknown()
    if data.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            data = compressed.read(MAX_SMALL + 1)
            if len(data) > MAX_SMALL or compressed.read(1):
                raise Unknown()
    if digest(data) != diff_id:
        raise Unknown()
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        count = 0
        for member in archive:
            count += 1
            if count > 32 or member.pax_headers or member.linkname or member.size or member.uid or member.gid:
                raise Unknown()
            # Only Docker's empty bind mount placeholders are allowed. No
            # executable/config changes, whiteouts, links or special devices.
            if member.isdir() and member.name in ('var', 'var/run', 'run') and member.mode == 0o755:
                continue
            if member.isfile() and member.name in ('var/run/docker.sock', 'run/docker.sock') and member.mode in (0o644, 0o755):
                continue
            raise Unknown()


def verify_export(payload, image, base_layers):
    files = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:') as archive:
        for count, member in enumerate(archive, 1):
            if count > 512:
                raise Unknown()
            if not member.isfile():
                if not member.isdir():
                    raise Unknown()
                continue
            if member.name in files or len(files) >= 512:
                raise Unknown()
            if member.size <= MAX_SMALL:
                files[member.name] = archive.extractfile(member).read(MAX_SMALL + 1)
            else:
                files[member.name] = None
    manifest = json.loads(files['manifest.json'])
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise Unknown()
    selected = manifest[0]
    config = files[selected['Config']]
    if not isinstance(config, bytes):
        raise Unknown()
    # The classic store uses the config digest as image ID; the containerd
    # store uses the OCI manifest digest. Bind both formats cryptographically.
    if digest(config) != image['Id']:
        raw = files.get('blobs/sha256/' + image['Id'].split(':')[1])
        if not isinstance(raw, bytes) or digest(raw) != image['Id']:
            raise Unknown()
        immutable = json.loads(raw)
        if (immutable.get('schemaVersion') != 2 or immutable.get('mediaType') not in (
                'application/vnd.oci.image.manifest.v1+json',
                'application/vnd.docker.distribution.manifest.v2+json')):
            raise Unknown()
        descriptor = immutable.get('config') or {}
        if (descriptor.get('digest') != digest(config) or descriptor.get('size') != len(config)
                or selected['Config'] != 'blobs/sha256/' + digest(config).split(':')[1]):
            raise Unknown()
        descriptors = immutable.get('layers')
        if not isinstance(descriptors, list) or len(descriptors) != len(selected['Layers']):
            raise Unknown()
        for name, descriptor in zip(selected['Layers'], descriptors):
            sha = descriptor.get('digest')
            if not isinstance(sha, str) or not ID.fullmatch(sha) or name != 'blobs/sha256/' + sha.split(':')[1]:
                raise Unknown()
            data = files.get(name)
            if isinstance(data, bytes) and (digest(data) != sha or descriptor.get('size') != len(data)):
                raise Unknown()
    config = json.loads(config)
    image_layers = layers(image)
    if config.get('rootfs', {}).get('diff_ids') != image_layers:
        raise Unknown()
    names = selected['Layers']
    if not isinstance(names, list) or len(names) != len(image_layers) or image_layers[:len(base_layers)] != base_layers:
        raise Unknown()
    for name, sha in zip(names[len(base_layers):], image_layers[len(base_layers):]):
        data = files.get(name)
        if not isinstance(data, bytes):
            raise Unknown()
        safe_layer(data, sha)


def prove(api, image_id):
    image = api.image(image_id)
    current = image
    seen = set()
    for _ in range(5):
        identity = current.get('Id')
        if identity in seen:
            raise Unknown()
        seen.add(identity)
        trusted = anchor(current)
        if trusted:
            base = current
            break
        parent = current.get('Parent')
        if not isinstance(parent, str) or not ID.fullmatch(parent):
            raise Unknown()
        parent_image = api.image(parent)
        if layers(current)[:len(layers(parent_image))] != layers(parent_image):
            raise Unknown()
        current = parent_image
    else:
        raise Unknown()
    entrypoint = (base.get('Config') or {}).get('Entrypoint')
    if entrypoint != ['/socket-proxy'] or (image.get('Config') or {}).get('Entrypoint') != entrypoint:
        raise Unknown()
    if (image.get('Config') or {}).get('WorkingDir', '') not in ('', '/'):
        raise Unknown()
    base_layers = layers(base)
    if layers(image) != base_layers:
        if not isinstance(image.get('Size'), int) or not 0 < image['Size'] <= MAX_EXPORT:
            raise Unknown()
        verify_export(api.export(image_id), image, base_layers)
    return {'source_id': base['Id'], 'source_digest': trusted, 'profile': PROFILE, 'fs_verified': True}


def limited_clients(value):
    clients = value.split(',')
    if not 1 <= len(clients) <= 8:
        return False
    for item in clients:
        try:
            address = ipaddress.ip_address(item)
            if address.is_unspecified or address.is_multicast:
                return False
        except ValueError:
            if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?', item):
                return False
    return True


def restricted_command(config):
    if config.get('Entrypoint') != ['/socket-proxy']:
        return False
    environment = config.get('Env') or []
    if not isinstance(environment, list):
        return False
    keys = set()
    for value in environment:
        if not isinstance(value, str) or not (value.startswith('PATH=') or value == 'GODEBUG=multipathtcp=0'):
            return False
        key = value.partition('=')[0]
        if key in keys:
            return False
        keys.add(key)
    command = config.get('Cmd')
    if not isinstance(command, list) or not 1 <= len(command) <= 24:
        return False
    options = {}
    for arg in command:
        if not isinstance(arg, str) or len(arg) > 512 or not arg.startswith('-') or arg.startswith('--'):
            return False
        key, separator, value = arg[1:].partition('=')
        if key in options:
            return False
        if key == 'stoponwatchdog':
            if separator and value != 'true':
                return False
        elif not separator:
            return False
        options[key] = value
    for key, value in options.items():
        if key in ('allowGET', 'allowHEAD'):
            if value not in READ_PATTERNS:
                return False
        elif key == 'allowfrom':
            if not limited_clients(value):
                return False
        elif key in ('shutdowngracetime', 'watchdoginterval', 'listenport'):
            if not value.isascii() or not value.isdigit() or int(value) > 86400:
                return False
        elif key == 'listenip':
            try:
                ipaddress.ip_address(value)
            except ValueError:
                return False
        elif key == 'loglevel':
            if value not in ('warn', 'info', 'error'):
                return False
        elif key == 'socketpath':
            if value not in SOCKETS:
                return False
        elif key != 'stoponwatchdog':
            return False
    return bool(options.get('allowGET')) and bool(options.get('allowfrom'))


def restricted_runtime(api, container):
    config = container.get('Config') or {}
    host = container.get('HostConfig') or {}
    user = str(config.get('User') or '')
    if not re.fullmatch(r'[1-9][0-9]*(?::[0-9]+)?', user) or not restricted_command(config):
        return False
    if host.get('Privileged') is not False or host.get('ReadonlyRootfs') is not True or host.get('CapAdd'):
        return False
    if set(host.get('CapDrop') or []) != {'ALL'} or set(host.get('SecurityOpt') or []) not in ({'no-new-privileges'}, {'no-new-privileges:true'}):
        return False
    if any(host.get(k) for k in ('Devices', 'DeviceRequests', 'DeviceCgroupRules', 'GroupAdd', 'VolumesFrom', 'Tmpfs', 'PortBindings')):
        return False
    if host.get('Runtime') not in (None, '', 'runc'):
        return False
    if (any(host.get(k) not in (None, '') for k in ('PidMode', 'UTSMode', 'UsernsMode'))
            or host.get('IpcMode') not in (None, '', 'private')
            or host.get('NetworkMode') in ('host', 'none')
            or str(host.get('NetworkMode') or '').startswith('container:')):
        return False
    if host.get('Sysctls') not in (None, {}, {'net.mptcp.enabled': '0'}):
        return False
    mounts = container.get('Mounts')
    if not isinstance(mounts, list) or len(mounts) != 1:
        return False
    mount = mounts[0]
    if mount.get('Type') != 'bind' or mount.get('Source') not in SOCKETS or mount.get('Destination') not in SOCKETS or mount.get('RW') is not False:
        return False
    networks = (container.get('NetworkSettings') or {}).get('Networks')
    if not isinstance(networks, dict) or not 1 <= len(networks) <= 4:
        return False
    for network in networks.values():
        identity = network.get('NetworkID')
        if not isinstance(identity, str) or not CID.fullmatch(identity):
            return False
        if api.get('/networks/' + identity).get('Internal') is not True:
            return False
    return True


def safe_path(path, directory=False):
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_mode & 0o022:
            raise Unknown()
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid or info.st_mode & 0o022:
        raise Unknown()


def atomic(path, value, mode):
    safe_path(path.parent, directory=True)
    if path.exists() or path.is_symlink():
        safe_path(path)
    fd, name = tempfile.mkstemp(prefix='.docker-origin-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, separators=(',', ':'))
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def collect(api, cached, now):
    rows = api.get('/containers/json?all=0')
    if not isinstance(rows, list) or len(rows) > 4096:
        raise Unknown()
    output = []
    attempted = 0
    for row in rows:
        if not isinstance(row, dict) or not any(m.get('Source') in SOCKETS for m in row.get('Mounts') or [] if isinstance(m, dict)):
            continue
        attempted += 1
        if attempted > 8:
            break
        identity = row.get('Id')
        if not isinstance(identity, str) or not CID.fullmatch(identity):
            continue
        try:
            container = api.get('/containers/' + identity + '/json')
            image = container.get('Image')
            if container.get('Id') != identity or not isinstance(image, str) or not ID.fullmatch(image):
                raise Unknown()
            proof = cached.get(image)
            if not isinstance(proof, dict) or proof.get('profile') != PROFILE or proof.get('fs_verified') is not True:
                proof = prove(api, image)
                cached[image] = proof
            if not restricted_runtime(api, container):
                continue
            output.append({**proof, 'id': identity, 'image_id': image,
                           'config_image': (container.get('Config') or {}).get('Image'), 'restricted_runtime': True})
        except Exception:
            continue
    return {'v': 1, 'ts': now, 'containers': output}


def main():
    now = int(time.time())
    cached = {}
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    try:
        safe_path(CACHE)
        if CACHE.stat().st_size <= 1024 * 1024:
            old = json.loads(CACHE.read_text())
            if old.get('code') == code and isinstance(old.get('proofs'), dict):
                cached = dict(list(old['proofs'].items())[-128:])
    except Exception:
        pass
    try:
        report = collect(Docker(), cached, now)
    except Exception:
        report = {'v': 1, 'ts': now, 'containers': [], 'error': 'inspection_unavailable'}
    atomic(REPORT, report, 0o644)
    atomic(CACHE, {'code': code, 'proofs': dict(list(cached.items())[-128:])}, 0o600)


if __name__ == '__main__':
    main()
PYTHON_COLLECTOR
/usr/bin/python3 - "$payload" <<'PYTHON_INSTALL'
from pathlib import Path
import os,stat,sys,tempfile

def ensure(path,mode=0o755):
    for p in list(reversed(path.parents))+[path]:
        if not p.exists() and not p.is_symlink():
            p.mkdir(mode=mode if p==path else 0o755)
        s=p.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:
            raise SystemExit('Unsafe helper directory; nothing will be installed there.')

def write(path,data,mode):
    ensure(path.parent)
    if path.exists() or path.is_symlink():
        s=path.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:
            raise SystemExit('Unsafe helper file; nothing will be overwritten.')
    fd,tmp=tempfile.mkstemp(prefix='.docker-origin-',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fchmod(f.fileno(),mode);os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

for p in ('/lib65/scripts','/var/lib/kervax/report.d','/var/lib/kervax/versions'):
    ensure(Path(p))
ensure(Path('/var/lib/kervax/docker-origin'),0o700)
write(Path('/lib65/scripts/kervax-docker-origin.py'),Path(sys.argv[1]).read_bytes(),0o755)
service=b"""[Unit]
Description=Kervax read-only Docker image origin evidence
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /lib65/scripts/kervax-docker-origin.py
TimeoutStartSec=45
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX
ReadWritePaths=/var/lib/kervax/report.d /var/lib/kervax/docker-origin
MemoryMax=256M
CPUQuota=50%
"""
timer=b"""[Unit]
Description=Kervax Docker image origin evidence refresh
[Timer]
OnBootSec=30s
OnUnitActiveSec=120s
AccuracySec=5s
Unit=kervax-docker-origin.service
[Install]
WantedBy=timers.target
"""
write(Path('/etc/systemd/system/kervax-docker-origin.service'),service,0o644)
write(Path('/etc/systemd/system/kervax-docker-origin.timer'),timer,0o644)
write(Path('/var/lib/kervax/versions/docker-origin-setup.ver'),b'0.1\n',0o644)
PYTHON_INSTALL
systemctl daemon-reload
systemctl enable --now kervax-docker-origin.timer >/dev/null
systemctl start kervax-docker-origin.service
echo 'Read-only Docker image origin collector installed.'
