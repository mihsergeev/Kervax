#!/usr/bin/env python3
"""Require previously reporting agents to report again after backend recreation."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time


QUERY = '''import asyncio,json
from datetime import datetime,timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.config import get_settings
async def main():
    engine=create_async_engine(get_settings().db_url)
    try:
        async with engine.connect() as connection:
            rows=(await connection.execute(text("SELECT id,last_seen FROM servers WHERE enabled AND last_seen IS NOT NULL"))).all()
        output=[]
        for server_id,seen in rows:
            if isinstance(seen,str): seen=datetime.fromisoformat(seen.replace("Z","+00:00"))
            if seen.tzinfo is None: seen=seen.replace(tzinfo=timezone.utc)
            output.append([server_id,seen.timestamp()])
        print(json.dumps(output))
    finally: await engine.dispose()
asyncio.run(main())
'''


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=10).stdout


def rows():
    data = json.loads(docker("exec", "kervax-backend-1", "python", "-c", QUERY))
    result = {}
    for server_id, seen in data:
        if type(server_id) is not int or server_id <= 0 or server_id in result or not isinstance(seen, (int, float)) or not math.isfinite(seen):
            raise ValueError("invalid report counters")
        result[server_id] = seen
    return result


def capture(path):
    now = time.time()
    current = rows()
    fresh = {str(server_id): seen for server_id, seen in current.items() if now - 90 <= seen <= now + 30}
    if not fresh:
        raise ValueError("no recently reporting agents available for an acceptance check")
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            json.dump({"v": 1, "captured_at": now, "agents": fresh}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    print("agent acceptance baseline: %s fresh agents" % len(fresh))


def verify(path, since, timeout=60):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("v") != 1 or not isinstance(data.get("agents"), dict) or not data["agents"]:
        raise ValueError("missing agent acceptance baseline")
    expected = {int(server_id): float(seen) for server_id, seen in data["agents"].items()}
    if any(server_id <= 0 or not math.isfinite(seen) for server_id, seen in expected.items()):
        raise ValueError("invalid agent acceptance baseline")
    if not math.isfinite(since) or since <= 0:
        raise ValueError("invalid post-recreation cutoff")
    runtime = docker("exec", "kervax-backend-1", "python", "-c",
                     "from app.config import get_settings;print(int(getattr(get_settings(),'agent_source_guard',False)))").strip()
    if runtime != "1":
        raise ValueError("new backend does not enforce the agent source guard")
    started = docker("inspect", "--format", "{{.State.StartedAt}}", "kervax-backend-1").strip()
    parsed_start = datetime.fromisoformat(started.replace("Z", "+00:00"))
    if parsed_start.tzinfo is None:
        raise ValueError("backend start must have a timezone")
    backend_start = parsed_start.timestamp()
    deadline = time.monotonic() + timeout
    while True:
        current = rows()
        pending = [server_id for server_id, seen in expected.items()
                   if current.get(server_id, 0) <= max(seen, backend_start, since)]
        if not pending:
            print("agent acceptance passed: %s agents reported after backend start" % len(expected))
            return True
        if time.monotonic() >= deadline:
            print("agent acceptance failed; reports not advanced for IDs: " + ",".join(map(str, sorted(pending))))
            return False
        time.sleep(min(3, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture")
    mode.add_argument("--verify")
    parser.add_argument("--since", type=float)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("must run as root")
    try:
        if args.capture:
            capture(args.capture)
        elif args.since is None or not verify(args.verify, args.since):
            raise SystemExit(1)
    except Exception:
        raise SystemExit("agent acceptance check unavailable; deployment cannot be confirmed")
