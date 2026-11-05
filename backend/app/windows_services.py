"""Read-only Windows service inventory and operator-selected service alerts."""
from datetime import datetime, timezone

KEY = "windows_services"
KIND = "windows_service"
PREFIX = "winsvc:"
MAX_AGE = 180


def names(server) -> set[str]:
    return {str(n).casefold() for n in getattr(server, "windows_service_names", None) or []}


def muted(server, now: datetime) -> set[str]:
    out = {m[len(PREFIX):].casefold() for m in server.alert_mutes or [] if m.startswith(PREFIX)}
    for key, until in (server.alert_snoozes or {}).items():
        if not key.startswith(PREFIX):
            continue
        try:
            at = datetime.fromisoformat(until)
            if (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) > now:
                out.add(key[len(PREFIX):].casefold())
        except (ValueError, TypeError):
            pass
    return out


def inventory(report: dict) -> tuple[dict[str, dict], bool]:
    """Only a complete current enumeration can prove health; no paths or accounts."""
    if report.get("platform") != "windows":
        return {}, False
    block = report.get(KEY)
    if not isinstance(block, dict) or not isinstance(block.get("items"), list):
        return {}, False
    try:
        collected = float(block["collected_at"])
        clock = float(report["clock_unix"])
        uptime = float(report.get("uptime_seconds") or 0)
        if not 0 <= clock - collected <= 120 or collected < clock - uptime - 5:
            return {}, False
    except (KeyError, TypeError, ValueError, OverflowError):
        return {}, False
    complete = block.get("complete") is True and len(block["items"]) <= 4096
    rows = {}
    for item in block["items"][:4096]:
        if not isinstance(item, dict):
            complete = False
            continue
        name, state = item.get("name"), item.get("state")
        if not isinstance(name, str) or not name or len(name) > 256 or any(ord(c) < 32 for c in name):
            complete = False
            continue
        key = name.casefold()
        if key in rows:
            complete = False
            rows[key]["state"] = "Unknown"
            continue
        if state not in ("Running", "Stopped", "Paused", "Start Pending", "Stop Pending", "Pause Pending", "Continue Pending"):
            complete = False
            state = "Unknown"
        rows[key] = {"name": name, "display_name": str(item.get("display_name") or name)[:256],
                     "state": state, "start_mode": str(item.get("start_mode") or "")[:32]}
    return rows, complete


def sample(server, now: datetime) -> tuple[list[dict], bool]:
    rep = server.last_report or {}
    rows, complete = inventory(rep)
    seen = server.last_seen
    if seen is None or not 0 <= (now - (seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc))).total_seconds() <= MAX_AGE:
        return [], False
    selected = names(server) - muted(server, now)
    observed = [rows[n] for n in sorted(selected) if n in rows]
    complete = complete and selected <= rows.keys() and all(r["state"] in ("Running", "Stopped", "Paused") for r in observed)
    return [r for r in observed if r["state"] in ("Stopped", "Paused")], bool(complete)


def conditions(server, now: datetime) -> tuple[int, dict]:
    bad, complete = sample(server, now)
    parts = [(r["name"].casefold(), f'{r["name"]}: служба не работает') for r in bad]
    return (2 if bad else 0), {"detail": "; ".join(t for _, t in parts[:3]),
                              "sig": [k for k, _ in parts], "items": bad,
                              "parts": parts, "_complete": complete}


def view(server, now: datetime) -> dict | None:
    report = server.last_report or {}
    if report.get("platform") != "windows":
        return None
    rows, complete = inventory(report)
    seen = server.last_seen
    fresh = seen is not None and 0 <= (now - (seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc))).total_seconds() <= MAX_AGE
    selected = names(server)
    entries = [{**row, "selected": key in selected} for key, row in sorted(rows.items())]
    entries.extend({"name": key, "display_name": key, "state": "Unknown", "start_mode": "", "selected": True}
                   for key in sorted(selected - rows.keys()))
    return {"complete": complete and fresh, "items": entries}
