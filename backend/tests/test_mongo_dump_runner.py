"""Atomic publication, credential path checks and inspect's side-effect boundary."""
import gzip
import json
import os
import re
import subprocess
import sys
import pytest
from tests.test_backup_dump_paths_security import installed, setup_dump

pytestmark = pytest.mark.skipif(sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid()!=0,
                               reason="isolated full installer needs Linux root ownership")


def prepare(installed):
    scratch, helper = installed
    result = setup_dump(helper, scratch/"backup", engine="mongo")
    assert result.returncode == 0, result.stdout+result.stderr
    docker = scratch/"bin/docker"
    docker.write_text("#!/usr/bin/env python3\n"
        "import gzip,os,re,sys,time\n"
        "js=sys.argv[-1]; secret=sys.stdin.read()\n"
        "assert secret not in js or not secret\n"
        "operation=re.search(\"const operation='(probe|dump)'\",js).group(1)\n"
        "if operation=='probe': print('standalone');sys.exit(0)\n"
        "if os.environ.get('MONGO_FAIL'): sys.stderr.write('MongoDB: dump failed\\n');sys.exit(2)\n"
        "active=os.environ.get('MONGO_ACTIVE')\n"
        "if active:\n"
        "    fd=os.open(active,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.close(fd);time.sleep(1.1);os.unlink(active)\n"
        "sys.stdout.buffer.write(gzip.compress(b'archive-fixture'))\n",encoding="utf-8")
    docker.chmod(0o755)
    env={**os.environ,"PATH":str(scratch/"bin")+os.pathsep+os.environ["PATH"]}
    return scratch,helper,scratch/"lib65/dumps.d/mongo@db-a.sh",env


def test_mongo_failed_run_keeps_previous_archive(installed):
    scratch,helper,runner,env=prepare(installed)
    destination=scratch/"backup/mongo/db-a"
    old=destination/"mongodump-standalone-20000101-000000.archive.gz"
    old.write_bytes(gzip.compress(b"previous",mtime=0))
    result=subprocess.run(["bash",str(runner)],env={**env,"MONGO_FAIL":"1"},capture_output=True,text=True)
    assert result.returncode != 0
    assert old.read_bytes()==gzip.compress(b"previous",mtime=0)
    assert list(destination.iterdir())==[old]


def test_mongo_success_publishes_private_complete_archive_and_rotates(installed):
    scratch,helper,runner,env=prepare(installed)
    destination=scratch/"backup/mongo/db-a"
    for i in range(3):
        (destination/f"old-{i}.archive.gz").write_bytes(gzip.compress(b"previous",mtime=0))
    result=subprocess.run(["bash",str(runner)],env=env,capture_output=True,text=True)
    assert result.returncode == 0,result.stdout+result.stderr
    files=list(destination.iterdir())
    assert len(files)==2 and all(not f.name.startswith('.') for f in files)
    archive=next(destination.glob('mongodump-standalone-*.archive.gz'))
    assert gzip.decompress(archive.read_bytes())==b"archive-fixture"
    assert archive.stat().st_mode & 0o777==0o600


@pytest.mark.parametrize('unsafe',['symlink','mode','parent'])
def test_mongo_credentials_reject_untrusted_paths(installed,unsafe):
    scratch,helper,runner,env=prepare(installed)
    config=scratch/'etc/kervax/mongo-backup.d/db-a.json'
    config.parent.mkdir(parents=True)
    config.write_text('{"password":"fixture-secret"}');config.chmod(0o600)
    if unsafe=='symlink':
        real=config.with_suffix('.real');config.rename(real);config.symlink_to(real)
    elif unsafe=='mode':config.chmod(0o644)
    else:config.parent.chmod(0o777)
    result=subprocess.run(['bash',str(runner)],env=env,capture_output=True,text=True)
    assert result.returncode !=0 and 'root-owned regular 0600' in result.stderr
    assert 'fixture-secret' not in result.stdout+result.stderr
    assert not list((scratch/'backup/mongo/db-a').iterdir())


def test_partial_archive_is_never_reported_as_coverage(installed):
    scratch,helper,runner,env=prepare(installed)
    partial=scratch/'backup/mongo/db-a/.partial';partial.mkdir()
    (partial/'unfinished.archive.gz').write_bytes(gzip.compress(b"unfinished"))
    result=subprocess.run(['bash',str(helper),'get-config'],capture_output=True,text=True,env=env)
    assert result.returncode==0,result.stderr
    dump=json.loads(result.stdout)['dumps'][0]
    assert dump['files']==0 and dump['last_ts']==0


def test_inspect_calls_fresh_collectors_only_and_propagates_failure(installed):
    scratch,helper=installed
    text=helper.read_text()
    marker='case "${1:-}" in\n'
    overrides=r"""
cmd_get_config() { printf '{"mode":"include","dumps":[]}'; }
unit_report() { printf '{"runner":"unit"}'; }
cmd_custom_scan() { [ ! -e "${CONF_JSON%/*}/deny" ] || return 9; printf '{"items":[{"fresh":%s}]}' "$(date +%s%N)"; }
refresh_config() { echo forbidden >&2; return 99; }
custom_scan_maybe() { echo forbidden >&2; return 99; }
"""
    helper.write_text(text.replace(marker,overrides+marker))
    def inspect():return subprocess.run(['bash',str(helper),'inspect'],capture_output=True,text=True)
    one=inspect();two=inspect()
    assert one.returncode==two.returncode==0
    first,second=json.loads(one.stdout),json.loads(two.stdout)
    assert first['complete'] and first['backup_config']['dumps']==[]
    assert first['custom_backups']!=second['custom_backups']
    assert not one.stderr+two.stderr
    (scratch/'state/deny').touch()
    failed=inspect()
    assert failed.returncode!=0 and not failed.stdout


def test_mongo_publication_failure_keeps_previous_and_marks_error(installed):
    scratch,helper,runner,env=prepare(installed)
    destination=scratch/'backup/mongo/db-a'
    old=destination/'old.archive.gz';old.write_bytes(gzip.compress(b'previous',mtime=0))
    move=scratch/'bin/mv';move.write_text('#!/bin/sh\nexit 9\n');move.chmod(0o755)
    failed=subprocess.run(['bash',str(runner)],env=env,capture_output=True,text=True)
    assert failed.returncode!=0 and 'publication failed' in failed.stderr
    assert list(destination.iterdir())==[old]
    assert old.read_bytes()==gzip.compress(b'previous',mtime=0)
    marker=scratch/'lib65/dumps.d/mongo@db-a.error'
    assert marker.read_text().strip().isdigit()
    report=subprocess.run(['bash',str(helper),'get-config'],capture_output=True,text=True)
    assert json.loads(report.stdout)['dumps'][0]['error_ts']>0
    move.unlink()
    success=subprocess.run(['bash',str(runner)],env=env,capture_output=True,text=True)
    assert success.returncode==0,success.stdout+success.stderr
    assert not marker.exists()


def test_mongo_concurrent_starts_are_serialized(installed):
    scratch,helper,runner,env=prepare(installed)
    env={**env,'MONGO_ACTIVE':str(scratch/'dump-active')}
    one=subprocess.Popen(['bash',str(runner)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    two=subprocess.Popen(['bash',str(runner)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    first=one.communicate(timeout=30);second=two.communicate(timeout=30)
    assert one.returncode==0,first
    assert two.returncode==0,second
    assert len(list((scratch/'backup/mongo/db-a').glob('*.archive.gz')))==2
