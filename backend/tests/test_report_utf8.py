"""Битый UTF-8 в отчете агента. helper ошибок в логах обрезал пример строки по байтам посреди
русской буквы, агент положил его файл в отчет как есть, и панель отвечала 400 на каждый отчет:
живая нода выглядела недоступной. Теперь битые байты заменяются, отчет доходит целиком."""
import gzip
import json


async def test_broken_utf8_in_helper_block_does_not_lose_the_report(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "stats-a"}, headers=auth_headers)
    sid, h = r.json()["server"]["id"], {"Authorization": f"Bearer {r.json()['token']}"}
    # как кладет агент: файл helper'а вставлен в JSON сырыми байтами, строка оборвана на 0xd0
    block = b'{"v":1,"sources":[{"where":"pod default/app-0","last":"\xd0\x90\xd0\xb3\xd0\xb5\xd0"}]}'
    body = (b'{"hostname":"stats-a","agent_version":"2.38","cpu_percent":3,"mem_used":1,"mem_total":2,'
            b'"disks":[],"extras":{"logerr":' + block + b'}}')
    r = await client.post("/api/agent/report", content=gzip.compress(body),
                          headers={**h, "Content-Type": "application/json", "Content-Encoding": "gzip"})
    assert r.status_code == 200, r.text
    srv = next(x for x in (await client.get("/api/servers", headers=auth_headers)).json() if x["id"] == sid)
    assert srv["last_report"]["cpu_percent"] == 3
    assert srv["last_report"]["extras"]["logerr"]["sources"][0]["last"] == "Аге�"
    # целый отчет не трогаем
    ok = json.dumps({"hostname": "stats-a", "agent_version": "2.38", "cpu_percent": 4, "mem_used": 1,
                     "mem_total": 2, "disks": [], "extras": {"logerr": {"v": 1, "note": "Агенты"}}}).encode()
    r = await client.post("/api/agent/report", content=gzip.compress(ok),
                          headers={**h, "Content-Type": "application/json", "Content-Encoding": "gzip"})
    assert r.status_code == 200, r.text


def test_logerr_helper_hands_over_valid_utf8():
    """logerr-setup 0.4: перед выдачей отчет проходит iconv -c, обрезанная буква не уходит в панель."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "agent" / "logerr-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.4" in src and b"\r" not in src.encode()
    conv, move = src.index('iconv -f UTF-8 -t UTF-8 -c "$OUT.tmp.$$"'), src.index('mv -f "$OUT.tmp.$$" "$OUT"')
    assert conv < move
