"""Здоровье баз (helper dbstat-setup 0.5): репликация, долгие транзакции, блокировки, wraparound,
таблицы, которые давно не вакуумились. Блок extras["db-health"], helper обновляет его раз в
15 минут. С 0.8 у PostgreSQL еще и "что грузит базу" за последний час (load_view), с 0.9 настройки
и советы по ним (advice).

Почему это отдельно от инвентаря: база может быть жива и отвечать, а реплика уже час не
получает WAL, незакрытая транзакция держит блокировки и не дает VACUUM чистить, а возраст XID
ползет к остановке на wraparound. Снаружи этого не видно, пока не станет поздно.
"""
from __future__ import annotations

from datetime import datetime

LAG_WARN = 300  # реплика отстает на 5 минут - предупреждение
LAG_CRIT = 1800  # на полчаса - проблема
XACT_WARN = 3600  # транзакция идет час
XACT_CRIT = 6 * 3600
IDLE_WARN = 1800  # транзакция открыта и ничего не делает полчаса: держит блокировки и VACUUM
LOCK_WARN = 300  # запрос ждет блокировку дольше 5 минут
XID_WARN = 1_000_000_000  # на 2.1 млрд PostgreSQL остановится, чтобы не потерять данные
XID_CRIT = 1_500_000_000
VAC_DAYS = 7
# Дамп держит одну транзакцию от начала до конца, это его природа: ночной pg_dump большой базы
# шел четыре часа и давал алерт "транзакция идет 1 ч". Про дамп скажем, только если он идет
# больше 12 часов - тогда он, скорее всего, завис.
_DUMP_APPS = ("pg_dump", "pg_dumpall", "pg_basebackup")
DUMP_XACT_WARN = 12 * 3600
STALE = 3600  # блок старше часа - helper не работает, не судим по старому


def block(rep: dict, now: datetime) -> list[dict] | None:
    b = ((rep or {}).get("extras") or {}).get("db-health")
    if not isinstance(b, dict) or b.get("v") != 1 or not isinstance(b.get("items"), list):
        return None
    try:
        if now.timestamp() - float(b.get("ts") or 0) > STALE:
            return None
    except (TypeError, ValueError):
        return None
    # helper 0.5 принимал ошибку docker exec за ответ базы (postgres-exporter подходит под шаблон
    # образа, а psql в нем нет): такие записи - не база
    return [x for x in b["items"] if isinstance(x, dict) and "exec failed" not in str(x.get("xact_app") or "")
            and "exec failed" not in str(x.get("receiver") or "")]


def _dur(sec: int) -> str:
    if sec >= 86400:
        return f"{sec // 86400} сут {sec % 86400 // 3600} ч"
    if sec >= 3600:
        return f"{sec // 3600} ч {sec % 3600 // 60} мин"
    return f"{max(sec // 60, 1)} мин"


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _who(db: str, app: str) -> str:
    parts = [f"база {db}" if db else "", app]
    return ", ".join(p for p in parts if p)


def problems(rep: dict, now: datetime) -> list[tuple[int, str, str]]:
    """(уровень, текст, ключ): ключ - что именно сломалось, чтобы новая беда дала свой алерт"""
    items = block(rep, now)
    out: list[tuple[int, str, str]] = []
    for x in items or []:
        eng = "PostgreSQL" if x.get("engine") == "pg" else "MySQL" if x.get("engine") == "mysql" else "СУБД"
        where = str(x.get("container") or "")
        name = f"{eng} {where}" if where else f"{eng} на хосте"
        k = f"{x.get('engine')}:{where}"
        lag = _int(x.get("lag"))
        if x.get("role") == "replica":
            if x.get("engine") == "pg" and str(x.get("receiver") or "") != "streaming":
                out.append((2, f"{name}: реплика не получает WAL от основной (статус {x.get('receiver') or 'нет'})",
                            k + ":stream"))
            if x.get("engine") == "mysql" and (x.get("io") != "Yes" or x.get("sql") != "Yes"):
                err = str(x.get("err") or "").strip()
                out.append((2, f"{name}: репликация стоит (IO {x.get('io') or '?'}, SQL {x.get('sql') or '?'})"
                               + (f": {err[:160]}" if err else ""), k + ":stream"))
            elif lag >= LAG_WARN:
                out.append((2 if lag >= LAG_CRIT else 1, f"{name}: реплика отстает на {_dur(lag)}", k + ":lag"))
        elif lag >= LAG_WARN:
            out.append((2 if lag >= LAG_CRIT else 1, f"{name}: реплика отстает от основной на {_dur(lag)}", k + ":lag"))
        xa = _int(x.get("xact"))
        if xa >= XACT_WARN and not (str(x.get("xact_app") or "").startswith(_DUMP_APPS) and xa < DUMP_XACT_WARN):
            who = _who(str(x.get("xact_db") or ""), str(x.get("xact_app") or ""))
            out.append((2 if xa >= XACT_CRIT else 1, f"{name}: транзакция идет {_dur(xa)}" + (f" ({who})" if who else ""),
                        k + ":xact"))
        # Простаивающая транзакция со снимком (backend_xmin, dbstat-setup 0.6) не дает VACUUM чистить
        # мертвые строки во всей базе. Без снимка она держит только свои блокировки: на одной ноде
        # самой старой была такая (advisory lock), а VACUUM держала соседняя после SELECT
        idle, imin = _int(x.get("idle_xact")), _int(x.get("idle_xmin"))
        if imin >= IDLE_WARN:
            who = _who(str(x.get("idle_xmin_db") or ""), str(x.get("idle_xmin_app") or ""))
            out.append((1, f"{name}: транзакция открыта и простаивает {_dur(imin)}" + (f" ({who})" if who else "")
                           + ": держит снимок, VACUUM не может чистить мертвые строки", k + ":idle"))
        # Простаивающая без снимка сама по себе никому не мешает: на одной ноде воркер сутками держит
        # так один advisory lock, и строка "держит блокировки" только шумела. Про нее - когда кто-то
        # ждет блокировку, в той же строке: скорее всего держит она
        lw, lwt = _int(x.get("lock_waits")), _int(x.get("lock_wait"))
        if lw and lwt >= LOCK_WARN:
            txt = f"{name}: ждут блокировку {lw} запр., дольше всех {_dur(lwt)}"
            if idle >= IDLE_WARN and idle > imin + 60:
                who = _who(str(x.get("idle_db") or ""), str(x.get("idle_app") or ""))
                txt += f", а транзакция простаивает {_dur(idle)}" + (f" ({who})" if who else "") + " - держит, скорее всего, она"
            out.append((1, txt, k + ":lock"))
        xid = _int(x.get("xid"))
        if xid >= XID_WARN:
            out.append((2 if xid >= XID_CRIT else 1,
                        f"{name}: возраст транзакций базы {x.get('xid_db') or '?'} {xid / 1e9:.2f} млрд, на 2.1 млрд "
                        f"PostgreSQL остановит запись - автоочистка не успевает замораживать строки", k + ":xid"))
        for d in x.get("dead") or []:
            if not isinstance(d, dict):
                continue
            vac, dead, live = _int(d.get("vac")), _int(d.get("dead")), _int(d.get("live"))
            if vac != -1 and vac < VAC_DAYS * 86400:
                continue
            when = "не было ни разу" if vac == -1 else f"не было {vac // 86400} дней"
            pct = f", {round(dead * 100 / live)}% от живых" if live else ""
            out.append((1, f"{name}: в {d.get('db')}.{d.get('table')} {dead / 1e6:.1f} млн мертвых строк{pct}, "
                           f"VACUUM {when}", f"{k}:vac:{d.get('db')}.{d.get('table')}"))
    out.sort(key=lambda r: -r[0])
    return out


# Что грузит PostgreSQL (dbstat-setup 0.8): разница счетчиков со снимком примерно часовой давности и
# самые долгие запросы из pg_stat_statements. Без алертов: это разбор для того, кто пришел выяснять,
# почему база тормозит или диски изнашиваются, а не авария
_LOAD_KEYS = ("temp_b", "temp_f", "xact", "rd", "wal_rec", "fpi", "wal_b", "wbf", "ck_t", "ck_r")


def load_view(x: dict) -> dict | None:
    """Счетчики в пересчете на час (None - эта версия PostgreSQL их не считает) и самые долгие
    запросы с долей во времени всех запросов"""
    ld = x.get("load")
    if not isinstance(ld, dict):
        return None
    win = _int(ld.get("win"))
    out: dict = {"win": max(win, 0), "pss": str(ld.get("pss") or ""), "qtext": max(_int(ld.get("qtext")), 0),
                 "cut": bool(ld.get("cut")), "top": []}
    if win <= 0:
        return out
    k = 3600 / win
    for key in _LOAD_KEYS:
        v = _int(ld.get(key))
        out[key] = round(v * k) if v >= 0 else None
    ms_all = max(_int(ld.get("ms_all")), 0)
    out["ms_all"] = round(ms_all * k)
    # Одинаковый текст в одной базе - один запрос: PostgreSQL заводит отдельную запись на каждую роль
    # и каждый набор типов параметров, на одной ноде самый тяжелый SELECT шел тремя записями
    top: dict[tuple[str, str], dict] = {}
    for q in ld.get("top") or []:
        if not isinstance(q, dict):
            continue
        txt = str(q.get("q") or "")
        key = (str(q.get("db") or ""), txt or "#" + str(q.get("id") or ""))
        r = top.setdefault(key, {"db": key[0], "q": txt, "calls": 0, "ms": 0, "temp": 0, "rd": 0})
        for f in ("calls", "ms", "temp", "rd"):
            r[f] += max(_int(q.get(f)), 0)
    rows = sorted(top.values(), key=lambda r: -r["ms"])
    for r in rows:
        r["share"] = round(r["ms"] * 100 / ms_all) if ms_all else 0
        r["avg_ms"] = round(r["ms"] / r["calls"], 1) if r["calls"] else 0
        for f in ("calls", "ms", "temp", "rd"):
            r[f] = round(r[f] * k)
    out["top"] = rows
    return out


# Советы по настройкам PostgreSQL (dbstat-setup 0.9): настройки против машины и против нагрузки из
# load_view. Только то, что прямо следует из цифр. Без алертов и без строк в "Что сломано": это не
# поломка, а то, что стоит поправить при случае. Текст рисует фронт по ключу k (там же перевод)
GIB = 1 << 30
MIB = 1 << 20
HP_MIN_SB = 8 * GIB  # с каких буферов звать на huge pages: таблицы страниц растут с каждым подключением
STOP_MIN_SB = GIB  # с каких буферов 10 секунд docker на остановку мало
STOP_OK = 60
TEMP_ADVICE = 10 * GIB  # временных файлов в час
WBF_ADVICE = 100  # переполнений буфера WAL в час
WB_GOOD = 128 * MIB


def _pow2_gib(n: float) -> int:
    g = 1
    while g * GIB < n:
        g *= 2
    return g * GIB


def _flt(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ssd(rep: dict) -> bool | None:
    """Все диски сервера SSD (helper diskhealth-setup), None - неизвестно. Диски ВМ не в счет:
    rotational у виртуального диска выставляет гипервизор как попало"""
    disks = (((rep or {}).get("extras") or {}).get("disk-health") or {}).get("disks") or []
    rota = [d.get("rota") for d in disks if isinstance(d, dict) and d.get("err") != "virtual"]
    if not rota:
        return None
    return all(r == 0 for r in rota)


def _db_size(rep: dict, container: str) -> int:
    for d in (rep or {}).get("db_stats") or []:
        if isinstance(d, dict) and d.get("engine") == "pg" and (d.get("container") or "") == container:
            return sum(_int(x.get("size")) for x in d.get("dbs") or [] if isinstance(x, dict))
    return 0


def advice(x: dict, lv: dict | None, *, pg_count: int = 1, ssd: bool | None = None, db_size: int = 0) -> list[dict]:
    cf = x.get("cfg")
    if not isinstance(cf, dict):
        return []
    out: list[dict] = []
    sb, mem = _int(cf.get("sb")), _int(cf.get("mem"))
    hp, hps = str(cf.get("hp") or ""), str(cf.get("hps") or "")
    hpn, hpt, hpsz = _int(cf.get("hpn")), _int(cf.get("hpt")), _int(cf.get("hpsz")) or 2 * MIB
    # Huge pages. С huge_pages = on без них postgres не запустился бы; до PG17 не видно, взял ли он их,
    # и если ядро держит достаточно, считаем, что взял
    if sb >= HP_MIN_SB and hp != "on" and hps != "on":
        need = hpn or -(-int(sb * 1.05) // hpsz)
        if hps == "off" or hpt < need:
            out.append({"k": "hugepages", "sb": sb, "pt": _int(cf.get("pt")), "need": need, "have": hpt, "hp": hp})
    load = lv if isinstance(lv, dict) and lv.get("win") else {}
    wal_b = load.get("wal_b") or 0
    ck_t, ck_r = load.get("ck_t"), load.get("ck_r")
    # Чекпоинты по объему WAL: в цикле checkpoint_timeout пишется wal_b * ckt / 3600, а чекпоинт по объему
    # наступает на max_wal_size / (1 + checkpoint_completion_target). Внеплановые дают и CHECKPOINT,
    # pg_basebackup, CREATE DATABASE: поэтому от трех в час и только если WAL между чекпоинтами правда
    # подходит к порогу - при 48 МБ WAL в час гигабайтный max_wal_size ни при чем
    mws, ckt, cct = _int(cf.get("mws")), _int(cf.get("ckt")), _flt(cf.get("cct"), 0.9)
    per_ck = wal_b / max((ck_t or 0) + (ck_r or 0), 1)
    if ck_r is not None and ck_r >= 3 and ck_r >= (ck_t or 0) and ckt and per_ck >= mws / (1 + cct) * 0.5:
        rec = _pow2_gib(max(wal_b * ckt / 3600 * (1 + cct) * 1.25, mws * 2))
        out.append({"k": "max_wal_size", "cur": mws, "rec": rec, "wal": wal_b})
    wb, wbf = _int(cf.get("wb")), load.get("wbf") or 0
    if wbf >= WBF_ADVICE and 0 < wb < WB_GOOD:
        out.append({"k": "wal_buffers", "cur": wb, "n": wbf})
    # pglz самый медленный; lz4 в сборках PG15+ есть всегда. Выключенное сжатие - только если WAL много и
    # заметная часть его - полные копии страниц
    walc, vn = str(cf.get("walc") or ""), _int(cf.get("v"))
    if vn >= 150000 and walc in ("pglz", "on"):
        out.append({"k": "wal_compression", "cur": walc})
    elif vn >= 150000 and walc == "off" and wal_b >= GIB and load.get("wal_rec"):
        fpi = round((load.get("fpi") or 0) * 100 / load["wal_rec"])
        if fpi >= 5:
            out.append({"k": "wal_compression_off", "wal": wal_b, "fpi": fpi})
    # docker ждет 10 секунд и убивает: остановка с большими буферами не успевает дописать чекпоинт, и
    # после каждого docker stop или перезагрузки база восстанавливается как после аварии. SIGTERM -
    # "умная" остановка, она ждет отключения всех клиентов, а пул соединений не отключится сам
    sto, sig = _int(cf.get("stop_to")), str(cf.get("stop_sig") or "")
    if x.get("container") and ((sb >= STOP_MIN_SB and sto < STOP_OK) or sig == "SIGTERM"):
        out.append({"k": "stop_timeout", "to": 10 if sto < 0 else sto, "sb": sb, "sig": sig})
    temp = load.get("temp_b") or 0
    if temp >= TEMP_ADVICE:
        out.append({"k": "temp", "temp": temp, "wm": _int(cf.get("wm")), "mc": _int(cf.get("mc"))})
    # Остальное - про планировщик и память; на маленькой базе или при нескольких инстансах на машине
    # (каждому своя доля) советовать проценты от памяти нельзя
    rpc = _flt(cf.get("rpc"))
    if ssd and rpc >= 3.5 and db_size >= GIB:
        out.append({"k": "random_page_cost", "v": rpc})
    if pg_count == 1 and db_size >= 4 * GIB and mem >= 16 * GIB and 0 < _int(cf.get("ecs")) <= 4 * GIB:
        out.append({"k": "effective_cache_size", "ecs": _int(cf.get("ecs")), "mem": mem})
    if pg_count == 1 and mem >= 8 * GIB and sb:
        if sb < mem * 0.1 and db_size >= 4 * sb:
            out.append({"k": "shared_buffers_low", "sb": sb, "mem": mem, "lim": bool(cf.get("mem_lim"))})
        elif sb > mem * 0.5:
            out.append({"k": "shared_buffers_high", "sb": sb, "mem": mem, "lim": bool(cf.get("mem_lim"))})
    return out


def view(rep: dict, now: datetime) -> list[dict] | None:
    """Для раздела "Сервисы": по движку роль, отставание, самая долгая транзакция и проблемы"""
    items = block(rep, now)
    if items is None:
        return None
    probs = problems(rep, now)
    pg_count = sum(1 for x in items if x.get("engine") == "pg")
    ssd = _ssd(rep)
    out = []
    for x in items:
        k = f"{x.get('engine')}:{x.get('container') or ''}"
        mine = [{"level": lv, "text": t} for lv, t, key in probs if key.startswith(k + ":")]
        lv = load_view(x)
        adv = advice(x, lv, pg_count=pg_count, ssd=ssd, db_size=_db_size(rep, str(x.get("container") or ""))) \
            if x.get("engine") == "pg" else []
        out.append({"engine": x.get("engine"), "container": x.get("container") or "", "role": x.get("role") or "",
                    "replicas": _int(x.get("replicas")), "lag": _int(x.get("lag")), "xact": _int(x.get("xact")),
                    "xid": _int(x.get("xid")), "problems": mine, "load": lv, "advice": adv})
    return out
