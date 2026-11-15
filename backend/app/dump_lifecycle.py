"""Suspend freshness checks only for an explicitly observed graceful Docker stop."""


def dump_paused(rep: dict, dump: dict) -> bool:
    target = dump.get("container")
    if not isinstance(target, str) or not target or target.startswith("k8s."):
        return False
    docker = rep.get("docker") or {}
    if docker.get("present") is not True:
        return False
    matches = [c for c in docker.get("containers") or [] if c.get("name") == target]
    if len(matches) != 1:
        return False
    c = matches[0]
    # Missing legacy exit codes, SIGKILL/137 and crash states are not proof of a
    # deliberate stop. The restart policy must also preserve that stop.
    return (c.get("state") == "exited" and type(c.get("exit")) is int
            and c["exit"] in (0, 143) and not c.get("oom") and not c.get("err")
            and c.get("policy") == "unless-stopped")


def paused_dump_targets(rep: dict) -> list[str]:
    return sorted({f"{d.get('engine')}@{d.get('container')}"
                   for d in (rep.get("backup") or {}).get("dumps") or []
                   if dump_paused(rep, d)})
