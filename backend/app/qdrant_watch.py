"""Opt-in Qdrant monitoring using the agent's bounded, read-only metadata."""
from __future__ import annotations

from app import tg_fmt


def observe(rep: dict, previous: list | None = None) -> tuple[list[tuple[int, str, str]], bool]:
    items = rep.get("services")
    if not isinstance(items, list):
        return [], False
    problems, healthy, seen = [], set(), set()
    complete = True
    for service in items:
        if not isinstance(service, dict) or service.get("kind") != "qdrant":
            continue
        source = str(service.get("source") or "")[:256]
        key = source or "Qdrant"
        if key in seen:
            complete = False
            continue
        seen.add(key)
        q = service.get("qdrant")
        if not isinstance(q, dict):
            complete = False
            continue
        state = q.get("state")
        if state == "unavailable" and q.get("reason") in ("api", "timeout"):
            problems.append((2, "Qdrant недоступен" + (f": {source}" if source else ""), key))
            continue
        if state not in ("ok", "partial"):
            complete = False  # authorization, address and budget failures are unknown
            continue
        collections = q.get("collections")
        if not isinstance(collections, list):
            complete = False
            continue
        total = q.get("collections_total")
        valid = (state == "ok" and not q.get("truncated")
                 and type(total) is int and total == len(collections))
        for collection in collections:
            if not isinstance(collection, dict):
                valid = False
                continue
            name = str(collection.get("name") or "")[:256]
            if collection.get("status") == "red" or collection.get("optimizer_status") == "error":
                problems.append((2, f"Qdrant: ошибка коллекции {name}" + (f" ({source})" if source else ""), key))
                valid = False
            elif (collection.get("status") not in ("green", "yellow", "grey")
                  or collection.get("optimizer_status") != "ok"):
                valid = False
        # Yellow/grey are optimization states, not failures. Missing metadata,
        # a truncated list or an omitted former instance cannot confirm recovery.
        if valid:
            healthy.add(key)
        else:
            complete = False
    required = set(previous or [])
    return problems, bool(seen) and complete and required <= healthy


def tg_short(items: list) -> tuple[str, list[str]] | None:
    if not items:
        return None
    details = [str(x[1]) for x in items]
    outage = any(x.startswith("Qdrant недоступен") for x in details)
    return ("Qdrant недоступен" if outage else "Qdrant: ошибка коллекции",
            [tg_fmt.names(details[:3])])
