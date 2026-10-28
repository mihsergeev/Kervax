"""Здоровье баз (helper dbstat-setup 0.5): репликация, долгие транзакции, блокировки, wraparound,
таблицы, которые давно не вакуумились. Блок extras["db-health"], helper обновляет его раз в
15 минут. С 0.8 у PostgreSQL еще и "что грузит базу" за последний час (load_view), с 0.9 настройки
и советы по ним (advice), с 0.10 здоровье ClickHouse и Redis (_ch_problems, _rd_problems), с 0.11 у MySQL
и MariaDB нагрузка, настройки и советы (my_load_view, my_advice), очередь очистки InnoDB и простаивающая
транзакция (_my_problems).

Почему это отдельно от инвентаря: база может быть жива и отвечать, а реплика уже час не
получает WAL, незакрытая транзакция держит блокировки и не дает VACUUM чистить, а возраст XID
ползет к остановке на wraparound. Снаружи этого не видно, пока не станет поздно.
"""
from __future__ import annotations

import posixpath
import re
import shlex
from datetime import datetime

from app import tg_fmt
from app.compose_cmd import verdict

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
_DUMP_APPS = ("pg_dump", "pg_dumpall", "pg_basebackup", "mysqldump")
DUMP_XACT_WARN = 12 * 3600
STALE = 3600  # блок старше часа - helper не работает, не судим по старому
# ClickHouse: куски в одной партиции. С parts_to_delay_insert вставки замедляются, с
# parts_to_throw_insert отклоняются (TOO_MANY_PARTS); старые версии - 150 и 300, новые - 1000 и 3000
CH_THROW_NEAR = 0.8  # от 80% порога отказа - уже авария: слияния не успевают за вставками
MUT_WARN = 6 * 3600  # мутация без ошибки идет 6 часов - скорее всего, встала
CH_QUEUE_WARN = 3600  # задача репликации висит час с ошибкой
RD_MEM_WARN = 0.9  # Redis с noeviction: от 90% maxmemory - предупреждение, от 98% запись отклоняется
RD_MEM_CRIT = 0.98
RD_LINK_DOWN = 120  # реплика без связи с основной 2 минуты
# Очередь очистки InnoDB (history list length): старые версии строк, которые нельзя удалить, пока жива
# транзакция старше них. Обычно тысячи; с миллиона каждый запрос пробирается через версии, растут undo
HIST_WARN = 1_000_000
_ENG = {"pg": "PostgreSQL", "mysql": "MySQL", "clickhouse": "ClickHouse", "redis": "Redis"}


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


def _mins(sec: int) -> str:
    return f"{max(round(sec / 60), 1)} мин"


def _gib(n: int) -> str:
    return f"{n / (1 << 30):.1f} ГБ" if n >= 1 << 30 else f"{round(n / (1 << 20))} МБ"


def _ch_problems(x: dict, name: str, k: str) -> list[tuple[int, str, str]]:
    """ClickHouse (dbstat-setup 0.10): только то, что ломает запись или стоит"""
    out: list[tuple[int, str, str]] = []
    p = x.get("parts") if isinstance(x.get("parts"), dict) else {}
    mx, dl, th = _int(p.get("max")), _int(p.get("delay")), _int(p.get("throw"))
    where = f"{p.get('tbl') or '?'} (партиция {p.get('part') or '?'})"
    if th and mx >= th * CH_THROW_NEAR:
        out.append((2, f"{name}: в {where} {mx} кусков, при {th} вставки начнут отклоняться - слияния не успевают "
                       "за вставками", k + ":parts"))
    elif dl and mx >= dl:
        out.append((1, f"{name}: в {where} {mx} кусков, с {dl} вставки замедляются - слияния не успевают",
                    k + ":parts"))
    ins = x.get("ins") if isinstance(x.get("ins"), dict) else {}
    win = _int(ins.get("win"))
    if win and _int(ins.get("rej")) > 0:
        out.append((2, f"{name}: за {_mins(win)} отклонено вставок: {_int(ins.get('rej'))} - слишком много кусков "
                       "в партиции (TOO_MANY_PARTS)", k + ":rej"))
    elif win and _int(ins.get("del")) > 0:
        out.append((1, f"{name}: за {_mins(win)} вставки замедлялись {_int(ins.get('del'))} раз - кусков больше "
                       "parts_to_delay_insert", k + ":delay"))
    rp = x.get("rep") if isinstance(x.get("rep"), dict) else {}
    if _int(rp.get("ro")) > 0:
        out.append((2, f"{name}: реплицируемая таблица {rp.get('ro_tbl') or '?'} в режиме только чтения"
                       + (f" (еще таких: {_int(rp.get('ro')) - 1})" if _int(rp.get("ro")) > 1 else "")
                       + " - нет связи с Keeper или кончилось место, запись в нее отклоняется", k + ":ro"))
    rl = _int(rp.get("delay"))
    if rl >= LAG_WARN:
        out.append((2 if rl >= LAG_CRIT else 1, f"{name}: реплика отстает на {_dur(rl)}", k + ":lag"))
    if _int(rp.get("q_age")) >= CH_QUEUE_WARN and str(rp.get("q_err") or "").strip():
        out.append((1, f"{name}: очередь репликации стоит, самой старой задаче {_dur(_int(rp.get('q_age')))}: "
                       f"{str(rp.get('q_err'))[:160]}", k + ":queue"))
    mu = x.get("mut") if isinstance(x.get("mut"), dict) else {}
    err = str(mu.get("err") or "").strip()
    if _int(mu.get("n")) and err:
        out.append((1, f"{name}: мутация в {mu.get('tbl') or '?'} не выполняется: {err[:160]}", k + ":mut"))
    elif _int(mu.get("n")) and _int(mu.get("age")) >= MUT_WARN:
        out.append((1, f"{name}: мутация в {mu.get('tbl') or '?'} идет {_dur(_int(mu.get('age')))}", k + ":mut"))
    return out


def _rd_problems(x: dict, name: str, k: str) -> list[tuple[int, str, str]]:
    """Redis (dbstat-setup 0.10): отказ в записи, потеря данных при перезапуске, отвалившаяся реплика"""
    out: list[tuple[int, str, str]] = []
    if x.get("rdb") == "err":
        if x.get("stop_writes") == "no":
            out.append((1, f"{name}: не может сохранить снимок на диск - при перезапуске пропадет все, что "
                           "записано после последнего снимка", k + ":rdb"))
        else:
            out.append((2, f"{name}: не может сохранить снимок на диск и отклоняет запись (MISCONF) - проверьте "
                           "место и права на каталог данных", k + ":rdb"))
    if _int(x.get("aof")) and x.get("aof_status") == "err":
        out.append((2, f"{name}: не может писать AOF на диск и отклоняет запись", k + ":aof"))
    mem, mx = _int(x.get("mem")), _int(x.get("maxmem"))
    if mx and x.get("policy") == "noeviction":
        pct = mem / mx
        if pct >= RD_MEM_CRIT:
            out.append((2, f"{name}: память кончилась ({_gib(mem)} из {_gib(mx)}), политика noeviction - запись "
                           "отклоняется (OOM)", k + ":mem"))
        elif pct >= RD_MEM_WARN:
            out.append((1, f"{name}: занято {round(pct * 100)}% maxmemory ({_gib(mem)} из {_gib(mx)}), при 100% "
                           "запись начнет отклоняться (noeviction)", k + ":mem"))
    if x.get("role") == "replica" and x.get("link") == "down" and _int(x.get("down")) >= RD_LINK_DOWN:
        out.append((2, f"{name}: реплика потеряла связь с основной {_dur(_int(x.get('down')))} назад", k + ":link"))
    win, rj = _int(x.get("win")), _int(x.get("rejected"))
    if win and rj > 0:
        out.append((1, f"{name}: за {_mins(win)} отклонено подключений: {rj} - упор в maxclients", k + ":conn"))
    return out


def _my_problems(x: dict, name: str, k: str) -> list[tuple[int, str, str]]:
    """MySQL/MariaDB (dbstat-setup 0.11). Долгая транзакция, простаивающая транзакция и очередь очистки
    чаще всего одна беда: транзакцию забыли закрыть, и из-за нее растет очередь. Про нее одна строка"""
    out: list[tuple[int, str, str]] = []
    xa, idle, sleep, hist = _int(x.get("xact")), _int(x.get("idle_xact")), _int(x.get("idle_sleep")), _int(x.get("hist"))
    who = _who(str(x.get("xact_db") or ""), str(x.get("xact_app") or ""))
    dump = str(x.get("xact_app") or "").startswith(_DUMP_APPS) and xa < DUMP_XACT_WARN
    tail = f"; очередь очистки InnoDB уже {hist / 1e6:.1f} млн версий строк" if hist >= HIST_WARN else ""
    told = False
    # Транзакция открыта, а соединение ничего не делает (Sleep): приложение забыло COMMIT или держит в пуле
    # соединение с начатой транзакцией. Держит свои блокировки строк и не дает InnoDB удалять старые версии
    if sleep >= IDLE_WARN:
        iw = _who(str(x.get("idle_db") or ""), str(x.get("idle_app") or ""))
        out.append((2 if idle >= XACT_CRIT else 1,
                    f"{name}: транзакция открыта {_dur(idle)} и простаивает {_dur(sleep)}" + (f" ({iw})" if iw else "")
                    + ": держит блокировки строк и не дает InnoDB удалять старые версии" + tail, k + ":idle"))
        told = True
    # самая старая - другая, работающая (идет дольше простаивающей) - про нее своя строка
    if xa >= XACT_WARN and not dump and not (told and idle >= xa - 60):
        out.append((2 if xa >= XACT_CRIT else 1, f"{name}: транзакция идет {_dur(xa)}" + (f" ({who})" if who else "")
                    + ("" if told else tail), k + ":xact"))
        told = True
    if hist >= HIST_WARN and not told:
        txt = (f"{name}: очередь очистки InnoDB {hist / 1e6:.1f} млн версий строк - старые версии не удаляются, "
               "запросы пробираются через них и замедляются")
        if xa >= 600:
            txt += f"; самая долгая транзакция идет {_dur(xa)}" + (f" ({who})" if who else "") + " - держит, скорее всего, она"
        out.append((1, txt, k + ":hist"))
    lw, lwt = _int(x.get("lock_waits")), _int(x.get("lock_wait"))
    if lw and lwt >= LOCK_WARN:
        txt = f"{name}: ждут блокировку {lw} запр., дольше всех {_dur(lwt)}"
        if 60 <= sleep < IDLE_WARN:
            txt += f", а транзакция простаивает {_dur(sleep)} - держит, скорее всего, она"
        out.append((1, txt, k + ":lock"))
    # Отказы по max_connections: приложение получает "Too many connections", это видно только в его логе
    ld = x.get("load") if isinstance(x.get("load"), dict) else {}
    win, rf = _int(ld.get("win")), _int(ld.get("connection_errors_max_connections"))
    if win > 0 and rf > 0:
        mc = (x.get("cfg") or {}).get("max_connections") if isinstance(x.get("cfg"), dict) else None
        out.append((1, f"{name}: за {_mins(win)} отказано в подключении: {rf} - упор в max_connections"
                       + (f" ({mc})" if mc else ""), k + ":conn"))
    return out


def problems(rep: dict, now: datetime) -> list[tuple[int, str, str]]:
    """(уровень, текст, ключ): ключ - что именно сломалось, чтобы новая беда дала свой алерт"""
    items = block(rep, now)
    out: list[tuple[int, str, str]] = []
    for x in items or []:
        eng = _ENG.get(str(x.get("engine")), "СУБД")
        where = str(x.get("container") or "")
        name = f"{eng} {where}" if where else f"{eng} на хосте"
        k = f"{x.get('engine')}:{where}"
        if x.get("engine") == "clickhouse":
            out.extend(_ch_problems(x, name, k))
            continue
        if x.get("engine") == "redis":
            out.extend(_rd_problems(x, name, k))
            continue
        if x.get("engine") == "mysql":
            # MariaDB - так и называем: версия есть в настройках (0.11)
            cf = x.get("cfg") if isinstance(x.get("cfg"), dict) else {}
            if "mariadb" in str(cf.get("version") or "").lower():
                name = f"MariaDB {where}" if where else "MariaDB на хосте"
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
        if x.get("engine") == "mysql":
            out.extend(_my_problems(x, name, k))
            continue
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


# ---- короткий алерт в Telegram (формат A, app/tg_fmt.py) ----
# "Транзакция висит 8 ч / база app, клиент worker-a". Вход - список (уровень, текст, ключ), как его
# отдает collector.db_health_problems: вид беды - из ключа, имена и сроки - из текста этого же модуля
# (строки выше), без порогов и подсказок. Несколько бед - что про первую (худшую), остальные
# короткими названиями

# беда по виду из ключа коротким названием - для строки "Еще: ..."
_TAG = {"stream": "реплика не получает данные", "lag": "реплика отстает", "xact": "долгая транзакция",
        "idle": "висит транзакция", "lock": "ждут блокировку", "xid": "wraparound", "vac": "давно не было VACUUM",
        "hist": "очередь очистки InnoDB", "conn": "отказы в подключении", "parts": "много кусков",
        "rej": "отклоняются вставки", "delay": "замедляются вставки", "ro": "таблица только на чтение",
        "queue": "стоит очередь репликации", "mut": "мутация стоит", "rdb": "не сохраняется снимок",
        "aof": "не пишется AOF", "mem": "кончается память", "link": "реплика без связи"}
_DUR_RX = re.compile(r"(\d+) сут(?: (\d+) ч)?|(\d+) ч(?: (\d+) мин)?|(\d+) мин")


def _kind(key: str) -> tuple[str, str]:
    """Вид беды из ключа: "pg:app-postgres-1:idle" -> ("idle", ""), "pg::vac:app.t" -> ("vac", "app.t")"""
    kind = (str(key).split(":", 2) + ["", ""])[2]
    k, _, sub = kind.partition(":")
    return k, sub


def _secs(s: str) -> int:
    """Срок из текста _dur ("8 ч 25 мин", "2 сут 3 ч", "40 мин") обратно в секунды"""
    m = _DUR_RX.search(s or "")
    if not m:
        return 0
    d, dh, h, hm, mi = (int(g) if g else 0 for g in m.groups())
    return d * 86400 + (dh + h) * 3600 + (hm + mi) * 60


def _who_back(who: str) -> str:
    """Кто держит из текста _who ("база app, worker-a") -> "база app, клиент worker-a" """
    db, sep, app = str(who or "").partition(", ")
    if not db.startswith("база "):
        db, app = "", str(who or "")
    return ", ".join(p for p in (db, f"клиент {app}" if app else "") if p)


def _short_one(lvl: int, text: str, key: str) -> tuple[str, list[str]]:
    """(что случилось, конкретика) одной беды"""
    inst, _, body = str(text).partition(": ")
    eng, _, cont = inst.partition(" ")
    place = cont or "на хосте"
    kind, sub = _kind(key)

    def find(rx: str) -> re.Match | None:
        return re.search(rx, body)

    if kind == "stream":
        if "репликация стоит" in body:
            return f"Репликация {eng} стоит", [place]
        return f"Реплика {eng} не получает WAL", [place]
    if kind == "lag":
        m = find(r"отстает(?: от основной)? на (.+)$")
        sec = _secs(m.group(1)) if m else 0
        return (f"Реплика {eng} отстает на {tg_fmt.dur(sec)}" if sec else f"Реплика {eng} отстает"), [place]
    if kind in ("xact", "idle"):
        word = "идет" if kind == "xact" else "простаивает"
        m = find(rf"{word} (.+?)(?: \(([^()]*)\))?(?::|;|$)")
        sec = _secs(m.group(1)) if m else 0
        who = _who_back(m.group(2)) if m and m.group(2) else ""
        verb = "идет" if kind == "xact" else "висит"
        return (f"Транзакция {verb} {tg_fmt.dur(sec)}" if sec else f"Транзакция {verb}"), [who or inst]
    if kind == "lock":
        m = find(r"дольше всех (\d+ (?:сут|ч|мин)(?: \d+ (?:ч|мин))?)")
        sec = _secs(m.group(1)) if m else 0
        return (f"Запросы ждут блокировку {tg_fmt.dur(sec)}" if sec else "Запросы ждут блокировку"), [inst]
    if kind == "xid":
        m = find(r"базы (\S+)")
        return f"{eng} подходит к остановке записи", [f"база {m.group(1) if m else '?'}, wraparound"]
    if kind == "vac":
        m, dm = find(r"([\d.]+) млн мертвых строк"), find(r"VACUUM не было (\d+) дней")
        what = ("VACUUM ни разу не чистил таблицу" if "ни разу" in body else
                f"VACUUM не чистил таблицу {tg_fmt.days(int(dm.group(1)))}" if dm else "VACUUM давно не чистил таблицу")
        return what, [sub + (f", {m.group(1)} млн мертвых строк" if m else "")]
    if kind == "hist":
        return "Растет очередь очистки InnoDB", [inst]
    if kind == "conn":
        why = "упор в maxclients" if eng == "Redis" else "упор в max_connections"
        return f"{eng} отказывает в подключениях", [f"{place}, {why}"]
    if kind == "parts":
        m = find(r"в (\S+) \(партиция")
        what = "ClickHouse скоро начнет отклонять вставки" if lvl >= 2 else "ClickHouse замедляет вставки"
        return what, [f"таблица {m.group(1) if m else '?'}, слияния не успевают"]
    if kind == "rej":
        return "ClickHouse отклоняет вставки", [f"{place}, слишком много кусков"]
    if kind == "delay":
        return "ClickHouse замедляет вставки", [f"{place}, слишком много кусков"]
    if kind == "ro":
        m = find(r"таблица (\S+) в режиме")
        return "Таблица ClickHouse только на чтение", [f"{m.group(1) if m else '?'}, нет связи с Keeper или места"]
    if kind == "queue":
        m = find(r"самой старой задаче (.+?):")
        sec = _secs(m.group(1)) if m else 0
        return (f"Очередь репликации ClickHouse стоит {tg_fmt.dur(sec)}" if sec
                else "Очередь репликации ClickHouse стоит"), [place]
    if kind == "mut":
        m = find(r"мутация в (\S+) (?:не выполняется|идет (.+))$")
        tbl = f"таблица {m.group(1)}" if m else inst
        if m and m.group(2):
            return f"Мутация ClickHouse идет {tg_fmt.dur(_secs(m.group(2)))}", [tbl]
        return "Мутация ClickHouse не выполняется", [tbl]
    if kind == "rdb":
        if lvl >= 2:
            return "Redis отклоняет запись", [f"{place}, не сохраняется снимок на диск"]
        return "Redis не сохраняет снимок на диск", [f"{place}, при перезапуске данные пропадут"]
    if kind == "aof":
        return "Redis отклоняет запись", [f"{place}, не пишется AOF"]
    if kind == "mem":
        if lvl >= 2:
            return "У Redis кончилась память", [f"{place}, запись отклоняется"]
        m = find(r"занято (\d+)%")
        return (f"Redis заполнен на {m.group(1)}%" if m else "Redis почти заполнен"), [place]
    if kind == "link":
        return "Реплика Redis потеряла связь с основной", [place]
    # незнакомый вид: первая фраза после имени экземпляра
    if not body:
        return tg_fmt.split_detail(text)
    return tg_fmt.cap(re.split(r": | - | \(|; ", body)[0]), [inst]


def tg_short(dh: list[tuple[int, str, str]]) -> tuple[str, list[str]] | None:
    """Короткий алерт по списку (уровень, текст, ключ), как его отдает collector.db_health_problems:
    что - про первую беду (худшую), остальные короткими названиями"""
    if not dh:
        return None
    lvl, text, key = dh[0]
    what, facts = _short_one(lvl, text, key)
    rest = [_TAG.get(_kind(k)[0], "") for _l, _t, k in dh[1:]]
    rest = [r for r in rest if r]
    if rest:
        facts = facts[:1] + [tg_fmt.fact("Еще", tg_fmt.names(rest, 2))]
    return what, facts


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
    # 0 - в снимке часовой давности запросов еще не было: pg_stat_statements только что включили
    if "pbase" in ld:
        out["pbase"] = _int(ld.get("pbase"))
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


def _db_size(rep: dict, container: str, engine: str = "pg") -> int:
    for d in (rep or {}).get("db_stats") or []:
        if isinstance(d, dict) and d.get("engine") == engine and (d.get("container") or "") == container:
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


# Как включить pg_stat_statements (dbstat-setup 0.14): шаги под этот экземпляр, после которых плашка в
# "Что грузит базу" уходит сама. Как у пунктов обновлений ОС: тексты шагов во фронте по id, здесь
# команды. Расширение helper ищет по базам начиная с postgres, а статистика в нем общая на весь
# сервер - одной базы достаточно. shared_preload_libraries из команды контейнера (-c в command compose)
# ALTER SYSTEM не перебивает, тогда правится compose. Список дописывается к текущему, каждая библиотека
# своей строкой в кавычках: 'a,b' одной строкой PostgreSQL примет за одну библиотеку с запятой в имени
# и не запустится. CREATE EXTENSION - первым: оно работает и без загруженной библиотеки и заодно
# проверяет, что она есть в сборке (иначе после перезапуска база не поднимется)
PGSS = "pg_stat_statements"
_SPL = "shared_preload_libraries"
_LIB_RX = re.compile(r"^[A-Za-z0-9_.-]+$")
_SPL_RAW_RX = re.compile(r'^[A-Za-z0-9_.," -]+$')
RESTART_WAIT = 120  # docker restart -t: дать базе дописать чекпоинт, обычно это секунды


def _libs(spl: str) -> list[str] | None:
    """Значение shared_preload_libraries -> библиотеки; None - имя, которое в команду не подставить
    ($libdir/..., кавычки внутри)"""
    out: list[str] = []
    for p in spl.split(","):
        p = p.strip()
        if len(p) > 1 and p[0] == p[-1] == '"':
            p = p[1:-1]
        if not p:
            continue
        if not _LIB_RX.match(p):
            return None
        out.append(p)
    return out


def _spl_sql(libs: list[str]) -> str:
    if not libs:
        return f"ALTER SYSTEM RESET {_SPL}"
    return f"ALTER SYSTEM SET {_SPL} = " + ", ".join(f"'{x}'" for x in libs)


def _pod(c: str, cf: dict) -> tuple[str, str] | None:
    """(namespace, под) базы в поде kubernetes (dbstat-setup 0.17: rt=pod и имя ns/под); None - не под.
    rt=pod без "/" в имени - docker-контейнер с образом под другим именем, к нему docker exec"""
    if cf.get("rt") != "pod" or "/" not in c:
        return None
    ns, _, pod = c.partition("/")
    return (ns, pod) if ns and pod else None


def _psql(c: str, role: str, db: str, *sql: str, pod: tuple[str, str] | None = None, kc: str = "") -> str:
    """psql тем же путем, каким в базу ходит helper: в контейнере под ролью, под которой он вошел (роль
    postgres - системным пользователем postgres), на хосте - от postgres через runuser: sudo -u postgres
    просит пароль, если sudo разрешен только как root. В поде - kubectl exec в контейнер базы (helper сам
    заходит через nsenter, у человека на ноде есть kubectl)"""
    args = f"-d {shlex.quote(db)} " + " ".join(f'-c "{s}"' for s in sql)
    if pod:
        q = shlex.quote
        return (f"kubectl -n {q(pod[0])} exec {q(pod[1])}" + (f" -c {q(kc)}" if kc else "")
                + f" -- psql -U {q(role or 'postgres')} {args}")
    if not c:
        return f"sudo runuser -u postgres -- psql {args}"
    qc = shlex.quote(c)
    if role == "postgres":
        return f"sudo docker exec -u postgres {qc} psql {args}"
    if role:
        return f"sudo docker exec {qc} psql -U {shlex.quote(role)} {args}"
    return f'sudo docker exec {qc} psql -U "$(sudo docker exec {qc} printenv POSTGRES_USER)" {args}'


def _compose(cf: dict) -> tuple[str, list[str], str] | None:
    """(cd в каталог проекта, файлы относительно него, docker compose ... up) по меткам контейнера"""
    d, proj, svc = str(cf.get("cdir") or ""), str(cf.get("cproj") or ""), str(cf.get("csvc") or "")
    files = [f for f in str(cf.get("cfiles") or "").split(",") if f]
    if not (d and proj and svc and files):
        return None
    q = shlex.quote
    names = [posixpath.basename(f) if posixpath.dirname(f) == d.rstrip("/") else f for f in files]
    up = (f"sudo docker compose -p {q(proj)} " + " ".join(f"-f {q(n)}" for n in names)
          + f" up -d --no-deps -t {RESTART_WAIT} {q(svc)}")
    return f"cd {q(d)}", names, up


def pgss_fix(x: dict, rep: dict) -> list[dict]:
    """Шаги [{"id", "cmd"?, "arg"?, "alt"?, "undo"?}], чтобы включить pg_stat_statements; [] - включен.
    Без данных helper'а 0.14 (откуда задан список и что в нем) первым шагом - посмотреть самому"""
    ld = x.get("load") if isinstance(x.get("load"), dict) else {}
    pss = str(ld.get("pss") or "")
    if x.get("engine") != "pg" or pss not in ("off", "noext"):
        return []
    cf = x.get("cfg") if isinstance(x.get("cfg"), dict) else {}
    c = str(x.get("container") or "")
    inv = next((d for d in (rep or {}).get("db_stats") or [] if isinstance(d, dict) and d.get("engine") == "pg"
                and (d.get("container") or "") == c), {})
    names = [str(d.get("name") or "") for d in inv.get("dbs") or [] if isinstance(d, dict)]
    db = "postgres" if "postgres" in names or not names else names[0]
    role = str(cf.get("role") or "")
    if not role:
        # helper до 0.14: вошел он под postgres или под единственной ролью с правом входа
        users = [str(u) for u in inv.get("users") or []]
        role = "postgres" if "postgres" in users else users[0] if len(users) == 1 else ""

    pod = _pod(c, cf)

    def sql(*s: str) -> str:
        return _psql(c, role, db, *s, pod=pod, kc=str(cf.get("kc") or ""))

    create = {"id": "pgss_create", "arg": db, "cmd": sql(f"CREATE EXTENSION IF NOT EXISTS {PGSS}")}
    check = {"id": "pgss_check", "cmd": sql(f"SHOW {_SPL}", f"SELECT count(*) FROM {PGSS}")}
    if pss == "noext":
        return [create, check]
    q = shlex.quote
    if pod:
        return _pgss_pod(cf, pod, sql, create, check)
    unit = str(cf.get("unit") or "")
    restart = ({"id": "pgss_restart", "cmd": f"sudo docker restart -t {RESTART_WAIT} {q(c)}"} if c else
               {"id": "pgss_restart_host", "arg": unit, "cmd": f"sudo systemctl restart {q(unit or 'postgresql')}"})
    drop = sql(f"DROP EXTENSION IF EXISTS {PGSS}")
    spl, src = str(cf.get("spl") or ""), str(cf.get("spl_src") or "")
    libs = _libs(spl) if src and cf.get("su") == "on" else None
    if libs is None:
        # не знаем, что уже загружено и откуда: посмотреть, дописать руками
        look = sql(f"SELECT setting, source FROM pg_settings WHERE name = '{_SPL}'")
        if c:
            look += ("\nsudo docker inspect -f '{{json .Config.Cmd}} {{index .Config.Labels \"com.docker.compose.project.working_dir\"}} "
                     "{{index .Config.Labels \"com.docker.compose.project\"}} {{index .Config.Labels \"com.docker.compose.service\"}}' " + q(c))
        steps = [{"id": "pgss_look", "arg": "docker" if c else "", "cmd": look}, create,
                 {"id": "pgss_alter_hand", "cmd": sql(_spl_sql([PGSS]))}, restart]
        if c:
            steps.append({"id": "pgss_compose_hand", "alt": True, "cmd": "cd <working_dir>\nsudo docker compose -p <project> "
                          f"up -d --no-deps -t {RESTART_WAIT} <service>"})
        return steps + [check, {"id": "pgss_undo_hand", "undo": True,
                                "cmd": sql(f"DROP EXTENSION IF EXISTS {PGSS}", _spl_sql([])) + "\n" + restart["cmd"]}]
    new = [*libs, PGSS] if PGSS not in libs else libs
    if src != "command line":
        return [create, {"id": "pgss_alter", "arg": ", ".join(libs), "cmd": sql(_spl_sql(new))}, restart, check,
                {"id": "pgss_undo", "undo": True, "arg": ", ".join(libs),
                 "cmd": sql(f"DROP EXTENSION IF EXISTS {PGSS}", _spl_sql(libs)) + "\n" + restart["cmd"]}]
    # задан в команде запуска: ALTER SYSTEM его не перебьет
    cmp = _compose(cf) if c else None
    if cmp is None:
        look = (f"sudo docker inspect -f '{{{{json .Config.Cmd}}}}' {q(c)}" if c else f"systemctl cat {q(unit or 'postgresql')}")
        return [create, {"id": "pgss_cmdline", "arg": ",".join(new), "cmd": look}, *([] if c else [restart]), check]
    cd, files, up = cmp
    svc = str(cf.get("csvc") or "")
    if len(files) == 1 and libs and _SPL_RAW_RX.match(spl):
        f, bak = q(files[0]), q(files[0] + ".bak-kervax")
        rx = spl.replace(".", "\\.")
        edit = {"id": "pgss_compose", "arg": svc, "cmd": "\n".join([
            cd, f"sudo cp -p {f} {bak}",
            f"sudo sed -i -E 's/({_SPL}=[^A-Za-z0-9_]?{rx})([^A-Za-z0-9_,]|$)/\\1,{PGSS}\\2/' {f}", f"diff {bak} {f}",
            # sed, не нашедший строку, молчит: без этой строки перезапускали бы с прежним списком
            verdict(f"grep -qE '{_SPL}=[^[:space:]]*{PGSS}' {f}", "pg_stat_statements добавлен",
                    "НЕ ДОБАВЛЕНО: строка shared_preload_libraries другая, поправьте руками")])}
        undo = "\n".join([drop, cd, f"sudo cp -p {bak} {f}", up])
    else:
        edit = {"id": "pgss_compose_grep", "arg": ",".join(new),
                "cmd": "\n".join([cd, f"grep -n {_SPL} " + " ".join(q(n) for n in files)])}
        undo = "\n".join([drop, cd, up])
    return [create, edit, {"id": "pgss_compose_up", "cmd": "\n".join([cd, up])}, check,
            {"id": "pgss_undo_compose", "undo": True, "cmd": undo}]


def _pgss_pod(cf: dict, pod: tuple[str, str], sql, create: dict, check: dict) -> list[dict]:
    """Шаги для базы в поде. ALTER SYSTEM пишет postgresql.auto.conf в каталог данных на томе, он
    переживает пересоздание пода; перезапуск - удалить под, контроллер (StatefulSet, Deployment)
    создаст его заново. Параметр в args контейнера ALTER SYSTEM не перебьет: правится манифест или
    values чарта, панель показывает текущие args"""
    q = shlex.quote
    ns, name = pod
    restart = {"id": "pgss_restart_pod", "arg": f"{ns}/{name}", "cmd": f"kubectl -n {q(ns)} delete pod {q(name)}"}
    args = (f"kubectl -n {q(ns)} get pod {q(name)} -o jsonpath='{{range .spec.containers[*]}}"
            '{.name}{": "}{.command} {.args}{"\\n"}{end}\'')
    spl, src = str(cf.get("spl") or ""), str(cf.get("spl_src") or "")
    libs = _libs(spl) if src and cf.get("su") == "on" else None
    if libs is None:
        look = sql(f"SELECT setting, source FROM pg_settings WHERE name = '{_SPL}'") + "\n" + args
        return [{"id": "pgss_look", "arg": "pod", "cmd": look}, create,
                {"id": "pgss_alter_hand", "cmd": sql(_spl_sql([PGSS]))}, restart, check,
                {"id": "pgss_undo_hand", "undo": True,
                 "cmd": sql(f"DROP EXTENSION IF EXISTS {PGSS}", _spl_sql([])) + "\n" + restart["cmd"]}]
    new = [*libs, PGSS] if PGSS not in libs else libs
    if src == "command line":
        return [create, {"id": "pgss_cmdline_pod", "arg": ",".join(new), "cmd": args}, check]
    return [create, {"id": "pgss_alter", "arg": ", ".join(libs), "cmd": sql(_spl_sql(new))}, restart, check,
            {"id": "pgss_undo", "undo": True, "arg": ", ".join(libs),
             "cmd": sql(f"DROP EXTENSION IF EXISTS {PGSS}", _spl_sql(libs)) + "\n" + restart["cmd"]}]


# Что грузит MySQL/MariaDB (dbstat-setup 0.11): разница счетчиков SHOW GLOBAL STATUS со снимком примерно
# часовой давности и самые долгие запросы из performance_schema. Тоже без алертов, кроме отказов в
# подключении (они в _my_problems)
_MY_KEYS = {"q": ("questions",), "sel": ("com_select",), "wr": ("com_insert", "com_update", "com_delete", "com_replace"),
            "slow": ("slow_queries",), "tmp": ("created_tmp_tables",), "tmp_disk": ("created_tmp_disk_tables",),
            "req": ("innodb_buffer_pool_read_requests",), "data_w": ("innodb_data_written",),
            "redo": ("innodb_os_log_written",), "log_waits": ("innodb_log_waits",),
            "lock_waits": ("innodb_row_lock_waits",), "lock_ms": ("innodb_row_lock_time",),
            "deadlocks": ("innodb_deadlocks", "im_deadlocks"), "aborted": ("aborted_connects",),
            "refused": ("connection_errors_max_connections",), "full_join": ("select_full_join",),
            "sort_merge": ("sort_merge_passes",)}


def my_load_view(x: dict) -> dict | None:
    """Счетчики MySQL в пересчете на час (None - эта версия их не считает), чтения с диска в байтах и
    доля попаданий в буфер, самые долгие запросы с долей во времени всех запросов"""
    ld = x.get("load")
    if not isinstance(ld, dict):
        return None
    win = _int(ld.get("win"))
    out: dict = {"win": max(win, 0), "ps": str(ld.get("ps") or ""), "cut": bool(ld.get("cut")), "top": [],
                 "run": _int(ld.get("run")), "max_used": _int(ld.get("max_used"))}
    if win <= 0:
        return out
    k = 3600 / win
    for key, src in _MY_KEYS.items():
        # дедлоки: у MariaDB счетчик статуса, у MySQL - innodb_metrics; что есть, то и берем
        vals = [_int(ld.get(f)) for f in src if f in ld]
        if key == "deadlocks":
            vals = vals[:1]
        out[key] = round(sum(vals) * k) if vals else None
    page = _int(ld.get("page")) or 16384
    reads = _int(ld.get("innodb_buffer_pool_reads")) if "innodb_buffer_pool_reads" in ld else None
    out["rd"] = round(reads * page * k) if reads is not None else None
    req = _int(ld.get("innodb_buffer_pool_read_requests"))
    out["hit"] = round(100 - reads * 100 / req, 2) if reads is not None and req >= 1000 else None
    ms_all = max(_int(ld.get("ms_all")), 0)
    out["ms_all"] = round(ms_all * k)
    top: dict[tuple[str, str], dict] = {}
    for q in ld.get("top") or []:
        if not isinstance(q, dict):
            continue
        txt = str(q.get("q") or "")
        key2 = (str(q.get("db") or ""), txt or "#" + str(q.get("id") or ""))
        r = top.setdefault(key2, {"db": key2[0], "q": txt, "calls": 0, "ms": 0, "rows": 0, "tmp": 0, "noidx": 0})
        for f in ("calls", "ms", "rows", "tmp", "noidx"):
            r[f] += max(_int(q.get(f)), 0)
    rows = sorted(top.values(), key=lambda r: -r["ms"])
    for r in rows:
        r["share"] = round(r["ms"] * 100 / ms_all) if ms_all else 0
        r["avg_ms"] = round(r["ms"] / r["calls"], 1) if r["calls"] else 0
        r["rows_call"] = round(r["rows"] / r["calls"]) if r["calls"] else 0
        for f in ("calls", "ms", "rows", "tmp", "noidx"):
            r[f] = round(r[f] * k)
    out["top"] = rows
    return out


# Советы по настройкам MySQL/MariaDB: те же правила, что у PostgreSQL - только то, что прямо следует из
# цифр, текст рисует фронт по ключу k
REDO_ADVICE = GIB  # redo в час, с которого есть о чем говорить
LOG_WAITS_ADVICE = 100
TMP_DISK_ADVICE = 1000  # временных таблиц на диске в час


def _my_ver(v: str) -> tuple[bool, tuple[int, int, int]]:
    """("10.11.6-MariaDB-log") -> (MariaDB?, (10, 11, 6))"""
    nums = []
    for part in str(v or "").split("-")[0].split(".")[:3]:
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return "mariadb" in str(v or "").lower(), (nums[0], nums[1], nums[2])


def my_advice(x: dict, lv: dict | None, *, my_count: int = 1, db_size: int = 0) -> list[dict]:
    cf = x.get("cfg")
    if not isinstance(cf, dict):
        return []
    out: list[dict] = []
    maria, ver = _my_ver(str(cf.get("version") or ""))
    bp, mem, lim = _int(cf.get("innodb_buffer_pool_size")), _int(cf.get("mem")), bool(cf.get("mem_lim"))
    load = lv if isinstance(lv, dict) and lv.get("win") else {}
    # Буфер InnoDB. Мал: данных намного больше буфера, и InnoDB заметно читает с диска. Только когда
    # MySQL на машине один - иначе неизвестно, какая доля памяти его
    rd, hit = load.get("rd") or 0, load.get("hit")
    if my_count == 1 and mem >= 4 * GIB and bp and db_size >= 2 * bp and bp < mem * 0.4 and             (rd >= GIB or (hit is not None and hit < 99 and (load.get("req") or 0) >= 1_000_000)):
        out.append({"k": "my_bp_low", "bp": bp, "mem": mem, "lim": lim, "data": db_size, "rd": rd})
    elif bp and mem and bp > mem * 0.8:
        out.append({"k": "my_bp_high", "bp": bp, "mem": mem, "lim": lim})
    # Redo. Когда журнал переписывается по кругу чаще раза в полчаса, InnoDB приходится сбрасывать
    # страницы раньше времени (агрессивный flush), запись на диск растет. Обычно журнала хватает на час
    redo = load.get("redo") or 0
    if "innodb_redo_log_capacity" in cf:
        cap, var, online = _int(cf.get("innodb_redo_log_capacity")), "innodb_redo_log_capacity", True
    elif maria and ver >= (10, 5, 0):
        cap, var, online = _int(cf.get("innodb_log_file_size")), "innodb_log_file_size", ver >= (10, 11, 0)
    else:
        cap = _int(cf.get("innodb_log_file_size")) * max(_int(cf.get("innodb_log_files_in_group")), 1)
        var, online = "innodb_log_file_size", False
    if redo >= REDO_ADVICE and cap and cap < redo / 2:
        out.append({"k": "my_redo", "cur": cap, "rec": _pow2_gib(redo), "redo": redo, "var": var, "online": online})
    lw = load.get("log_waits") or 0
    lb = _int(cf.get("innodb_log_buffer_size"))
    if lw >= LOG_WAITS_ADVICE and 0 < lb < 64 * MIB:
        out.append({"k": "my_log_buffer", "cur": lb, "n": lw, "online": not maria and ver >= (8, 0, 0)})
    td, ta = load.get("tmp_disk") or 0, load.get("tmp") or 0
    if td >= TMP_DISK_ADVICE and td >= ta * 0.1:
        out.append({"k": "my_tmp", "n": td, "all": ta, "tts": _int(cf.get("tmp_table_size")),
                    "mhts": _int(cf.get("max_heap_table_size"))})
    # Бинлоги без срока хранения копятся, пока не кончится диск
    exp_s = _int(cf.get("binlog_expire_logs_seconds"))
    exp_d = _flt(cf.get("expire_logs_days"))
    if cf.get("log_bin") == "ON" and not exp_s and not exp_d and _int(cf.get("binlog_b")) >= GIB:
        out.append({"k": "my_binlog", "b": _int(cf.get("binlog_b"))})
    # docker ждет 10 секунд: InnoDB не успевает сбросить грязные страницы большого буфера, и после
    # docker stop или перезагрузки база восстанавливается по redo как после аварии
    sto = _int(cf.get("stop_to"))
    if cf.get("rt") == "docker" and bp >= STOP_MIN_SB and sto < STOP_OK:
        out.append({"k": "my_stop_timeout", "to": 10 if sto < 0 else sto, "bp": bp})
    return out


# почему helper не опросил базу в поде (dbstat-setup 0.17): нет nsenter на ноде, нет клиента в образе,
# база не пустила, не ответила вовремя, прочее
FAIL_WHY = ("nsenter", "client", "auth", "timeout", "conn")


def fails(rep: dict, now: datetime) -> list[dict] | None:
    """Базы в подах, до которых helper не достучался: движок, под, почему и начало ошибки клиента"""
    if block(rep, now) is None:
        return None
    b = rep["extras"]["db-health"]
    out = []
    for x in b.get("fails") or []:
        if not isinstance(x, dict) or not x.get("container"):
            continue
        why = str(x.get("why") or "")
        out.append({"engine": str(x.get("engine") or ""), "container": str(x["container"]),
                    "why": why if why in FAIL_WHY else "conn", "err": str(x.get("err") or "").strip()[:200]})
    return out or None


def view(rep: dict, now: datetime) -> list[dict] | None:
    """Для раздела "Сервисы": по движку роль, отставание, самая долгая транзакция и проблемы"""
    items = block(rep, now)
    if items is None:
        return None
    probs = problems(rep, now)
    pg_count = sum(1 for x in items if x.get("engine") == "pg")
    my_count = sum(1 for x in items if x.get("engine") == "mysql")
    ssd = _ssd(rep)
    out = []
    for x in items:
        k = f"{x.get('engine')}:{x.get('container') or ''}"
        mine = [{"level": lv, "text": t} for lv, t, key in probs if key.startswith(k + ":")]
        cont = str(x.get("container") or "")
        if x.get("engine") == "mysql":
            lv = my_load_view(x)
            adv = my_advice(x, lv, my_count=my_count, db_size=_db_size(rep, cont, "mysql"))
        else:
            lv = load_view(x)
            adv = advice(x, lv, pg_count=pg_count, ssd=ssd, db_size=_db_size(rep, cont)) if x.get("engine") == "pg" else []
        o = {"engine": x.get("engine"), "container": x.get("container") or "", "role": x.get("role") or "",
             "replicas": _int(x.get("replicas")), "lag": _int(x.get("lag")), "xact": _int(x.get("xact")),
             "xid": _int(x.get("xid")), "problems": mine, "load": lv, "advice": adv}
        # у MySQL свои счетчики - отдельным полем, "load" остается за PostgreSQL
        if x.get("engine") == "mysql":
            o["load"], o["my"] = None, lv
        elif x.get("engine") == "pg":
            o["pgss_fix"] = pgss_fix(x, rep)
        # ClickHouse и Redis - факты для карточки как есть: куски, мутации, реплики, вставки, запросы;
        # память, снимок, вытеснение, тяжелые команды
        if x.get("engine") == "clickhouse":
            o["ch"] = {key: x.get(key) for key in ("parts", "mut", "rep", "detached", "merges", "merge_max", "ins",
                                                   "qlog", "qerr", "top", "cut")}
        elif x.get("engine") == "redis":
            o["rd"] = {key: x.get(key) for key in ("link", "down", "mem", "maxmem", "policy", "rdb", "rdb_changes",
                                                   "rdb_age", "aof", "aof_status", "stop_writes", "win", "evicted",
                                                   "rejected", "cmds")}
        out.append(o)
    return out
