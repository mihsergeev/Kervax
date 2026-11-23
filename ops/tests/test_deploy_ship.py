"""What ops/deploy.sh sends to a panel: the archive, its manifest and the list of files to delete.

A throwaway git repository with synthetic files and a fake ssh that answers like a panel and
unpacks the upload into a directory - no network, no panel, no credentials. The run on the
panel itself (deploy-remote.sh) is exercised by backend/tests/test_deploy_remote.py."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None or shutil.which("git") is None,
                                reason="POSIX sh and git")

# Answers by the command deploy.sh sends. The upload is unpacked into $FAKE_SSH_STATE/rd;
# FAKE_SSH_CUT cuts the compressed stream to that many bytes, as a dropped link would.
SSH = r"""#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -*) shift ;; *) break ;; esac; done
shift
cmd="$*"
S="$FAKE_SSH_STATE"
case "$cmd" in
  *"docker inspect kervax-frontend-1"*)
    printf '%s\n' "/app/kervax/compose.yml" '{"status":"ok","version":"1.8.80","db":"ok"}' "0153" "1|2|3" ;;
  *".kervax-deployed"*) cat "$S/old-manifest" ;;
  *"find frontend/src backend/app backend/alembic"*) printf '%s\n' backend/app/orphan.py backend/app/main.py ;;
  *"tar xzf -"*)
    cat > "$S/upload.gz"
    if [ -n "${FAKE_SSH_CUT:-}" ]; then head -c "$FAKE_SSH_CUT" "$S/upload.gz" > "$S/cut.gz"; mv "$S/cut.gz" "$S/upload.gz"; fi
    mkdir -p "$S/rd" && tar xzf - -C "$S/rd" < "$S/upload.gz" || exit 1
    echo "fake: run started" ;;
  *"run.sh' follow"*) echo "fake: run ok" ;;
  *"KERVAX_DB_PASSWORD"*) printf '%s\n' '{"status":"ok","version":"1.8.81","db":"ok"}' "0153" "1|2|3" "" ;;
  *) echo "fake ssh: unexpected command: $cmd" >&2; exit 99 ;;
esac
"""

FILES = {
    "backend/app/config.py": '    version: str = "1.8.81"\n',
    "backend/app/main.py": "app = None\n",
    "docs/install.md": "install\n",
    "docs/img/home.png": "synthetic screenshot\n",
    "landing/site/index.html": "<p>landing</p>\n",
    "тест.txt": "non-ASCII name\n",
    "dir with space/file name.txt": "spaces\n",
}


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    for name, body in FILES.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(body, encoding="utf-8")
    (repo / "ops").mkdir()
    for name in ("deploy.sh", "deploy-remote.sh"):
        shutil.copyfile(ROOT / "ops" / name, repo / "ops" / name)
    env = {**os.environ, "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "fixture"]):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True)
    state = tmp_path / "state"
    state.mkdir()
    # the previous delivery had the screenshots, the landing and a file gone since
    (state / "old-manifest").write_text("docs/img/home.png\nlanding/site/index.html\ngone.txt\nтест.txt\n",
                                        encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(SSH)
    (bin_dir / "ssh").chmod(0o755)
    env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", FAKE_SSH_STATE=str(state))
    return repo, state, env


def deploy(repo, env, cwd=None, **extra):
    return subprocess.run(["sh", "ops/deploy.sh" if cwd is None else "deploy.sh", "panel-a"],
                          cwd=cwd or repo, env={**env, **extra}, text=True, capture_output=True, timeout=60)


@pytest.mark.parametrize("subdir", [False, True])
def test_ships_the_whole_tree_without_screenshots_and_landing(repo, subdir):
    repo, state, env = repo
    result = deploy(repo, env, cwd=repo / "ops" if subdir else None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deployed and verified" in result.stdout
    rd = state / "rd"
    with tarfile.open(rd / "src.tar") as archive:
        shipped = sorted(m.name for m in archive.getmembers() if m.isfile())
    manifest = (rd / "manifest.txt").read_text(encoding="utf-8").splitlines()
    expected = sorted(n for n in [*FILES, "ops/deploy.sh", "ops/deploy-remote.sh"]
                      if not n.startswith(("docs/img/", "landing/")))
    # from ops/ as from the root: the whole repository, never just the current directory
    assert shipped == expected
    assert sorted(manifest) == expected
    stale = (rd / "stale.txt").read_text(encoding="utf-8").splitlines()
    assert sorted(stale) == ["backend/app/orphan.py", "docs/img/home.png", "gone.txt", "landing/site/index.html"]
    assert "2 files in docs/img/ and landing/: no longer shipped to panels" in result.stdout
    params = (rd / "params").read_text()
    assert f"SRC_SHA256='{hashlib.sha256((rd / 'src.tar').read_bytes()).hexdigest()}'" in params
    assert (rd / "run.sh").read_bytes() == (ROOT / "ops/deploy-remote.sh").read_bytes()


def test_cut_upload_does_not_start_the_run(repo):
    repo, state, env = repo
    result = deploy(repo, env, FAKE_SSH_CUT="4096")
    assert result.returncode != 0
    assert "production was not touched" in result.stderr
    assert "fake: run started" not in result.stdout
