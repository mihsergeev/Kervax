#!/usr/bin/env python3
"""Read only the effective guard flag; never print the Compose configuration."""
import argparse
import json
import subprocess


def enabled(directory, files=()):
    command = ["docker", "compose"]
    for filename in files:
        command += ["-f", filename]
    result = subprocess.run(command + ["config", "--format", "json"], cwd=directory,
                            check=True, capture_output=True, text=True, timeout=20)
    environment = json.loads(result.stdout)["services"]["backend"].get("environment", {})
    value = environment.get("KERVAX_AGENT_SOURCE_GUARD", "0")
    if value is None:
        return False
    value = str(value).strip().casefold()
    if value in ("1", "on", "t", "true", "y", "yes"):
        return True
    if value in ("0", "off", "f", "false", "n", "no"):
        return False
    raise ValueError("invalid effective agent guard boolean")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True)
    parser.add_argument("-f", action="append", default=[])
    args = parser.parse_args()
    try:
        print(int(enabled(args.dir, args.f)))
    except Exception:
        raise SystemExit("cannot resolve the effective agent guard setting")
