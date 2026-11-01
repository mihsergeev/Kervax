"""An optional root collector proves unchanged image origin and a restricted role.

The agent/host is the same trust boundary as Docker privilege reporting. Names,
mutable tags and image hashes alone are not evidence. Only the socket permission
is eligible; every other risk remains independently evaluated.
"""
from __future__ import annotations

import re
import time

SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
CID = re.compile(r'[0-9a-f]{64}\Z')
SHORT_ID = re.compile(r'[0-9a-f]{12,64}\Z')
ANCHOR = re.compile(r'(?:(?:docker\.io/)?wollomatic/socket-proxy|ghcr\.io/wollomatic/socket-proxy)@sha256:[0-9a-f]{64}\Z')
PROFILE = 'wollomatic-socket-proxy-v1'
SOCKETS = {'/run/docker.sock', '/var/run/docker.sock'}
MAX_AGE = 180


def verified_socket(rep: dict, container: dict) -> bool:
    """Fresh root evidence, tied to the current immutable container identity."""
    extras = rep.get('extras')
    if not isinstance(extras, dict):
        return False
    block = extras.get('docker-origin')
    if not isinstance(block, dict) or type(block.get('v')) is not int or block['v'] != 1:
        return False
    timestamp = block.get('ts')
    reference = rep.get('clock_unix') or time.time()
    if type(timestamp) not in (int, float) or type(reference) not in (int, float):
        return False
    try:
        if not 0 < timestamp < 2**63 or not 0 < reference < 2**63:
            return False
        age = reference - timestamp
        if not 0 <= age <= MAX_AGE:
            return False
    except (OverflowError, ValueError):
        return False
    identity = container.get('id')
    if not isinstance(identity, str) or not SHORT_ID.fullmatch(identity):
        return False
    # Contradictory main inventory cannot be overridden by an allowance report.
    if container.get('priv') not in (None, False) or container.get('caps') or container.get('pid_host') or container.get('vols'):
        return False
    binds, ro = container.get('binds'), container.get('ro')
    if not isinstance(binds, list) or len(binds) != 1 or not isinstance(binds[0], str) or binds[0] not in SOCKETS:
        return False
    if not isinstance(ro, list) or binds[0] not in ro:
        return False
    if container.get('ports'):
        return False
    entries = block.get('containers')
    if not isinstance(entries, list) or len(entries) > 8:
        return False
    candidates = [entry for entry in entries if isinstance(entry, dict)
                  and isinstance(entry.get('id'), str) and CID.fullmatch(entry['id']) and entry['id'].startswith(identity)]
    if len(candidates) != 1:
        return False
    entry = candidates[0]
    if (entry.get('profile') != PROFILE or entry.get('fs_verified') is not True
            or entry.get('restricted_runtime') is not True):
        return False
    if not isinstance(entry.get('source_digest'), str) or not ANCHOR.fullmatch(entry['source_digest']):
        return False
    if any(not isinstance(entry.get(k), str) or not SHA.fullmatch(entry[k]) for k in ('source_id', 'image_id')):
        return False
    image = container.get('image')
    if not isinstance(image, str) or entry.get('config_image') != image:
        return False
    if image.startswith('sha256:') and image != entry['image_id']:
        return False
    return True
