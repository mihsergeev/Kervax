"""Opaque image identity is not a purpose; only bounded root evidence grants SOCK."""
from copy import deepcopy
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import types

import pytest

from app import docker_origin, docker_risk, setup_scripts
from tests.test_docker_risk import _c, _rep, _srv, _names

SCRIPT = Path(__file__).parents[2] / 'agent/docker-origin-setup.sh'
SOURCE = SCRIPT.read_text(encoding='utf-8').split("<<'PYTHON_COLLECTOR'\n", 1)[1].split('\nPYTHON_COLLECTOR\n', 1)[0]
collector = types.ModuleType('origin_collector')
collector.__file__ = str(SCRIPT)
exec(compile(SOURCE, str(SCRIPT), 'exec'), collector.__dict__)
BASE = 'sha256:' + 'a' * 64
IMAGE = 'sha256:' + 'b' * 64
CONTAINER = 'c' * 64
NETWORK = 'd' * 64
DIGEST = 'wollomatic/socket-proxy@sha256:' + 'f' * 64
READ = max(collector.READ_PATTERNS, key=len)


def report(name='edge-api', image=IMAGE):
    c = _c(name, image, binds=['/var/run/docker.sock'], ro=['/var/run/docker.sock'], id=CONTAINER[:12])
    r = _rep(c)
    r['clock_unix'] = 1800000000
    r['extras'] = {'docker-origin': {'v': 1, 'ts': r['clock_unix'], 'containers': [{
        'id': CONTAINER, 'image_id': IMAGE, 'config_image': image, 'source_id': BASE,
        'source_digest': DIGEST, 'profile': collector.PROFILE, 'fs_verified': True, 'restricted_runtime': True}]}}
    return r, c, r['extras']['docker-origin']['containers'][0]


def test_confirmed_hash_or_local_tag_gets_only_socket_allowance():
    for image in (IMAGE, 'local/rollback:fixture'):
        r, _, _ = report(image=image)
        assert _names(r) == {}
        assert docker_origin.verified_socket(r, r['docker']['containers'][0])


@pytest.mark.parametrize('name', ['edge-api', 'docker-api', 'kervax-docker-proxy'])
def test_container_name_or_rollback_tag_is_not_evidence(name):
    for image in (IMAGE, 'local/rollback:fixture', 'example.invalid/unrelated:fixture'):
        r, _, _ = report(name, image)
        r['extras'] = {}
        assert _names(r) == {name: ['sock']}


@pytest.mark.parametrize('key,value', [
    ('source_digest', 'example.invalid/socket-proxy@sha256:' + 'f' * 64),
    ('source_digest', 'wollomatic/socket-proxy:fixture'), ('source_id', 'not-a-digest'),
    ('image_id', 'sha256:' + 'e' * 64), ('id', 'e' * 64), ('config_image', 'local/other:fixture'),
    ('profile', 'unverified'), ('fs_verified', 1), ('restricted_runtime', False),
])
def test_missing_or_conflicting_proof_does_not_remove_warning(key, value):
    r, _, proof = report()
    proof[key] = value
    assert _names(r) == {'edge-api': ['sock']}


@pytest.mark.parametrize('age', [-1, 181, float('nan'), float('inf')])
def test_stale_or_invalid_observation_is_not_authority(age):
    r, _, _ = report()
    r['extras']['docker-origin']['ts'] -= age
    assert _names(r) == {'edge-api': ['sock']}


def test_ambiguous_identity_is_rejected():
    r, _, proof = report()
    r['extras']['docker-origin']['containers'].append(deepcopy(proof))
    assert _names(r) == {'edge-api': ['sock']}


@pytest.mark.parametrize('extra,expected', [
    ({'priv': True}, ['priv', 'sock']), ({'caps': ['SYS_ADMIN']}, ['caps', 'sock']),
    ({'binds': ['/var/run/docker.sock', '/etc']}, ['sock', 'host']),
    ({'pid_host': True}, ['sock']), ({'ports': [{'pub': 12345}]}, ['sock']),
    ({'ro': []}, ['sock']), ({'vols': [{'dst': '/socket-proxy'}]}, ['sock']),
])
def test_extra_permissions_survive_verified_family(extra, expected):
    r, c, _ = report()
    c.update(extra)
    assert _names(r) == {'edge-api': expected}


def test_origin_helper_needed_only_for_unexplained_sockets_or_existing_collector():
    r, _, _ = report()
    r['extras'] = {}
    assert setup_scripts.setup_needed('docker-origin-setup', r)
    assert not setup_scripts.setup_needed('docker-origin-setup', _rep(_c('application', 'example.invalid/app:fixture')))
    assert not setup_scripts.setup_needed('docker-origin-setup', _rep(_c('proxy', 'wollomatic/socket-proxy:fixture', binds=['/var/run/docker.sock'])))
    assert setup_scripts.setup_needed('docker-origin-setup', {'extras': {'docker-origin': {}}})


def runtime():
    return {'Id': CONTAINER, 'Image': IMAGE, 'Config': {
        'Image': IMAGE, 'Entrypoint': ['/socket-proxy'], 'Env': ['PATH=/usr/bin'],
        'User': '65534:999', 'Cmd': ['-listenip=0.0.0.0', '-allowfrom=edge-client', '-allowGET=' + READ,
                                   '-allowHEAD=/_ping', '-stoponwatchdog', '-watchdoginterval=600']},
        'HostConfig': {'Privileged': False, 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                       'SecurityOpt': ['no-new-privileges:true'], 'IpcMode': 'private'},
        'Mounts': [{'Type': 'bind', 'Source': '/var/run/docker.sock', 'Destination': '/var/run/docker.sock', 'RW': False}],
        'NetworkSettings': {'Networks': {'private-net': {'NetworkID': NETWORK}}}}


class Networks:
    def get(self, path):
        assert path == '/networks/' + NETWORK
        return {'Internal': True}


def test_strict_runtime_matches_hardened_cli_configuration():
    assert collector.restricted_runtime(Networks(), runtime())
    c = runtime()
    c['Config']['Env'].append('GODEBUG=multipathtcp=0')
    assert collector.restricted_runtime(Networks(), c)


@pytest.mark.parametrize('section,key,value', [
    ('HostConfig', 'Privileged', True), ('HostConfig', 'ReadonlyRootfs', False),
    ('HostConfig', 'CapAdd', ['SYS_ADMIN']), ('HostConfig', 'CapDrop', []),
    ('HostConfig', 'SecurityOpt', ['no-new-privileges:true', 'seccomp=unconfined']),
    ('HostConfig', 'PidMode', 'host'), ('HostConfig', 'IpcMode', 'container:fixture'),
    ('HostConfig', 'NetworkMode', 'host'), ('HostConfig', 'Devices', [{}]),
    ('HostConfig', 'DeviceCgroupRules', ['a *:* rwm']), ('HostConfig', 'Runtime', 'custom-runtime'),
    ('HostConfig', 'PortBindings', {'2375/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '12345'}]}),
    ('Config', 'User', '0:999'), ('Config', 'Entrypoint', ['/bin/sh']),
    ('Config', 'Env', ['PATH=/usr/bin', 'SP_ALLOW_POST=.*']),
    ('Config', 'Env', ['GODEBUG=multipathtcp=0,tlsrsakex=1']),
    ('Config', 'Env', ['GODEBUG=multipathtcp=0', 'GODEBUG=multipathtcp=1']),
])
def test_runtime_profile_never_accepts_extra_rights(section, key, value):
    c = runtime()
    c[section][key] = value
    assert not collector.restricted_runtime(Networks(), c)


@pytest.mark.parametrize('argument', ['-allowPOST=.*', '-allowGET=.*', '-allowfrom=0.0.0.0/0',
                                      '-allowfrom=*', '-proxycontainername=proxy', '-unknown=1'])
def test_unrestricted_or_unknown_command_options_are_rejected(argument):
    c = runtime()
    key = argument.partition('=')[0]
    c['Config']['Cmd'] = [x for x in c['Config']['Cmd'] if x.partition('=')[0] != key] + [argument]
    assert not collector.restricted_runtime(Networks(), c)


def test_mount_over_binary_or_external_network_is_not_an_expected_proxy():
    c = runtime()
    c['Mounts'].append({'Type': 'bind', 'Source': '/srv/example', 'Destination': '/socket-proxy', 'RW': False})
    assert not collector.restricted_runtime(Networks(), c)
    class External(Networks):
        def get(self, path):
            return {'Internal': False}
    assert not collector.restricted_runtime(External(), runtime())


def tar(entries):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as archive:
        for name, payload, kind, mode in entries:
            item = tarfile.TarInfo(name)
            item.type, item.mode, item.size = kind, mode, len(payload)
            if kind == tarfile.SYMTYPE:
                item.linkname = '/socket-proxy'
            archive.addfile(item, io.BytesIO(payload))
    return out.getvalue()


def stub():
    return tar([('var', b'', tarfile.DIRTYPE, 0o755), ('var/run', b'', tarfile.DIRTYPE, 0o755),
                ('var/run/docker.sock', b'', tarfile.REGTYPE, 0o644)])


def images(delta=None, compressed=False):
    delta = stub() if delta is None else delta
    diff = collector.digest(delta)
    layer_ids = ['sha256:' + 'd' * 64, diff]
    config = json.dumps({'rootfs': {'type': 'layers', 'diff_ids': layer_ids}}).encode()
    identity = collector.digest(config)
    manifest = json.dumps([{'Config': 'config.json', 'Layers': ['base/layer.tar', 'new/layer.tar']}]).encode()
    payload = tar([('config.json', config, tarfile.REGTYPE, 0o644), ('manifest.json', manifest, tarfile.REGTYPE, 0o644),
                   ('base/layer.tar', b'fixture-base', tarfile.REGTYPE, 0o644),
                   ('new/layer.tar', gzip.compress(delta) if compressed else delta, tarfile.REGTYPE, 0o644)])
    parent = {'Id': BASE, 'RootFS': {'Layers': layer_ids[:1]}, 'RepoDigests': [DIGEST],
              'Config': {'Entrypoint': ['/socket-proxy']}}
    child = {'Id': identity, 'Parent': BASE, 'RootFS': {'Layers': layer_ids}, 'Size': len(payload),
             'Config': {'Entrypoint': ['/socket-proxy']}}
    return parent, child, payload


class Images:
    def __init__(self, parent, child, payload):
        self.data = {parent['Id']: parent, child['Id']: child}
        self.payload, self.exports = payload, 0
    def image(self, identity):
        if identity not in self.data:
            raise collector.Unknown()
        return self.data[identity]
    def export(self, identity):
        assert identity in self.data
        self.exports += 1
        return self.payload


@pytest.mark.parametrize('compressed', [False, True])
def test_parent_digest_and_only_mount_stub_changes_prove_program_origin(compressed):
    parent, child, payload = images(compressed=compressed)
    proof = collector.prove(Images(parent, child, payload), child['Id'])
    assert proof == {'source_id': BASE, 'source_digest': DIGEST, 'profile': collector.PROFILE, 'fs_verified': True}


@pytest.mark.parametrize('entry', [
    ('socket-proxy', b'changed executable', tarfile.REGTYPE, 0o755),
    ('var/run/.wh.docker.sock', b'', tarfile.REGTYPE, 0o644),
    ('var/run/docker.sock', b'', tarfile.SYMTYPE, 0o644),
    ('var', b'', tarfile.DIRTYPE, 0o777),
    ('etc/config', b'changed configuration', tarfile.REGTYPE, 0o644),
])
def test_changed_files_links_whiteouts_or_permissions_cannot_inherit_purpose(entry):
    parent, child, payload = images(tar([entry]))
    with pytest.raises(collector.Unknown):
        collector.prove(Images(parent, child, payload), child['Id'])


def test_local_tag_is_not_an_anchor_and_parent_chain_must_match_layers():
    parent, child, payload = images()
    parent['RepoDigests'] = []
    parent['RepoTags'] = ['wollomatic/socket-proxy:fixture']
    with pytest.raises(collector.Unknown):
        collector.prove(Images(parent, child, payload), child['Id'])
    parent['RepoDigests'] = [DIGEST]
    child['RootFS']['Layers'][0] = 'sha256:' + 'e' * 64
    with pytest.raises(collector.Unknown):
        collector.prove(Images(parent, child, payload), child['Id'])


def test_export_config_and_payload_hashes_cannot_be_substituted():
    parent, child, payload = images()
    child['Id'] = IMAGE
    with pytest.raises(collector.Unknown):
        collector.prove(Images(parent, child, payload), child['Id'])
    with pytest.raises(collector.Unknown):
        collector.safe_layer(stub(), 'sha256:' + 'e' * 64)


def oci_images(wrong_config=False, wrong_layer=False):
    parent, child, payload = images()
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        config = archive.extractfile('config.json').read()
    base = b'fixture-base'
    delta = gzip.compress(stub())
    config_id, base_id, delta_id = [collector.digest(x).split(':')[1] for x in (config, base, delta)]
    paths = ['blobs/sha256/' + x for x in (config_id, base_id, delta_id)]
    immutable = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                 'config': {'digest': collector.digest(config), 'size': len(config)},
                 'layers': [{'digest': collector.digest(x), 'size': len(x)} for x in (base, delta)]}
    if wrong_config:
        immutable['config']['digest'] = IMAGE
    if wrong_layer:
        immutable['layers'][1]['digest'] = IMAGE
    raw = json.dumps(immutable).encode()
    child['Id'] = collector.digest(raw)
    manifest = json.dumps([{'Config': paths[0], 'Layers': paths[1:]}]).encode()
    payload = tar([(name, data, tarfile.REGTYPE, 0o644) for name, data in [
        (paths[0], config), (paths[1], base), (paths[2], delta),
        ('blobs/sha256/' + child['Id'].split(':')[1], raw), ('manifest.json', manifest)]])
    child['Size'] = len(payload)
    return parent, child, payload


def test_containerd_manifest_id_binds_config_and_compressed_layer_digests():
    parent, child, payload = oci_images()
    assert collector.prove(Images(parent, child, payload), child['Id'])['fs_verified'] is True


@pytest.mark.parametrize('change', ['wrong_config', 'wrong_layer'])
def test_containerd_manifest_cannot_point_at_an_unverified_payload(change):
    parent, child, payload = oci_images(**{change: True})
    with pytest.raises(collector.Unknown):
        collector.prove(Images(parent, child, payload), child['Id'])


def test_cached_filesystem_proof_never_caches_runtime_permissions():
    class Cached(Networks):
        def __init__(self):
            self.container = runtime()
        def get(self, path):
            if path == '/containers/json?all=0':
                return [{'Id': CONTAINER, 'Mounts': self.container['Mounts']}]
            if path == '/containers/' + CONTAINER + '/json':
                return self.container
            return super().get(path)
    api = Cached()
    cached = {IMAGE: {'source_id': BASE, 'source_digest': DIGEST, 'profile': collector.PROFILE, 'fs_verified': True}}
    first = collector.collect(api, cached, 100)
    assert len(first['containers']) == 1
    assert 'Env' not in json.dumps(first) and 'allowGET' not in json.dumps(first)
    api.container['HostConfig']['CapAdd'] = ['SYS_ADMIN']
    assert collector.collect(api, cached, 101)['containers'] == []


def test_compressed_layer_cannot_exceed_the_uncompressed_budget():
    data = b'x' * (collector.MAX_SMALL + 1)
    with pytest.raises(collector.Unknown):
        collector.safe_layer(gzip.compress(data), collector.digest(data))


def test_missing_docker_evidence_is_fail_closed():
    for value in (None, [], 'malformed', {'v': True}, {'v': 1, 'ts': True}):
        r, _, _ = report()
        r['extras'] = {'docker-origin': value}
        assert _names(r) == {'edge-api': ['sock']}
