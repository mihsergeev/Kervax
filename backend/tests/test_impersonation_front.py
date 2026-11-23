"""Вход админа под чужой учеткой: токен админа на это время лежит в sessionStorage вкладки, а не
в localStorage. localStorage общий для всех вкладок и остается на диске после закрытия браузера,
а токен админа там был все время входа под чужой учеткой.

Тест собирает frontend/tests/impersonation.check.ts (rolldown из node_modules фронта)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONT = Path(__file__).resolve().parents[2] / "frontend"
CLI = FRONT / "node_modules" / "rolldown" / "bin" / "cli.mjs"
# заглушки хранилищ браузера; в localStorage - вход, начатый до обновления
BANNER = (
    "const kxStore=()=>{const m=new Map();return {_m:m,getItem:k=>m.has(k)?m.get(k):null,"
    "setItem:(k,v)=>{m.set(k,String(v))},removeItem:k=>{m.delete(k)}}};"
    "globalThis.localStorage=kxStore();globalThis.sessionStorage=kxStore();"
    "globalThis.window={location:{pathname:'/',search:''}};"
    "localStorage.setItem('kervax_token','imp-old');localStorage.setItem('kervax_admin_token','admin-old');"
    "localStorage.setItem('kervax_imp_id','7');"
)


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    node = shutil.which("node")
    if not node or not CLI.exists():
        pytest.skip("нет node или node_modules фронта (npm ci в frontend)")
    out = tmp_path_factory.mktemp("imp") / "check.mjs"
    subprocess.run(
        [node, str(CLI), "tests/impersonation.check.ts", "--format", "esm", "--platform", "node",
         "--banner", BANNER, "-o", str(out)],
        cwd=FRONT, check=True, capture_output=True,
    )
    run = subprocess.run([node, str(out)], cwd=FRONT, check=True, capture_output=True, text=True)
    return json.loads(run.stdout)


def test_old_session_moves_admin_token_out_of_local_storage(result):
    m = result["migrated"]
    assert m["impersonating"] is True
    assert m["local"] == {"kervax_token": "imp-old"}
    assert m["session"] == {"kervax_admin_token": "admin-old", "kervax_imp_id": "7"}


def test_return_to_self_restores_admin_and_closes_the_session(result):
    s = result["stopped"]
    assert s["token"] == "admin-old" and s["local"] == {"kervax_token": "admin-old"} and s["session"] == {}
    assert s["calls"] == [{"url": "/api/users/impersonation/7/stop", "auth": "Bearer admin-old"}]


def test_new_impersonation_keeps_admin_token_in_this_tab_only(result):
    s = result["started"]
    assert s["token"] == "imp-new" and s["impersonating"] is True
    assert s["local"] == {"kervax_token": "imp-new"}
    assert s["session"] == {"kervax_admin_token": "admin-old", "kervax_imp_id": "8"}
    assert result["otherTab"] is False


def test_owner_tab_confirms_with_admin_token_and_other_tabs_cannot(result):
    """Вход живет, пока его вкладка подтверждает (app/impersonate.py): токеном админа из
    sessionStorage, уходя - keepalive-запросом (sendBeacon не несет Authorization)."""
    beat = {"url": "/api/users/impersonation/8/alive", "auth": "Bearer admin-old"}
    assert result["beats"] == [
        {**beat, "body": '{"leaving":false}', "keepalive": False},
        {**beat, "body": '{"leaving":true}', "keepalive": True},
        {**beat, "body": '{"leaving":false}', "keepalive": False},
        {**beat, "body": '{"leaving":false}', "keepalive": False},
    ]
    # вход закончен - false (к себе), сбой сервера - не знаем (вход не заканчивается)
    assert result["alive"] == [True, True, False, None]
    # у соседней вкладки токена админа нет: подтверждать нечем, запроса нет
    assert result["otherBeat"] is None


def test_owner_tab_confirms_again_after_load_and_throttles_visibility(result):
    """Перезагрузка: keepalive "ухожу" старой страницы может дойти до сервера позже первого
    подтверждения новой и сжать вход до полуминуты - поэтому после загрузки и возврата из bfcache
    подтверждение повторяется через несколько секунд. Обычный pageshow второй раз не подтверждает,
    возврат на вкладку - не чаще раза в IMP_VISIBLE_MIN_MS."""
    assert result["schedule"] == {
        "load": ["beat"],
        "rebeat": ["beat", "beat"],
        "minute": ["beat", "beat", "beat"],
        "visible": ["beat"],
        "bfcache": ["beat", "beat", "leave"],
        "stopped": [],
    }
