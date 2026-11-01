"""Execute the installed dispatcher's boundary functions with an isolated spool."""
import ast
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux root-helper file semantics")
SCRIPT = Path(__file__).resolve().parents[2] / "agent" / "recheck-setup.sh"
ID = "0" * 32


@pytest.fixture
def helper(tmp_path):
    source = SCRIPT.read_text(encoding="utf-8").split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    tree = ast.parse(source)
    # The installed service uses the real account ids. Tests use their current
    # unprivileged account and an isolated directory, exercising the same functions.
    tree.body = [x for x in tree.body if not (isinstance(x, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id in {"UID", "GID"} for t in x.targets))]
    ns = {"__name__": "recheck_test", "UID": os.getuid(), "GID": os.getgid()}
    exec(compile(tree, str(SCRIPT), "exec"), ns)
    ns["ROOT"] = tmp_path
    ns["RESULTS"] = tmp_path / "results"
    ns["RESULTS"].mkdir()
    return ns


def payload(**kw):
    return json.dumps({"id": ID, "source": "changes", "db_stats": False, **kw})


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "oversize", "wrong_nonce", "command", "fifo"])
def test_untrusted_spool_is_not_a_root_command(helper, tmp_path, kind):
    p = tmp_path / "request"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text(payload())
        p.symlink_to(target)
    elif kind == "hardlink":
        p.write_text(payload())
        os.link(p, tmp_path / "link")
    elif kind == "fifo":
        os.mkfifo(p)
    elif kind == "oversize":
        p.write_text("x" * 2049)
    else:
        p.write_text(payload(id="1" * 32) if kind == "wrong_nonce" else payload(source="run-now"))
    with pytest.raises((ValueError, OSError)):
        helper["request"](p, ID)


def test_collector_cannot_reuse_old_file(helper, tmp_path):
    p = tmp_path / "block.json"
    p.write_text('{"items": []}')
    helper["run"] = lambda argv: b""  # collector exits successfully without writing
    with pytest.raises(ValueError, match="old block"):
        helper["collect_file"]("/fixed/collector", p)


def test_dispatch_is_a_fixed_allowlist(helper):
    calls = []
    helper["run"] = lambda argv: calls.append(argv)
    with pytest.raises(ValueError):
        helper["collect"]({"id": ID, "source": "run-now", "db_stats": False})
    assert calls == []


def test_receipt_preserves_nonce_and_is_atomic(helper, monkeypatch):
    ownership = []
    monkeypatch.setattr(helper['os'], 'fchown', lambda fd, uid, gid: ownership.append((uid, gid)))
    helper["receipt"](ID, True, {"kube_expiry": [], "flux": []})
    data = json.loads((helper["RESULTS"] / (ID + ".json")).read_text())
    assert data == {"id": ID, "ok": True, "snapshot": {"kube_expiry": [], "flux": []}}
    assert not list(helper["RESULTS"].glob("*.tmp"))
    assert ownership == [(0, os.getgid())]


def test_complete_request_does_not_unlink_a_concurrent_pending_writer(helper, tmp_path):
    import builtins
    requests = tmp_path / 'requests'
    requests.mkdir()
    helper['REQUESTS'] = requests
    helper['open'] = lambda name, *a, **kw: builtins.open(tmp_path / 'lock' if name == '/run/kervax-recheck.lock' else name, *a, **kw)
    helper['collect'] = lambda req: {}
    done = []
    helper['receipt'] = lambda request_id, ok, snapshot: done.append((request_id, ok))
    (requests / (ID + '.json')).write_text(payload())
    pending = requests / '.pending-another'
    second_id = '1' * 32
    with pending.open('w') as writer:
        writer.write('{"id":')
        writer.flush()
        helper['main']()
        assert pending.exists()
        writer.write(json.dumps(second_id) + ',"source":"changes","db_stats":false}')
    pending.rename(requests / (second_id + '.json'))
    helper['main']()
    assert done == [(ID, True), (second_id, True)]


def test_backup_inspect_updates_only_its_read_only_report_caches(helper, tmp_path):
    (tmp_path / 'report.d').mkdir()
    config = {'manageable': True, 'helper_version': 50, 'repo_dest': 'rest:https://backup.example/demo'}
    doc = {'v': 1, 'complete': True, 'ts': 1, 'backup_config': config, 'custom_backups': {'items': []}}
    calls = []
    def run(argv):
        calls.append(argv)
        return json.dumps(doc).encode()
    helper['run'] = run
    got = helper['collect']({'source': 'backup', 'db_stats': False})
    assert calls == [['/lib65/kervax/kervax-backup-helper', 'inspect']]
    assert got['inspect'] == doc
    assert json.loads((tmp_path / 'backup-config.json').read_text()) == config
    assert json.loads((tmp_path / 'report.d/custom-backups.json').read_text()) == {'items': []}
