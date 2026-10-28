"""Файлы в data/ пишутся через временный файл и rename. Имя временного должно быть свое у каждой
записи: у трех воркеров uvicorn общее agent_allow_ips.tmp давало гонку - второй писатель
переименовывал файл первого, и os.replace первого падал FileNotFoundError, PATCH сервера отвечал
500 уже после коммита (журнал ошибок панели 23.10, 1.8.11 и 1.8.17)."""
import os
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def _race(monkeypatch, other_writer):
    """os.replace, перед которым успевает дописать и переименовать свой файл второй писатель -
    как второй воркер между записью временного файла и rename первого."""
    real = os.replace
    state = {"inside": False, "tmps": []}

    def replace(src, dst):
        state["tmps"].append(str(src))
        if not state["inside"]:
            state["inside"] = True
            try:
                other_writer()
            finally:
                state["inside"] = False
        return real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    return state


def test_no_fixed_tmp_names_in_app():
    """Общее имя вида path + ".tmp" или f"{path}.tmp" - та самая гонка, если писателей больше одного."""
    fixed = re.compile(r"""\+\s*["']\.tmp["']|f["']\{\w+\}\.tmp["']""")
    bad = [f"{p.relative_to(APP)}:{i}" for p in APP.rglob("*.py")
           for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if fixed.search(line)]
    assert bad == []


def test_branding_meta_two_writers(monkeypatch, tmp_path):
    from app.api import branding
    monkeypatch.setattr(branding, "_dir", lambda: str(tmp_path))
    st = _race(monkeypatch, lambda: branding._write_meta({"title": "второй"}))
    branding._write_meta({"title": "первый"})
    assert len(set(st["tmps"])) == 2
    assert branding._read_meta() == {"title": "первый"}
    assert sorted(os.listdir(tmp_path)) == ["meta.json"]


def test_branding_meta_failed_write_leaves_no_tmp(monkeypatch, tmp_path):
    from app.api import branding
    monkeypatch.setattr(branding, "_dir", lambda: str(tmp_path))

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        branding._write_meta({"title": "x"})
    assert os.listdir(tmp_path) == []


async def test_heartbeat_two_writers(monkeypatch, tmp_path):
    import asyncio

    from app import heartbeat
    from app.config import Settings
    settings = Settings(data_dir=str(tmp_path), panel_url="https://kervax.example")
    cfg = {"webhook": "http://hook"}

    def other():
        # второй писатель в своем потоке со своим циклом событий
        import threading
        t = threading.Thread(target=lambda: asyncio.run(heartbeat.write_heartbeat(None, settings, cfg, True)))
        t.start()
        t.join()

    st = _race(monkeypatch, other)
    await heartbeat.write_heartbeat(None, settings, cfg, True)
    assert len(set(st["tmps"])) == 2
    assert sorted(os.listdir(tmp_path)) == ["heartbeat"]
    assert "alerts_ok=1" in (tmp_path / "heartbeat").read_text(encoding="utf-8")
    if os.name == "posix":
        assert (tmp_path / "heartbeat").stat().st_mode & 0o777 == 0o600


async def test_agent_allowlist_unique_tmp_and_patch_survives(client, auth_headers, monkeypatch):
    """PATCH сервера (заглушки "Требует действий") пишет agent_allow_ips: временный файл свой у
    каждой записи, а сбой записи не превращает уже сохраненную правку в 500."""
    from app import config
    r = await client.post("/api/servers", json={"name": "a", "agent_ip": "203.0.113.7"}, headers=auth_headers)
    assert r.status_code == 201, r.text
    sid = r.json()["server"]["id"]
    data = config.get_settings().data_dir
    real = os.replace
    tmps: list[str] = []

    def spy(src, dst):
        tmps.append(str(src))
        return real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    for mutes in (["osupd:eol@0a1b2c3d4e"], ["osupd:eol@0a1b2c3d4e", "osupd:reboot@5f6a7b8c9d"]):
        r = await client.patch(f"/api/servers/{sid}", json={"alert_mutes": mutes}, headers=auth_headers)
        assert r.status_code == 200, r.text
    mine = [t for t in tmps if os.path.basename(t).startswith("agent_allow_ips")]
    assert len(mine) == 2 and len(set(mine)) == 2
    assert all(not t.endswith("agent_allow_ips.tmp") for t in mine)

    def gone(src, dst):
        raise FileNotFoundError(2, "No such file or directory", str(src))

    monkeypatch.setattr(os, "replace", gone)
    r = await client.patch(f"/api/servers/{sid}", json={"alert_mutes": []}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["alert_mutes"] == []
    assert not [n for n in os.listdir(data) if n.startswith("agent_allow_ips.")]
