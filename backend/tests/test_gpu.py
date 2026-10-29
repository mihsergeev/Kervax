"""GPU NVIDIA (helper gpu-setup) и LLM-сервисы (агент 2.36): проблемы, перегрев, плашка, карточка,
обычные задержки и точки для графиков."""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import collector, gpu, llm_lat
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
    # Xid 79 до перезагрузки драйвера ушел вместе со старым драйвером
    b = _block(xid=[{"ts": TS - 120, "bus": "0000:41:00", "code": 48, "text": "An uncorrectable double bit error"},
                    {"ts": TS - 60, "bus": "0000:81:00", "code": 13, "text": "Graphics Exception"},
                    {"ts": TS - 7200, "bus": "0000:81:00", "code": 79, "text": "old"}], nv_load=TS - 3600)
    probs = gpu.problems(_rep(b), NOW)
    assert [(p[0], p[1]) for p in probs] == [(2, "GPU 0: ошибка Xid 48 - двойная ошибка ECC памяти: нужен сброс GPU или перезагрузка")]
    assert probs[0][2] == f"gpu:xid:41:00:48:{TS - 120}" and probs[0][3].startswith("2026-10-18T11:58")
    v = gpu.view(_rep(b), NOW)
    assert [(x["code"], x["gpu"], x["hw"], x["held"]) for x in v["xid"]] == [
        (13, 1, False, False), (48, 0, True, True), (79, 1, False, False)]


def test_xid_waiting_for_reset_holds_until_driver_reload():
    old = TS - 35 * 86400
    b = _block(xid=[{"ts": old, "bus": "0000:41:00", "code": 62, "text": "pid='<unknown>', name=<unknown>, 2a5d1c9c"},
                    {"ts": old + 60, "bus": "0000:41:00", "code": 13, "text": "Graphics Exception"},
                    {"ts": TS - 7200, "bus": "0000:81:00", "code": 92, "text": "High single-bit ECC error rate"},
                    {"ts": TS - 3 * 86400, "bus": "0000:81:00", "code": 154,
                     "text": "GPU recovery action changed from 0x1 (GPU Reset Required) to 0x0 (None)"}],
               nv_load=TS - 60 * 86400)
    probs = gpu.problems(_rep(b), NOW)
    # 62 месяц назад держится, 92 только час, 154 сказал "восстанавливать нечего", 13 - ошибка приложения
    assert [(p[0], p[2]) for p in probs] == [(1, f"gpu:xid:41:00:62:{old}")]
    assert probs[0][1].startswith("GPU 0: ошибка Xid 62 - мониторинг GPU слеп, нужен плановый сброс")
    assert probs[0][3].startswith("2026-09-13")
    v = gpu.view(_rep(b), NOW)
    assert [(x["code"], x["held"], x["hw"], x["old"], x["lv"]) for x in v["xid"]] == [
        (92, False, True, False, 1), (62, True, True, True, 1)]
    # Xid 154 с "нужен сброс" держится
    b["xid"][3]["text"] = "GPU recovery action changed from 0x0 (None) to 0x1 (GPU Reset Required)"
    assert {p[2] for p in gpu.problems(_rep(b), NOW)} == {f"gpu:xid:41:00:62:{old}", f"gpu:xid:81:00:154:{TS - 3 * 86400}"}
    # драйвер перезагрузили - карта сброшена, старой ошибки нет ни в проблемах, ни в карточке
    b["nv_load"] = TS - 86400
    assert gpu.problems(_rep(b), NOW) == []
    assert [x["code"] for x in gpu.view(_rep(b), NOW)["xid"]] == [92]
    # helper 0.2 без nv_load: держится, пока ошибка в его окне за сутки
    b2 = _block(xid=[{"ts": TS - 7200, "bus": "0000:41:00", "code": 119, "text": "Timeout after 6s of waiting for RPC"}])
    assert [p[2] for p in gpu.problems(_rep(b2), NOW)] == [f"gpu:xid:41:00:119:{TS - 7200}"]


def test_xid62_alone_is_blind_monitoring_not_broken():
    """Xid 62 один: встал микроконтроллер телеметрии, nvidia-smi отвечает, показания застыли, модель
    работает. Это предупреждение "мониторинг слеп", застывшие показания - его причина, не вторая тревога"""
    x62 = {"ts": TS - 7 * 86400, "bus": "0000:41:00", "code": 62, "text": "pid='<unknown>', name=<unknown>, 2a5d1c9c"}
    b = _block(xid=[x62], nv_load=TS - 30 * 86400)
    b["gpus"][0] = _g(0, "00000000:41:00.0", U0, frz=7 * 86400, temp=95, slow=89)
    rep = _rep(b)
    probs = gpu.problems(rep, NOW)
    assert [(p[0], p[2]) for p in probs] == [(1, f"gpu:xid:41:00:62:{TS - 7 * 86400}")]
    txt = probs[0][1]
    assert "упал внутренний микроконтроллер" in txt and "проверь сервис" in txt and "из гостя" not in txt
    # застывшая температура - не перегрев, загрузка слепой карты не в плашке и не на графике
    assert gpu.heat(rep, NOW) == (0, "")
    s = gpu.summary(rep, NOW)
    assert s["util"] == 12 and s["blind"] == 1 and s["frozen"] == 1
    assert [p["g"] for p in gpu.metric_gpu(rep["extras"], NOW, TS)] == ["1"]
    v = gpu.view(rep, NOW)
    assert [g["blind"] for g in v["gpus"]] == [True, False] and v["vm"] is False
    assert [(x["code"], x["lv"], x["held"]) for x in v["xid"]] == [(62, 1, True)]
    # Xid 62 только что: показания еще не застыли на час, а перегрев по ним уже не судим
    b2 = _block(xid=[{**x62, "ts": TS - 120}])
    b2["gpus"][0]["temp"] = 95
    assert gpu.heat(_rep(b2), NOW) == (0, "")
    # драйвер перезагрузили после Xid 62 - карта сброшена, застывшие показания снова своя тревога
    b["nv_load"] = TS - 86400
    assert [p[2] for p in gpu.problems(_rep(b), NOW)] == ["gpu:frozen:0"]


def test_xid62_red_when_gpu_really_broken():
    x62 = {"ts": TS - 7200, "bus": "0000:41:00", "code": 62, "text": ""}
    # на той же карте 119 (GSP завис) - красный; 48 на соседней карте 62 не красит
    b = _block(xid=[x62, {"ts": TS - 600, "bus": "0000:41:00", "code": 119, "text": "Timeout after 6s of waiting for RPC"}])
    assert {p[2]: p[0] for p in gpu.problems(_rep(b), NOW)}[f"gpu:xid:41:00:62:{TS - 7200}"] == 2
    b = _block(xid=[x62, {"ts": TS - 600, "bus": "0000:81:00", "code": 48, "text": ""}])
    got = {p[2]: p[0] for p in gpu.problems(_rep(b), NOW)}
    assert got[f"gpu:xid:41:00:62:{TS - 7200}"] == 1 and got[f"gpu:xid:81:00:48:{TS - 600}"] == 2
    # сервис на этой карте не отвечает - красный и сказано, какой
    dead = _llm(ok=False, err="timeout", up=7200)
    probs = gpu.problems(_rep(_block(xid=[x62]), llm=[dead]), NOW)
    assert probs[0][0] == 2 and probs[0][1].endswith("; не отвечает vLLM vllm-qwen")
    # не отвечает сервис на другой карте (tei на GPU 1) или модель еще грузится - не повод
    assert gpu.problems(_rep(_block(xid=[x62]), llm=[_llm(name="tei", engine="tei", ok=False, err="timeout", up=7200)]), NOW)[0][0] == 1
    assert gpu.problems(_rep(_block(xid=[x62]), llm=[_llm(ok=False, err="timeout", up=300)]), NOW)[0][0] == 1


def test_reset_advice_on_vm_says_not_from_guest():
    """На ВМ с пробросом сброс из гостя роняет ВМ: советы про сброс - перезагрузкой хоста гипервизора"""
    b = _block(xid=[{"ts": TS - 7200, "bus": "0000:41:00", "code": 62, "text": ""},
                    {"ts": TS - 600, "bus": "0000:81:00", "code": 48, "text": ""}],
               gpus=[_g(0, "00000000:41:00.0", U0), _g(1, "00000000:81:00.0", U1, remap=True)])
    for vm in ({"is_vm": True, "virt": "VMware"}, {"virt": "KVM"}):
        probs = {p[2]: p[1] for p in gpu.problems(_rep(b, **vm), NOW)}
        assert all("из гостя не сбрасывать" in t and "хоста гипервизора" in t for t in probs.values()), probs
        assert "nvidia-smi -r" in probs[f"gpu:xid:41:00:62:{TS - 7200}"]
        v = gpu.view(_rep(b, **vm), NOW)
        assert v["vm"] is True and all("из гостя не сбрасывать" in x["desc"] for x in v["xid"])
    # железо: обычный совет
    probs = {p[2]: p[1] for p in gpu.problems(_rep(b, virt="none", is_vm=False), NOW)}
    assert probs[f"gpu:xid:81:00:48:{TS - 600}"].endswith("нужен сброс GPU или перезагрузка")
    assert not any("из гостя" in t for t in probs.values())


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
    assert s == {"n": 2, "models": ["A100 80GB PCIe"], "util": 50, "mem": 75.8, "memt": 160.0, "helper": True, "ok": True,
                 "smi": True, "frozen": 0, "blind": 0}
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


def test_nouveau_card_is_not_a_gpu():
    """На одной ноде (Ryzen без встроенной графики) стоит GT 710 под монитор на nouveau: красная
    плашка "GPU нет связи" и просьба поставить helper были ложными"""
    card = [{"bus": "0000:2d:00.0", "dev": "128b", "drv": "nouveau"}]
    for rep in ({"gpu_pci": card, "clock_unix": TS},
                _rep(_block(smi=False, ok=False, gpus=[], procs=[], driver="", cuda="", pci=card))):
        assert not gpu.has_gpu(rep)
        assert not setup_needed("gpu-setup", rep)
        assert gpu.summary(rep, NOW) is None and gpu.view(rep, NOW) is None
        assert gpu.problems(rep, NOW) == []
    # helper перестал отрабатывать на такой ноде - про GPU, которой нет, тоже не говорим
    old = _rep(_block(ts=TS - 3600, smi=False, ok=False, gpus=[], procs=[], pci=card))
    assert gpu.problems(old, NOW) == []
    # поставили драйвер NVIDIA - карта вернулась
    assert gpu.has_gpu({"gpu_pci": [{**card[0], "drv": "nvidia"}]})


GPU_SETUP = Path(__file__).resolve().parents[2] / "agent" / "gpu-setup.sh"


def _when() -> str:
    return re.search(r'^KERVAX_SETUP_WHEN="([^"]+)"', GPU_SETUP.read_text(encoding="utf-8"), re.M).group(1)


def test_setup_when_rules():
    """Условие в двойных кавычках и после set -u: $ развернулся бы при запуске самого скрипта, а
    кавычки, слэши и скобки ломают разбор в плейбуке и установщике"""
    assert not set(_when()) & set("$`'\\{}"), _when()
    assert "nouveau" in _when()


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("sh") is None, reason="нужны sh и симлинки")
def test_setup_when_on_fake_sys(tmp_path):
    """Helper ставится на карту NVIDIA без драйвера и на nvidia, на nouveau - нет"""
    def tree(name, cards):
        root = tmp_path / name / "sys"
        (root / "bus/pci/devices").mkdir(parents=True)
        for bus, vendor, cls, drv in cards:
            d = root / "devices/pci0000:00" / bus
            d.mkdir(parents=True)
            (d / "vendor").write_text(vendor + "\n")
            (d / "class").write_text(cls + "\n")
            os.symlink(d, root / "bus/pci/devices" / bus)
            if drv:
                (root / "bus/pci/drivers" / drv).mkdir(parents=True, exist_ok=True)
                os.symlink(root / "bus/pci/drivers" / drv, d / "driver")
        return _when().replace("/sys/bus/pci/devices", str(root / "bus/pci/devices"))

    nic = ("0000:00:1f.6", "0x8086", "0x020000", "e1000e")
    cases = {
        "nouveau": ([nic, ("0000:2d:00.0", "0x10de", "0x030000", "nouveau"), ("0000:2d:00.1", "0x10de", "0x040300", "")], 1),
        "nodrv": ([nic, ("0000:41:00.0", "0x10de", "0x030200", "")], 0),
        "nvidia": ([nic, ("0000:41:00.0", "0x10de", "0x030200", "nvidia")], 0),
        "mixed": ([("0000:2d:00.0", "0x10de", "0x030000", "nouveau"), ("0000:41:00.0", "0x10de", "0x030200", "nvidia")], 0),
        "none": ([nic], 1),
    }
    env = {**os.environ, "PATH": "/usr/bin:/bin"}  # без nvidia-smi
    for name, (cards, rc) in cases.items():
        r = subprocess.run(["sh", "-c", tree(name, cards)], env=env, capture_output=True)
        assert r.returncode == rc, name


def test_no_driver_is_not_lost_link():
    """Карта без драйвера (drv "") и без nvidia-smi: плашка есть, но smi=false, это не потеря связи"""
    card = [{"bus": "0000:41:00.0", "dev": "20b5", "drv": ""}]
    rep = _rep(_block(smi=False, ok=False, gpus=[], procs=[], driver="", cuda="", pci=card))
    s = gpu.summary(rep, NOW)
    assert s["n"] == 1 and s["helper"] and not s["ok"] and s["smi"] is False
    assert gpu.view(rep, NOW)["smi"] is False
    assert gpu.problems(rep, NOW) == []


def _llm(**kw):
    x = {"engine": "vllm", "kind": "container", "name": "vllm-qwen", "model": "model-a", "port": 8000,
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
    # /metrics закрыт ключом - так настроено: в карточке подсказка, алерта нет
    locked = _llm(engine="litellm", name="litellm", ok=False, err="http 401", port=4000)
    assert gpu.llm_problems({"llm": [locked]}, None) == []
    v = gpu.llm_view({"llm": [locked]}, None)[0]
    assert v["locked"] and v["engine_name"] == "LiteLLM" and v["label"] == "LiteLLM litellm"
    assert gpu.llm_view({"llm": [_llm()]}, None)[0]["locked"] is False
    assert gpu.llm_problems({"llm": [_llm(engine="litellm", ok=False, err="refused", name="litellm")]}, None)[0][1] \
        .startswith("LiteLLM litellm: не отвечает на /metrics (порт закрыт)")
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
    # одна минута медленно - всплеск для раздела LLM, не алерт (llm_lat)
    assert gpu.llm_problems({"llm": [slow]}, st) == []
    # меньше 6 часов истории - обычного еще нет
    assert gpu.baseline(gpu.base_update(None, {"llm": [_llm()]}, NOW), x, "ttft") is None
    # раз в минуту, не чаще
    one = gpu.base_update(None, {"llm": [_llm()]}, NOW)
    assert gpu.base_update(one, {"llm": [_llm()]}, NOW + timedelta(seconds=20)) == one
    v = gpu.llm_view({"llm": [slow]}, st)[0]
    assert v["pub_port"] is None
    # контейнер с пробросом: в карточке порт хоста, агент ходит во внутренний
    dock = {"containers": [{"name": "tei-b", "ports": [{"ip": "0.0.0.0", "pub": 8002, "priv": 8000, "proto": "tcp"}]},
                           {"name": "vllm-qwen", "ports": [{"ip": "0.0.0.0", "pub": 9001, "priv": 9000, "proto": "tcp"}]}]}
    vs = gpu.llm_view({"llm": [_llm(), _llm(engine="tei", name="tei-b"), _llm(kind="pod", name="tei-b")],
                       "docker": dock}, None)
    assert [x["pub_port"] for x in vs] == [None, 8002, None]
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


def test_frozen_readings():
    """helper 0.2: показания карты не меняются час - строка проблемы, без загрузки в плашке,
    без точки на графике и без перегрева по застывшей температуре"""
    b = _block()
    b["gpus"][0] = _g(0, "00000000:41:00.0", U0, frz=7300, temp=95, slow=89)
    rep = _rep(b)
    probs = gpu.problems(rep, NOW)
    assert [(p[0], p[2]) for p in probs] == [(1, "gpu:frozen:0")]
    assert probs[0][1].startswith("GPU 0: показания nvidia-smi не меняются 2 ч")
    assert gpu.heat(rep, NOW) == (0, "")
    s = gpu.summary(rep, NOW)
    assert s["util"] == 12 and s["frozen"] == 1
    assert [x["frozen"] for x in gpu.view(rep, NOW)["gpus"]] == [7300, 0]
    assert [p["g"] for p in gpu.metric_gpu(rep["extras"], NOW, TS)] == ["1"]
    # меньше часа - еще не повод
    b["gpus"][0]["frz"] = 3000
    assert gpu.problems(_rep(b), NOW) == []


def _base():
    """Обычные задержки за 8 часов: TTFT 840 мс, TPOT 41 мс"""
    st, t = None, NOW - timedelta(hours=8)
    for h in range(8):
        for m in range(0, 60, 10):
            st = gpu.base_update(st, {"llm": [_llm()]}, t + timedelta(hours=h, minutes=m))
    return st


def _run(lat, base, x, start, minutes):
    for i in range(minutes):
        lat = llm_lat.update(lat, {"llm": [x]}, base, start + timedelta(minutes=i))
    return lat, start + timedelta(minutes=minutes - 1)


def test_llm_night_spike_not_alerted():
    """Ночью три запроса с длинным промптом: p95 TTFT в разы выше обычного полчаса - в разделе
    LLM видно когда и сколько запросов, в Telegram и "Что сломано" ничего"""
    base = _base()
    night = _llm(running=1, waiting=0, kv=3.0, req_s=0.01, ttft=[1.1, 2.6, 2.6])
    start = NOW - timedelta(minutes=29)
    lat, end = _run(None, base, night, start, 30)
    assert gpu.llm_problems({"llm": [night]}, base, lat, end) == []
    [ev] = gpu.llm_view({"llm": [night]}, base, lat, end)[0]["lat_recent"]
    assert ev["m"] == "ttft" and ev["peak"] == 2.6 and ev["base"] == 0.84 and ev["req"] == [3, 3]
    assert ev["from"] == start.isoformat() and ev["to"] == end.isoformat() and ev["load"] is False
    held = {"llm_base": base, "llm_lat": lat, "llm_since": (end - timedelta(minutes=20)).isoformat()}
    srv = _srv(held, rep=_rep(llm=[night]))
    assert collector._server_conditions(srv, end)["llm"][0] == 0
    assert not [r for r in collector.server_problems(srv, end) if r["kind"] == "llm"]


def test_llm_sustained_slow_under_load():
    """Под нагрузкой (60 запросов за 5 минут) TTFT втрое выше обычного держится - алерт после HOLD"""
    base = _base()
    busy = _llm(req_s=0.2, ttft=[2.5, 6.2, 9.8])
    lat, end = _run(None, base, busy, NOW - timedelta(minutes=16), 10)
    assert gpu.llm_problems({"llm": [busy]}, base, lat, end) == []
    lat, end = _run(lat, base, busy, end + timedelta(minutes=1), 6)
    [(lvl, txt, key)] = gpu.llm_problems({"llm": [busy]}, base, lat, end)
    assert lvl == 1 and key == "llm:ttft:vllm|container|vllm-qwen|8000"
    assert txt == ("vLLM vllm-qwen: задержка до первого токена p95 6.2 с, обычно 840 мс, "
                   "держится 15 мин при 60 запросах за 5 минут")
    [ev] = gpu.llm_view({"llm": [busy]}, base, lat, end)[0]["lat_recent"]
    assert ev["load"] is True and ev["req"] == [60, 60]
    # и в "Что сломано", и в алерте - после общей выдержки
    held = {"llm_base": base, "llm_lat": lat, "llm_since": (end - timedelta(minutes=20)).isoformat()}
    srv = _srv(held, rep=_rep(llm=[busy]))
    assert collector._server_conditions(srv, end)["llm"][0] == 1
    assert [r["kind"] for r in collector.server_problems(srv, end)].count("llm") == 1
    # задержка вернулась к обычной - серия кончилась, алерта нет
    lat = llm_lat.update(lat, {"llm": [_llm(req_s=0.2)]}, base, end + timedelta(minutes=1))
    assert gpu.llm_problems({"llm": [busy]}, base, lat, end + timedelta(minutes=1)) == []


def test_llm_load_gap_resets_series():
    """Нагрузка просела посреди серии: отсчет заново, разовые куски по 10 минут не алерт"""
    base = _base()
    busy, idle = _llm(req_s=0.2, ttft=[2.5, 6.2, 9.8]), _llm(req_s=0.02, ttft=[2.5, 6.2, 9.8])
    lat, end = _run(None, base, busy, NOW, 10)
    lat, end = _run(lat, base, idle, end + timedelta(minutes=1), 2)
    lat, end = _run(lat, base, busy, end + timedelta(minutes=1), 10)
    assert gpu.llm_problems({"llm": [busy]}, base, lat, end) == []
    # всплеск один: пропусков не было, нагрузка 6..60 запросов
    [ev] = llm_lat.recent(lat, busy, end)
    assert ev["req"] == [6, 60] and ev["load"] is False


def test_llm_spikes_kept_for_a_day():
    base = _base()
    night = _llm(req_s=0.01, ttft=[1.1, 2.6, 2.6])
    lat, end = _run(None, base, night, NOW, 3)
    # задержка снова обычная 5 минут - следующий всплеск отдельный
    lat, end = _run(lat, base, _llm(req_s=0.01), end + timedelta(minutes=1), 5)
    lat, end = _run(lat, base, _llm(req_s=0.01, ttft=[1.1, 3.1, 3.1]), end + timedelta(minutes=1), 2)
    evs = llm_lat.recent(lat, night, end)
    assert [e["peak"] for e in evs] == [3.1, 2.6]
    # через сутки первый ушел, второй еще виден
    later = NOW + timedelta(hours=24, minutes=5)
    assert [e["peak"] for e in llm_lat.recent(lat, night, later)] == [3.1]
    # раз в минуту, не чаще
    assert llm_lat.update(lat, {"llm": [night]}, base, end + timedelta(seconds=20)) == lat
