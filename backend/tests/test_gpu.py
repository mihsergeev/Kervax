"""GPU NVIDIA (helper gpu-setup) и LLM-сервисы (агент 2.36): проблемы, перегрев, плашка, карточка,
обычные задержки и точки для графиков."""
from datetime import datetime, timedelta, timezone

from app import collector, gpu
from app.models import Server
from app.setup_scripts import setup_needed

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())
U0, U1 = "GPU-11111111-2222-3333-4444-555555555555", "GPU-66666666-7777-8888-9999-000000000000"


def _g(i, bus, uuid, **kw):
    g = {"i": i, "bus": bus, "name": "NVIDIA A100 80GB PCIe", "uuid": uuid, "util": 87, "mutil": 40, "mem": 71234,
         "memt": 81920, "temp": 64, "pw": 240.0, "pwl": 300.0, "clk": 1410, "clkm": 1410, "ps": "P0", "fan": None,
         "pm": "Enabled", "ecc_u": 0, "ecc_c": 0, "slow": 89, "shut": 92, "maxop": 85,
         "thr": {"hw": False, "sw": False, "brake": False, "pcap": False}, "retire": False, "remap": False,
         "remap_fail": False}
    g.update(kw)
    return g


def _block(**kw):
    b = {"v": 1, "ts": TS - 30, "smi": True, "ok": True, "err": "", "driver": "550.127.05", "cuda": "12.4",
         "kernel": "6.8.0-45-generic", "mod": True, "mod_built": True, "nvrm": "",
         "pci": [{"bus": "0000:41:00.0", "dev": "20b5", "drv": "nvidia"}, {"bus": "0000:81:00.0", "dev": "20b5", "drv": "nvidia"}],
         "gpus": [_g(0, "00000000:41:00.0", U0), _g(1, "00000000:81:00.0", U1, util=12, mem=6400, temp=38)],
         "procs": [{"gpu": U1, "pid": 900, "mem": 5900, "name": "text-embeddings", "kind": "container", "owner": "tei"},
                   {"gpu": U0, "pid": 700, "mem": 70100, "name": "python3", "kind": "container", "owner": "vllm-qwen"}],
         "xid": []}
    b.update(kw)
    return b


def _rep(block=None, **kw):
    r = {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": TS,
         "extras": {"gpu": block if block is not None else _block()}}
    r.update(kw)
    return r


def _srv(state=None, rep=None):
    return Server(id=1, name="gpu-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_healthy_gpu_has_no_problems():
    assert gpu.problems(_rep(), NOW) == []
    assert gpu.heat(_rep(), NOW) == (0, "")
    assert gpu.bus_key("00000000:41:00.0") == gpu.bus_key("0000:41:00.0") == gpu.bus_key("0000:41:00") == "41:00"


def test_driver_down_says_why():
    b = _block(ok=False, gpus=[], procs=[], err="NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.",
               mod=False, mod_built=False)
    [(lvl, txt, key, _since)] = gpu.problems(_rep(b), NOW)
    assert lvl == 2 and key == "gpu:drv"
    assert "модуля nvidia нет для ядра 6.8.0-45-generic" in txt and "DKMS" in txt
    b.update(mod=True, mod_built=True, nvrm="NVRM: API mismatch: the client has the version 550.127.05")
    assert "API mismatch" in gpu.problems(_rep(b), NOW)[0][1]
    # драйвер не установлен вовсе: карта не используется, не авария
    assert gpu.problems(_rep(_block(smi=False, ok=False, gpus=[])), NOW) == []


def test_lost_gpu_and_vm_passthrough():
    b = _block(pci=_block()["pci"] + [{"bus": "0000:c1:00.0", "dev": "20b5", "drv": ""},
                                      {"bus": "0000:e1:00.0", "dev": "20b5", "drv": "vfio-pci"}],
               xid=[{"ts": TS - 600, "bus": "0000:c1:00", "code": 79, "text": "GPU has fallen off the bus."}])
    probs = gpu.problems(_rep(b), NOW)
    lost = [p for p in probs if p[2].startswith("gpu:lost")]
    assert len(lost) == 1 and "0000:c1:00.0 пропала" in lost[0][1] and "Xid 79" in lost[0][1]
    # Xid 79 пропавшей карты уже в ее строке: отдельной строкой не дублируется, время - по нему
    assert not any(p[2].startswith("gpu:xid:c1:00") for p in probs) and lost[0][3]
    # отданная в виртуальную машину карта не наша
    assert not any("e1:00" in p[2] for p in probs)
    v = gpu.view(_rep(b), NOW)
    assert [p["lost"] for p in v["pci"]] == [False, False, True, False]


def test_xid_hardware_alerts_app_errors_do_not():
    b = _block(xid=[{"ts": TS - 120, "bus": "0000:41:00", "code": 48, "text": "An uncorrectable double bit error"},
                    {"ts": TS - 60, "bus": "0000:81:00", "code": 13, "text": "Graphics Exception"},
                    {"ts": TS - 7200, "bus": "0000:81:00", "code": 79, "text": "old"}])
    probs = gpu.problems(_rep(b), NOW)
    assert [(p[0], p[1]) for p in probs] == [(2, "GPU 0: ошибка Xid 48 - двойная ошибка ECC памяти: нужен сброс GPU или перезагрузка")]
    assert probs[0][2] == f"gpu:xid:41:00:48:{TS - 120}" and probs[0][3].startswith("2026-10-18T11:58")
    v = gpu.view(_rep(b), NOW)
    assert [(x["code"], x["gpu"], x["hw"]) for x in v["xid"]] == [(13, 1, False), (48, 0, True), (79, 1, True)]


def test_memory_waiting_for_reset_and_stale_block():
    b = _block(gpus=[_g(0, "00000000:41:00.0", U0, remap=True, ecc_u=1), _g(1, "00000000:81:00.0", U1, remap_fail=True)])
    got = {p[2]: p[0] for p in gpu.problems(_rep(b), NOW)}
    assert got == {"gpu:rf:1": 2, "gpu:remap:0": 1, "gpu:ecc:0": 1}
    stale = gpu.problems(_rep(_block(ts=TS - 3600)), NOW)
    assert [p[2] for p in stale] == ["gpu:stale"] and "1 ч" in stale[0][1]


def test_heat_levels():
    hot = _block(gpus=[_g(0, "00000000:41:00.0", U0, temp=90, clk=1110, thr={"hw": True, "sw": False}),
                       _g(1, "00000000:81:00.0", U1, temp=86)])
    assert gpu.heat(_rep(hot), NOW) == (2, "GPU 0 перегрев 90°C: частота снижена до 1110 из 1410 МГц")
    warm = _block(gpus=[_g(0, "00000000:41:00.0", U0, temp=86)])
    assert gpu.heat(_rep(warm), NOW) == (1, "GPU 0 греется: 86°C, замедление начнется с 89°C")
    # карта без своих пределов: 90 и за 5 градусов до него
    plain = _block(gpus=[_g(0, "00000000:41:00.0", U0, temp=86, slow=None, maxop=None)])
    assert gpu.heat(_rep(plain), NOW)[0] == 1


def test_summary_and_view():
    s = gpu.summary(_rep(), NOW)
    assert s == {"n": 2, "models": ["A100 80GB PCIe"], "util": 50, "mem": 75.8, "memt": 160.0, "helper": True, "ok": True}
    # без helper'а - по шине из отчета агента
    s = gpu.summary({"gpu_pci": [{"bus": "0000:41:00.0", "dev": "20b5", "drv": "nvidia"}]}, NOW)
    assert s["n"] == 1 and not s["helper"] and s["util"] is None
    assert gpu.summary({"mem_total": 1}, NOW) is None
    v = gpu.view(_rep(), NOW)
    assert v["driver"] == "550.127.05" and v["cuda"] == "12.4" and not v["stale"]
    assert [(p["gpu"], p["owner"]) for p in v["procs"]] == [(0, "vllm-qwen"), (1, "tei")]
    assert v["gpus"][0]["name"] == "A100 80GB PCIe"


def test_conditions_and_home():
    b = _block(ok=False, gpus=[], procs=[], err="NVIDIA-SMI has failed", mod=True, mod_built=True)
    lvl, ctx = collector._server_conditions(_srv(rep=_rep(b)), NOW)["gpu"]
    assert lvl == 2 and ctx["sig"] == ["gpu:drv"] and "драйвер NVIDIA не отвечает" in ctx["detail"]
    rows = [r for r in collector.server_problems(_srv(rep=_rep(b)), NOW) if r["kind"] == "gpu"]
    assert len(rows) == 1 and rows[0]["sec"] == "gpu"
    # перегрев - только когда держится дольше выдержки
    hot = _rep(_block(gpus=[_g(0, "00000000:41:00.0", U0, temp=91)], pci=_block()["pci"][:1]))
    assert collector._server_conditions(_srv(rep=hot), NOW)["gpu_heat"][0] == 0
    held = {"gpu_heat_since": (NOW - timedelta(minutes=20)).isoformat()}
    assert collector._server_conditions(_srv(held, rep=hot), NOW)["gpu_heat"][0] == 2
    assert [r["kind"] for r in collector.server_problems(_srv(held, rep=hot), NOW)] == ["gpu_heat"]


def test_setup_needed():
    assert setup_needed("gpu-setup", {"gpu_pci": [{"bus": "0000:41:00.0", "drv": "nvidia"}]})
    assert setup_needed("gpu-setup", _rep())
    assert not setup_needed("gpu-setup", {"mem_total": 1})


def _llm(**kw):
    x = {"engine": "vllm", "kind": "container", "name": "vllm-qwen", "model": "Qwen/Qwen2.5-72B", "port": 8000,
         "up": 86400, "ok": True, "win": 300, "running": 12, "waiting": 0, "kv": 62.0, "req_s": 2.4,
         "prompt_tps": 9200, "gen_tps": 1840, "ttft": [0.31, 0.84, 1.9], "tpot": [0.028, 0.041, 0.055],
         "e2e": [7.9, 21.4, 38]}
    x.update(kw)
    return x


def test_llm_problems():
    assert gpu.llm_problems({"llm": [_llm()]}, None) == []
    down = gpu.llm_problems({"llm": [_llm(ok=False, err="refused", win=None)]}, None)
    assert down == [(2, "vLLM vllm-qwen: не отвечает на /metrics (порт закрыт), процесс работает 1 сут",
                     "llm:down:vllm|container|vllm-qwen|8000")]
    # модель еще грузится, агенту некуда стучаться, метрики не включены - не авария
    assert gpu.llm_problems({"llm": [_llm(ok=False, err="refused", up=300), _llm(ok=False, err="local", name="b"),
                                     _llm(engine="sglang", ok=False, no_metrics=True, name="c")]}, None) == []
    q = gpu.llm_problems({"llm": [_llm(waiting=18, running=12, kv=97, preempt=4)]}, None)
    assert [p[2].split(":")[1] for p in q] == ["queue", "kv"]
    assert q[1][1] == "vLLM vllm-qwen: KV-кэш заполнен на 97%, за 5 минут вытеснено 4 запроса"


def test_llm_baseline_and_slow_ttft():
    st = None
    t = NOW - timedelta(hours=8)
    for h in range(8):
        for m in range(0, 60, 10):
            st = gpu.base_update(st, {"llm": [_llm()]}, t + timedelta(hours=h, minutes=m))
    st = gpu.base_update(st, {"llm": [_llm()]}, NOW)
    x = _llm()
    assert gpu.baseline(st, x, "ttft") == 0.84 and gpu.baseline(st, x, "tpot") == 0.041
    slow = _llm(ttft=[2.5, 6.2, 9.8])
    [(lvl, txt, key)] = gpu.llm_problems({"llm": [slow]}, st)
    assert lvl == 1 and txt == "vLLM vllm-qwen: задержка до первого токена p95 6.2 с, обычно 840 мс"
    # меньше 6 часов истории - обычного еще нет
    assert gpu.baseline(gpu.base_update(None, {"llm": [_llm()]}, NOW), x, "ttft") is None
    # раз в минуту, не чаще
    one = gpu.base_update(None, {"llm": [_llm()]}, NOW)
    assert gpu.base_update(one, {"llm": [_llm()]}, NOW + timedelta(seconds=20)) == one
    v = gpu.llm_view({"llm": [slow]}, st)[0]
    assert v["base_ttft"] == 0.84 and v["where"] == "контейнер vllm-qwen" and v["engine_name"] == "vLLM"


def test_llm_conditions_need_sustain():
    rep = _rep(llm=[_llm(waiting=20, running=4)])
    assert collector._server_conditions(_srv(rep=rep), NOW)["llm"][0] == 0
    held = {"llm_since": (NOW - timedelta(minutes=20)).isoformat()}
    assert collector._server_conditions(_srv(held, rep=rep), NOW)["llm"][0] == 1
    rows = [r for r in collector.server_problems(_srv(held, rep=rep), NOW) if r["kind"] == "llm"]
    assert len(rows) == 1 and rows[0]["section"] == "services" and rows[0]["sec"] == "llm"


def test_metric_points():
    pts = gpu.metric_gpu({"gpu": _block()}, NOW, TS)
    assert pts[0] == {"g": "0", "u": 87, "m": 87.0, "t": 64, "p": 80.0}
    assert gpu.metric_gpu({"gpu": _block(ts=TS - 600)}, NOW, TS) is None
    assert gpu.metric_llm([_llm(), _llm(ok=False, name="x")]) == [
        {"k": "vLLM vllm-qwen", "run": 12, "wait": 0, "kv": 62.0, "gen": 1840, "req": 2.4,
         "ttft": 0.84, "tpot": 0.041, "e2e": 21.4}]


async def test_report_api(client, auth_headers):
    r = (await client.post("/api/servers", json={"name": "gpu-a"}, headers=auth_headers)).json()
    sid, ah = r["server"]["id"], {"Authorization": f"Bearer {r['token']}"}
    blk = _block(ts=int(datetime.now(timezone.utc).timestamp()))
    rep = {"agent_version": "2.36", "mem_used": 1, "mem_total": 2, "disks": [{"mount": "/", "used": 1, "total": 2}],
           "gpu_pci": blk["pci"], "llm": [_llm()], "extras": {"gpu": blk}}
    assert (await client.post("/api/agent/report", json=rep, headers=ah)).status_code == 200
    srv = (await client.get(f"/api/servers/{sid}", headers=auth_headers)).json()
    assert srv["gpu_sum"]["n"] == 2 and srv["gpu"]["gpus"][1]["util"] == 12
    assert srv["llm"][0]["label"] == "vLLM vllm-qwen" and srv["last_report"]["gpu_pci"][0]["bus"] == "0000:41:00.0"
    m = (await client.get(f"/api/servers/{sid}/metrics?hours=1", headers=auth_headers)).json()
    assert m[-1]["gpu"][0]["g"] == "0" and m[-1]["llm"][0]["k"] == "vLLM vllm-qwen"
