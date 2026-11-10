"""Credential-bearing checks require a server assigned by a panel user."""

from urllib.parse import urlsplit

from app.deps import user_sections


def token_site_groups(user) -> list[str] | None:
    if "sites" not in user_sections(user):
        return []
    return list(user.site_groups) if user.site_groups else None


def site_group_allowed(groups: list[str] | None, check) -> bool:
    return groups is None or (check.group_name or "") in groups


def requires_trusted_server(check) -> bool:
    if getattr(check, "http_headers", "") or (getattr(check, "auth_method", "") == "basic" and (
            getattr(check, "auth_user", "") or getattr(check, "auth_pass", ""))):
        return True
    target = (check.target or "").strip()
    try:
        url = urlsplit(target if "://" in target else "//" + target)
    except ValueError:
        return True
    paths = getattr(check, "extra_paths", None) or []
    return bool("@" in url.netloc or url.query or any(isinstance(p, str) and "?" in p for p in paths))


def target_host(target: str) -> str:
    target = (target or "").strip()
    try:
        return (urlsplit(target if "://" in target else "//" + target).hostname or "").rstrip(".")
    except ValueError:
        return ""


def trusted_server(check) -> int | None:
    # Only the user's explicit dependency, never a server picked from agent inventory.
    return check.parent_server_id if check.parent_mode == "server" else None


def server_allowed(check, server) -> bool:
    return not requires_trusted_server(check) or (
        trusted_server(check) == server.id and site_group_allowed(server.token_site_groups, check)
    )


def uses_agent(check) -> bool:
    return bool(check.probe_local and (trusted_server(check) or not requires_trusted_server(check)))


def explain_panel_fallback(check, outcome) -> None:
    if check.probe_local and not uses_agent(check) and outcome.status == "down":
        outcome.message += " · для проверки с учетными данными изнутри укажите сервер в зависимостях монитора"
