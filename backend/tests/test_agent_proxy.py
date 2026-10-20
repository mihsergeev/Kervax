"""Агент за прокси (агент 2.44): адрес соединения - это прокси, а не нода.

Ноде, чей адрес режут на пути к панели, агент ходит через прокси. Панель тогда видит
адрес прокси: страна и проверки снаружи шли бы по нему, а у всех нод за одним прокси он
был бы один. Адрес ноды в этом случае - только заданный руками (IP сервера)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

AGENT = Path(__file__).resolve().parents[2] / "agent"
REPORT = {"hostname": "h", "os": "Ubuntu", "agent_version": "2.44", "local_ip": "192.168.0.5",
          "cpu_percent": 1.0, "mem_used": 1, "mem_total": 2, "disks": []}


async def _server(client, auth_headers, **extra) -> str:
    r = await client.post("/api/servers", json={"name": "proxied", **extra}, headers=auth_headers)
    return r.json()["token"]


async def _report(client, token, **extra):
    r = await client.post(
        "/api/agent/report", json={**REPORT, **extra},
        headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "198.51.100.20, 172.18.0.1"})
    assert r.status_code == 200


async def _ext(client, auth_headers) -> dict:
    return next(s for s in (await client.get("/api/servers", headers=auth_headers)).json()
                if s["name"] == "proxied")


async def test_report_through_proxy_keeps_the_manual_ip(client: httpx.AsyncClient, auth_headers):
    token = await _server(client, auth_headers, agent_ip="203.0.113.40")
    await _report(client, token, proxy="proxy.example:3128")
    s = await _ext(client, auth_headers)
    assert s["external_ip"] == "203.0.113.40"  # не адрес прокси из X-Forwarded-For
    assert s["last_report"]["proxy"] == "proxy.example:3128"


async def test_report_through_proxy_without_manual_ip_leaves_it_empty(client: httpx.AsyncClient, auth_headers):
    token = await _server(client, auth_headers)
    await _report(client, token, proxy="proxy.example:3128")
    assert not (await _ext(client, auth_headers))["external_ip"]


async def test_direct_report_still_takes_the_connection_address(client: httpx.AsyncClient, auth_headers):
    token = await _server(client, auth_headers, agent_ip="203.0.113.40")
    await _report(client, token)
    assert (await _ext(client, auth_headers))["external_ip"] == "198.51.100.20"


def test_installers_send_every_panel_request_through_the_proxy():
    for name in ("install.sh", "install-ru.sh"):
        src = (AGENT / name).read_text(encoding="utf-8")
        assert "\r" not in src, name
        # каждый запрос к панели - через pcurl, голого curl к ней не осталось
        bare = [ln for ln in src.splitlines()
                if re.search(r"(^|[\s($])curl\s", ln) and "${URL%/}" in ln and not ln.lstrip().startswith("#")]
        assert not bare, (name, bare)
        assert src.count('pcurl -fsSL') >= 3 and 'pcurl -fsS --connect-timeout' in src, name
        # прокси уходит в конфиг агента (0600 вместе с токеном), а не в юнит или окружение
        conf = src.index('cat > "$CONF" <<EOF')
        assert src.index("printf 'proxy=%s\\n' \"$PROXY\" >> \"$CONF\"", conf) < src.index('chmod 0600 "$CONF"', conf), name


@pytest.mark.skipif(shutil.which("sh") is None or getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="нет sh или запущено под root (дальше установщик меняет систему)")
@pytest.mark.parametrize("proxy,ok", [
    ("http://user:secret@proxy.example:3128", True),
    ("socks5://proxy.example:1080", True),
    ("proxy.example:3128", False),
    ("ftp://proxy.example:21", False),
    ("http://user:se cret@proxy.example:3128", False),
])
def test_installer_checks_the_proxy_before_anything_else(proxy, ok):
    # до проверки root: неверный прокси отвергается сразу, верный доходит до "нужен root"
    r = subprocess.run(["sh", str(AGENT / "install.sh"), "--proxy", proxy, "https://panel.example", "tok"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert ("Root required" if ok else "--proxy must look like") in r.stderr, r.stderr
    assert "secret" not in r.stderr
