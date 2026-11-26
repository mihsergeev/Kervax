"""Контрольные точки чтения журналов cron и OOM, на синтетическом journalctl."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


AGENT = Path(__file__).resolve().parents[2] / "agent"
NOW = 1_790_000_000
LAST = NOW - 60
WEEK_START = NOW - 7 * 86400
JOB = "/bin/work"
CRON_ROW = f"{NOW - 5}.000000 node CRON[7]: (root) CMD ({JOB})\n"
LEGACY_HEADER = "-- Logs begin at Mon 2026-09-21 00:00:00 UTC, end at Tue 2026-09-22 00:00:00 UTC. --\n"


def _helper(name):
    source = (AGENT / f"{name}-setup.sh").read_text(encoding="utf-8")
    return source.split("<<'HELPER_EOF'\n", 1)[1].split("\nHELPER_EOF\n", 1)[0]


@pytest.fixture
def journal(tmp_path):
    bash = shutil.which("bash")
    if not bash or not shutil.which("awk"):
        pytest.skip("нужны bash и awk")

    def response(mode, code, stdout="", stderr=""):
        (tmp_path / f"{mode}.rc").write_text(str(code), encoding="utf-8")
        (tmp_path / f"{mode}.out").write_text(stdout, encoding="utf-8", newline="\n")
        (tmp_path / f"{mode}.err").write_text(stderr, encoding="utf-8", newline="\n")

    def run(body):
        script = tmp_path / "run.sh"
        # Git Bash can't set Unix permissions on a Windows test directory.
        if os.name == "nt":
            body = 'install() { mkdir -p "${@: -1}"; }\n' + body
        script.write_text(f'''\
set -u
export LC_ALL=C
JOURNAL_TEST_DIR=$PWD
date() {{ printf '%s\\n' {NOW}; }}
timeout() {{ shift; "$@"; }}
journalctl() {{
  printf '%s\\n' "$*" >> "$JOURNAL_TEST_DIR/calls"
  local mode=tail
  case " $* " in
    *' --after-cursor='*) mode=cursor ;;
    *' --since '*) mode=since ;;
  esac
  cat "$JOURNAL_TEST_DIR/$mode.out"
  cat "$JOURNAL_TEST_DIR/$mode.err" >&2
  return "$(cat "$JOURNAL_TEST_DIR/$mode.rc")"
}}
: > "$JOURNAL_TEST_DIR/calls"
{body}
''', encoding="utf-8", newline="\n")
        result = subprocess.run([bash, script.name], cwd=tmp_path, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        return (tmp_path / "calls").read_text(encoding="utf-8").splitlines()

    return response, run


@pytest.fixture
def cron(tmp_path, journal):
    cache = tmp_path / "cron.cache"
    cache.write_text(f"C\t{LAST}\tbefore\nL\thost|{JOB}\t{LAST}\t{LAST // 3600}:2\nX\t\n",
                     encoding="utf-8", newline="\n")
    (tmp_path / "rows").write_text(f"M\t42\tcron\thost\t1\nT\t42\t/etc/crontab\troot\t* * * * *\tset\t{JOB}\n",
                                  encoding="utf-8", newline="\n")
    (tmp_path / "tree").touch()
    response, run = journal

    def collect():
        # Обнаружение процессов заменено готовыми строками, запись отчета и кэша настоящие.
        calls = run(f'''
TMPD=$PWD
CACHE=$PWD/cron.cache
OUT=$PWD/cron.json
NOW={NOW}
FIRST_S=86400
MAX_RUN=5
MAX_JOBS=300
HOSTCRON=1
''' + _helper("cron").split("# -- the journal of the host cron:", 1)[1].split("\n", 1)[1])
        return calls, json.loads((tmp_path / "cron.json").read_text(encoding="utf-8"))

    return cache, response, collect


@pytest.mark.parametrize("status", [1, 124, 137])
def test_cron_failed_read_does_not_process_partial_stdout(cron, status):
    cache, response, collect = cron
    before = cache.read_bytes()
    response("cursor", status, CRON_ROW + "-- cursor: partial\n")
    response("since", status, CRON_ROW + "-- cursor: partial\n")

    calls, report = collect()

    assert len(calls) == 1 and "--after-cursor=before" in calls[0]
    if status == 1:
        assert cache.read_bytes() == before
    else:
        lines = cache.read_bytes().splitlines(keepends=True)
        assert lines.pop(1) == b"B\t1000\n"
        assert b"".join(lines) == before
    assert report["journal"] is False and report["jobs"][0]["runs"] == -1


@pytest.mark.parametrize(("status", "stdout"), [
    (0, "-- No entries --\n"),
    (1, "-- No entries --\n"),
    (1, LEGACY_HEADER + "-- No entries --\n"),
    (1, LEGACY_HEADER),
])
def test_cron_empty_selection_keeps_cursor_without_fallback(cron, status, stdout):
    cache, response, collect = cron
    response("cursor", status, stdout)

    calls, report = collect()

    assert len(calls) == 1
    assert cache.read_text(encoding="utf-8").splitlines()[0] == f"C\t{NOW}\tbefore"
    assert report["journal"] is True and report["jobs"][0]["runs"] == 2


def test_cron_retries_timed_out_cursor_in_batches_and_counts_only_committed_reads(cron):
    cache, response, collect = cron
    response("cursor", 124, CRON_ROW + "-- cursor: discarded\n")
    collect()
    calls, report = collect()
    assert len(calls) == 1 and "--after-cursor=before --lines=1000" in calls[0]
    assert cache.read_text(encoding="utf-8").splitlines()[:2] == [f"C\t{LAST}\tbefore", "B\t500"]
    assert report["journal"] is False

    # Use a small cap to exercise a complete batch without making a large test journal.
    cache.write_text(cache.read_text(encoding="utf-8").replace("B\t500", "B\t2"), encoding="utf-8", newline="\n")
    response("cursor", 0, CRON_ROW * 2 + "-- cursor: before\n")
    calls, report = collect()
    assert "--after-cursor=before --lines=2" in calls[0] and report["journal"] is False
    assert cache.read_text(encoding="utf-8").splitlines()[0] == f"C\t{LAST}\tbefore"

    response("cursor", 0, CRON_ROW * 2 + "-- cursor: batch\n")
    calls, report = collect()
    assert "--after-cursor=before --lines=2" in calls[0] and report["journal"] is False
    assert cache.read_text(encoding="utf-8").splitlines()[:2] == [f"C\t{NOW - 5}\tbatch", "B\t4"]

    response("cursor", 0, CRON_ROW + "-- cursor: after\n")
    calls, report = collect()
    assert "--after-cursor=batch --lines=4" in calls[0]
    assert cache.read_text(encoding="utf-8").splitlines()[0] == f"C\t{NOW}\tafter"
    assert report["journal"] is True and report["jobs"][0]["runs"] == 5

    response("cursor", 1, "-- No entries --\n")
    calls, report = collect()
    assert len(calls) == 1 and "--after-cursor=after" in calls[0] and "--lines=" not in calls[0]
    assert report["journal"] is True and report["jobs"][0]["runs"] == 5


def test_cron_retries_rejected_cursor_from_last_success(cron):
    cache, response, collect = cron
    before = cache.read_bytes()
    response("cursor", 1, stderr="Failed to seek to cursor: Invalid argument\n")
    response("since", 1, CRON_ROW + "-- cursor: partial\n", "Failed to iterate through journal: Input/output error\n")

    calls, report = collect()

    assert len(calls) == 2 and f"--since @{LAST}" in calls[1]
    assert cache.read_bytes() == before
    assert report["journal"] is False

    response("since", 0, CRON_ROW + "-- cursor: after\n")
    calls, report = collect()
    assert f"--since @{LAST}" in calls[1]
    assert cache.read_text(encoding="utf-8").splitlines()[0] == f"C\t{NOW}\tafter"
    assert report["journal"] is True and report["jobs"][0]["runs"] == 3


@pytest.fixture
def oom(tmp_path, journal):
    response, run = journal
    state = tmp_path / "oom"
    state.mkdir()
    report_path = tmp_path / "oom.json"
    (tmp_path / "passwd").write_text("test:x:1000:1000::/home/test:/bin/sh\n", encoding="utf-8", newline="\n")

    def collect():
        helper = _helper("units").split('\ncase "${1:-scan}"', 1)[0]
        return run(helper.replace("/etc/passwd", '"$PWD/passwd"') + '''
OOM_DIR=$PWD/oom
OOM_OUT=$PWD/oom.json
oom_scan
''')

    return state, report_path, response, collect


def _unit_line(when, unit):
    return f"{when}.000000 node systemd[1]: {unit}.service: Failed with result 'oom-kill'.\n"


def test_oom_backfill_timeout_does_not_advance_to_tail(oom):
    state, path, response, collect = oom
    row = _unit_line(WEEK_START + 1000, "worker")
    response("since", 124, row + "-- cursor: partial\n")
    response("tail", 0, row + "-- cursor: tail\n")

    calls = collect()

    assert len(calls) == 1 and f"--since @{WEEK_START} --until @{WEEK_START + 86400}" in calls[0]
    assert {p.name for p in state.iterdir()} == {"backfill_span"}
    assert (state / "backfill_span").read_text(encoding="utf-8").strip() == "43200"
    assert not path.exists()

    response("since", 0, row)
    calls = collect()
    assert f"--since @{WEEK_START} --until @{WEEK_START + 43200}" in calls[0]
    assert (state / "backfill_until").read_text(encoding="utf-8").strip() == str(WEEK_START + 43200)
    assert "worker.service" in (state / "kills.tsv").read_text(encoding="utf-8")
    assert not (state / "cursor").exists() and not path.exists()

    response("since", 0, "-- No entries --\n")
    calls = collect()
    assert f"--since @{WEEK_START + 43200 - 30}" in calls[0]
    assert (state / "backfill_until").read_text(encoding="utf-8").strip() == str(WEEK_START + 129600)
    assert not path.exists()


@pytest.mark.parametrize("status", [1, 124, 137])
def test_oom_failed_cursor_read_preserves_history_and_report(oom, status):
    state, path, response, collect = oom
    known = _unit_line(NOW - 50, "known")
    (state / "backfill_until").write_text(str(LAST), encoding="utf-8")
    response("since", 0, known + "-- cursor: before\n")
    collect()
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    report_before = path.read_bytes()
    response("cursor", status, _unit_line(NOW - 5, "new") + "-- cursor: partial\n")

    calls = collect()

    assert len(calls) == 1 and "--after-cursor=before" in calls[0]
    after = {p.name: p.read_bytes() for p in state.iterdir()}
    if status in (124, 137):
        assert after.pop("backfill_span") == b"43200\n"
    assert after == before
    assert path.read_bytes() == report_before

    if status in (124, 137):
        response("since", 0, _unit_line(WEEK_START + 3, "retained"))
        calls = collect()
        assert len(calls) == 1 and f"--since @{WEEK_START} --until @{WEEK_START + 43200}" in calls[0]
        assert (state / "backfill_until").read_text(encoding="utf-8").strip() == str(WEEK_START + 43200)
        assert (state / "cursor").read_bytes() == before["cursor"]
        assert "retained.service" in (state / "kills.tsv").read_text(encoding="utf-8")
        assert "new.service" not in (state / "kills.tsv").read_text(encoding="utf-8")
        assert path.read_bytes() == report_before


def test_oom_replays_rejected_cursor_without_duplicate_events(oom):
    state, path, response, collect = oom
    (state / "backfill_until").write_text(str(LAST), encoding="utf-8")
    known = _unit_line(NOW - 50, "known")
    killed = (f"{NOW - 51}.000001 node kernel: Memory cgroup out of memory: Killed process 42 (worker) "
              "total-vm:100kB, anon-rss:50kB, UID:1000\n")
    detail = (f"{NOW - 51}.000000 node kernel: oom-kill:constraint=CONSTRAINT_MEMCG,task_memcg=/test.slice,pid=42\n")
    response("since", 0, killed + known + "-- cursor: before\n")
    collect()
    before = path.read_bytes()

    response("cursor", 1, stderr="Failed to seek to cursor: Invalid argument\n")
    older = _unit_line(WEEK_START + 86400, "older")
    response("since", 0, older)
    calls = collect()
    assert len(calls) == 2 and f"--since @{WEEK_START} --until @{WEEK_START + 86400}" in calls[1]
    assert "--show-cursor" not in calls[1] and path.read_bytes() == before
    assert (state / "cursor").read_text(encoding="utf-8").strip() == "before"

    response("since", 0, older)
    calls = collect()
    assert len(calls) == 1 and f"--since @{WEEK_START + 86400 - 30}" in calls[0]
    response("since", 0, "-- No entries --\n")
    for _ in range(4):
        assert len(collect()) == 1
    assert path.read_bytes() == before

    response("since", 0, detail + killed + known + _unit_line(NOW - 5, "new") + "-- cursor: after\n")
    calls = collect()
    assert len(calls) == 1 and f"--since @{NOW - 86400 - 30}" in calls[0]
    assert "--until" not in calls[0] and "--show-cursor" in calls[0]
    assert not (state / "backfill_until").exists()
    assert (state / "cursor").read_text(encoding="utf-8").strip() == "after"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert [event["unit"] for event in report["units"]] == ["older.service", "known.service", "new.service"]
    assert [event["pid"] for event in report["kills"]] == [42]

    response("cursor", 1, "-- No entries --\n")
    calls = collect()
    assert len(calls) == 1 and "--after-cursor=after" in calls[0]
    assert (state / "cursor").read_text(encoding="utf-8").strip() == "after"


def test_oom_empty_recent_journal_resumes_at_watermark(oom):
    state, path, response, collect = oom
    (state / "backfill_until").write_text(str(LAST), encoding="utf-8")
    response("since", 1, LEGACY_HEADER + "-- No entries --\n")

    calls = collect()

    assert len(calls) == 1 and f"--since @{LAST - 30}" in calls[0]
    assert (state / "backfill_until").read_text(encoding="utf-8").strip() == str(NOW)
    assert not (state / "cursor").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["kills"] == []

    calls = collect()
    assert len(calls) == 1 and f"--since @{NOW - 30}" in calls[0]
