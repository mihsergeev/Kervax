"""Записи журнала ошибок панели 24-26.10 - падения интерфейса:

- `TypeError ... (reading 'split')` в "Сервисах" у учетки только для просмотра без галки "может читать
  логи": бэкенд (_strip_logs) вырезает у нее строковые ключи log, в том числе путь access-лога в
  web-rate, а интерфейс брал l.log.split('/');
- `TypeError ... (reading 'length')` в файлах сайтов: вкладка, открытая до выкладки 1.8.28, со
  старым JS читала r.edit.length, а новый бэкенд edit уже не отдавал. Такая вкладка теперь узнает,
  что панель обновили, не пишет ошибку в журнал и перезагружается на новую сборку.

Тест собирает frontend/tests/panelErrors.check.ts (rolldown из node_modules фронта) и кормит
ему ответ настоящего API наблюдателю."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_api_access import _report, _server, _user

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"
CLI = FRONT / "node_modules" / "rolldown" / "bin" / "cli.mjs"

POD_LOG = "/var/log/pods/default_shop-web-7d9f8c6b5-x2x4z_0c1d/web/0.log"
HOST_LOG = "/var/log/nginx/access.log"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    node = shutil.which("node")
    if not node or not CLI.exists():
        pytest.skip("нет node или node_modules фронта (npm ci в frontend)")
    out = tmp_path_factory.mktemp("perr") / "check.mjs"
    subprocess.run(
        [node, str(CLI), "tests/panelErrors.check.ts", "--format", "esm", "--platform", "node",
         "--banner", "globalThis.localStorage={getItem(){return null},setItem(){},removeItem(){}};",
         "-o", str(out)],
        cwd=FRONT, check=True, capture_output=True,
    )
    return node, out


async def _viewer_server(client, auth_headers, can_read_logs: bool) -> dict:
    sid, ah = await _server(client, auth_headers, f"node-{int(can_read_logs)}")
    import time
    now = int(time.time())
    rep = _report(clock_unix=now, extras={
        "web-rate": {"ts": now, "rpm": 8, "logs": [
            {"log": POD_LOG, "name": "default/shop-web-7d9f8c6b5-x2x4z", "rpm": 5, "sites": []},
            {"log": HOST_LOG, "name": "", "rpm": 3, "sites": []},
        ]},
        "units": {"v": 1, "ts": now, "fix": False, "units": [
            {"unit": "app.service", "desc": "app", "type": "simple", "result": "exit-code", "status": 1,
             "since": now - 60, "restarts": 3, "log": ["app.service: Main process exited, code=exited"]},
        ]},
    })
    assert (await client.post("/api/agent/report", json=rep, headers=ah)).status_code == 200
    vh = await _user(client, auth_headers, f"watch{int(can_read_logs)}", role="viewer", can_read_logs=can_read_logs)
    r = await client.get(f"/api/servers/{sid}", headers=vh)
    assert r.status_code == 200, r.text
    return r.json()


def _run(bundle, srv: dict, tmp_path) -> dict:
    node, out = bundle
    data = tmp_path / "srv.json"
    data.write_text(json.dumps(srv), encoding="utf-8")
    run = subprocess.run([node, str(out), str(data)], check=True, capture_output=True)
    return json.loads(run.stdout.decode("utf-8"))


async def test_viewer_without_logs_rate_labels(client, auth_headers, bundle, tmp_path):
    r = _run(bundle, await _viewer_server(client, auth_headers, False), tmp_path)
    # пути лога наблюдателю без галки не отдаются - на этом интерфейс и падал
    assert r["has_log"] == [False, False]
    assert r["labels"] == ["default/shop-web", "?"]
    assert r["unit_logs"] == [0]


async def test_viewer_with_logs_rate_labels(client, auth_headers, bundle, tmp_path):
    r = _run(bundle, await _viewer_server(client, auth_headers, True), tmp_path)
    assert r["has_log"] == [True, True]
    assert r["labels"] == ["default/shop-web", "access.log"]
    assert r["unit_logs"] == [1]


async def test_stale_tab_knows_panel_was_updated(client, auth_headers, bundle, tmp_path):
    r = _run(bundle, await _viewer_server(client, auth_headers, True), tmp_path)["stale"]
    assert r["bundle"] == "/assets/index-AbC123_x.js"
    assert r["dev_bundle"] is None
    assert r["updated"] == "/assets/index-new2.js"
    # та же сборка, dev-сервер и непонятный ответ (502 во время выкладки) - не старая вкладка
    assert r["same"] is None and r["same_abs"] is None
    assert r["dev"] is None and r["broken_answer"] is None
