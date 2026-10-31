"""PHP-FPM (fpm_pools, helper fpm-setup): пул подолгу упирается в pm.max_children или рвет запросы по
таймауту, мьют, "Что сломано", алерт, раздел."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import collector, fpm_pools as fp
from app.models import Server

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())


def _pool(where, pool="www", mx=5, sat=0, reached=0, tout=0, slow=0, **kw):
    p = {"where": where, "pool": pool, "listen": "9000", "pm": "dynamic", "max": mx, "children": 3, "conns": 0,
         "queue": 0, "sat": sat, "span": 86400, "reached": reached, "reached_d": reached * 10, "slow": slow,
         "slow_d": slow * 10, "tout": tout, "tout_d": tout * 10, "slowlog": 0, "terminate": 0, "status": 0}
    p.update(kw)
    return p


def _rep(pools=None, ts=TS):
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": TS,
            "extras": {"fpm": {"v": 1, "ts": ts, "skipped": [],
                               "pools": pools if pools is not None else [_pool("container shop-php")]}}}


def _srv(rep=None, mutes=None, state=None):
    return Server(id=1, name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_level_rules():
    # минута-другая очереди и редкие упоры - не беда
    assert fp.level(_pool("container a", sat=5, reached=10)) == 0
    # запросы ждали четверть часа из часа - предупреждение, три четверти - проблема
    assert fp.level(_pool("container a", sat=15)) == 1
    assert fp.level(_pool("container a", sat=45)) == 2
    # пул упирается в предел раз в минуту, хотя выборки очередь не застали
    assert fp.level(_pool("container a", reached=59)) == 0
    assert fp.level(_pool("container a", reached=60)) == 1
    # убитые по таймауту запросы - это 502 у пользователей
    assert fp.level(_pool("container a", tout=19)) == 0
    assert fp.level(_pool("container a", tout=20)) == 1
    assert fp.level(_pool("container a", tout=100)) == 2
    # медленные только показываются
    assert fp.level(_pool("container a", slow=5000)) == 0
    # после ушедшего алерта отбой при половине порогов
    assert fp.level(_pool("container a", sat=8)) == 0
    assert fp.level(_pool("container a", sat=8), held=True) == 1
    assert fp.level(_pool("container a", sat=7, reached=29, tout=9), held=True) == 0


def test_problems_text_mute_and_home():
    ex = "script '/app/index.php' (request: \"POST /api/pay?token=***\") execution timed out (30.01 sec), terminating"
    rep = _rep([_pool("container shop-php", sat=20, reached=80, tout=25, terminate=30, tout_last=ex),
                _pool("pod shop/web-0", pool="api", sat=2)])
    s = _srv(rep)
    p = fp.problems(s, NOW)
    assert [x["key"] for x in p] == ["контейнер shop-php|www"]
    assert p[0]["text"] == ("PHP-FPM контейнер shop-php, пул www: запросы ждали свободного воркера 20 мин из последнего"
                            " часа (воркеров 5), за час пул 80 раз уперся в pm.max_children = 5, запросов, убитых по"
                            " таймауту 30 с, за час: 25\n↳ " + ex)
    assert fp.why(_pool("container a", reached=62)) == "за час пул 62 раза уперся в pm.max_children = 5"
    assert fp.why(_pool("container a", tout=21)) == "запросов, убитых по таймауту, за час: 21"
    rows = [x for x in collector.server_problems(s, NOW) if x["kind"] == "fpm"]
    assert [r["level"] for r in rows] == [1] and rows[0]["mute"] == "fpm:контейнер shop-php|www"
    # заглушить один пул: насовсем и на время
    s.alert_mutes = ["fpm:контейнер shop-php|www"]
    assert fp.problems(s, NOW) == []
    s.alert_mutes = []
    s.alert_snoozes = {"fpm:контейнер shop-php|www": (NOW + timedelta(days=1)).isoformat()}
    assert fp.problems(s, NOW) == []
    v = fp.view(s, NOW)
    a, b = v["pools"]
    assert a["mute"] == {"until": (NOW + timedelta(days=1)).isoformat()} and a["level"] == 1 and a["tout_last"] == ex
    assert b["label"] == "под shop/web-0, пул api" and b["key"] == "под shop/web-0|api" and "level" not in b
    assert b["max"] == 5 and b["listen"] == "9000"
    # срок снуза вышел - снова в беде
    s.alert_snoozes = {"fpm:контейнер shop-php|www": (NOW - timedelta(minutes=1)).isoformat()}
    assert len(fp.problems(s, NOW)) == 1


def test_example_falls_back_to_slow():
    s = _srv(_rep([_pool("host", sat=50, slow_last="script '/x.php' (request: \"GET /x.php\") executing too slow (5.2 sec), logging")]))
    p = fp.problems(s, NOW)
    assert p[0]["level"] == 2 and p[0]["text"].endswith("\n↳ script '/x.php' (request: \"GET /x.php\") executing too slow (5.2 sec), logging")
    assert p[0]["label"] == "на хосте, пул www"


def test_stale_block_is_not_judged():
    s = _srv(_rep([_pool("container a", sat=60)], ts=TS - 3600))
    assert fp.problems(s, NOW) == [] and fp.view(s, NOW) is None


def test_held_comes_from_the_sent_alert():
    x = _pool("container a", sat=10)
    s = _srv(_rep([x]), state={"fpm_sig": ["контейнер a|www"]})
    assert [p["level"] for p in fp.problems(s, NOW)] == [1]
    assert fp.problems(s, NOW)[0]["why"] == "запросы ждали свободного воркера 10 мин из последнего часа (воркеров 5)"
    assert fp.problems(_srv(_rep([x])), NOW) == []


def test_snooze_accepts_a_pool_key():
    from app.schemas import AlertSnoozeIn
    k = "fpm:контейнер shop-php|www"
    assert AlertSnoozeIn(kind=k, hours=24).kind == k


def test_own_unit_does_not_alert():
    assert collector.own_helper_unit("kervax-fpm.service")


def test_alert_condition():
    rep = _rep([_pool("container a", sat=50), _pool("container b", tout=30)])
    lv, ctx = collector._server_conditions(_srv(rep), NOW)["fpm"]
    assert lv == 2 and ctx["sig"] == ["контейнер a|www", "контейнер b|www"]
    assert ctx["detail"].startswith("PHP-FPM контейнер a, пул www: запросы ждали свободного воркера 50 мин")
    assert collector._server_conditions(_srv(_rep()), NOW)["fpm"][0] == 0


def _kern_rep(pools=None, n=0, n_d=None):
    rep = _rep(pools)
    rep["extras"]["fpm"]["kern"] = {"n": n, "n_d": n if n_d is None else n_d, "ts": TS - 60,
                                    "last": "php-fpm[4242]: segfault at 0 ip 00007f sp 00007ffd error 4 in libphp.so"}
    return rep


def test_crash_level_and_text():
    # одно падение в день бывает от расширения; подряд - проблема
    assert fp.level(_pool("container a", crash=2)) == 0
    assert fp.level(_pool("container a", crash=3)) == 1
    assert fp.level(_pool("container a", crash=10)) == 2
    assert fp.level(_pool("container a", crash=2), held=True) == 1
    assert fp.level(_pool("container a", crash=1), held=True) == 0
    ex = "child 77 exited on signal 11 (SIGSEGV - core dumped) after 0.4 seconds from start"
    s = _srv(_rep([_pool("pod shop/web-0", crash=12, crash_d=40, crash_last=ex, tout_last="script x timed out")]))
    p = fp.problems(s, NOW)
    assert p[0]["level"] == 2 and p[0]["why"] == "воркеры падают: 12 за час"
    # в алерте не пример строки лога, а что это значит и что делать
    assert p[0]["text"] == "PHP-FPM под shop/web-0, пул www: воркеры падают: 12 за час\n↳ " + fp.CRASH_HINT
    row = fp.view(s, NOW)["pools"][0]
    assert row["crash"] == 12 and row["crash_d"] == 40 and row["crash_last"] == ex


def test_kernel_crashes():
    # падения php по журналу ядра: своя строка, когда пулы о них молчат
    s = _srv(_kern_rep([_pool("container a")], n=4, n_d=9))
    p = fp.problems(s, NOW)
    assert [(x["key"], x["level"]) for x in p] == [("ядро|php", 1)]
    assert p[0]["text"] == "PHP на ноде падает: 4 за час по журналу ядра (segfault)\n↳ " + fp.CRASH_HINT
    v = fp.view(s, NOW)["pools"]
    assert v[-1]["kern"] is True and v[-1]["label"] == "PHP на ноде (журнал ядра)" and v[-1]["crash_d"] == 9
    assert v[-1]["crash_last"].startswith("php-fpm[4242]: segfault at 0") and v[-1]["level"] == 1
    # те же падения уже видны у пула - второй строки в алерте нет
    s = _srv(_kern_rep([_pool("container a", crash=4)], n=4))
    assert [x["key"] for x in fp.problems(s, NOW)] == ["контейнер a|www"]
    # мало, мьют, за сутки ничего - нет ни алерта, ни строки
    assert fp.problems(_srv(_kern_rep(n=2)), NOW) == []
    assert fp.problems(_srv(_kern_rep(n=12), mutes=["fpm:ядро|php"]), NOW) == []
    assert fp.view(_srv(_kern_rep(n=12), mutes=["fpm:ядро|php"]), NOW)["pools"][-1]["mute"] == {"perm": True}
    assert all(not x.get("kern") for x in fp.view(_srv(_kern_rep(n=0)), NOW)["pools"])
    # отбой при половине порога после ушедшего алерта
    assert fp.problems(_srv(_kern_rep(n=2), state={"fpm_sig": ["ядро|php"]}), NOW)[0]["level"] == 1
    lv, ctx = collector._server_conditions(_srv(_kern_rep(n=11)), NOW)["fpm"]
    assert lv == 2 and ctx["sig"] == ["ядро|php"]


def test_helper_script():
    src = (Path(__file__).resolve().parents[2] / "agent" / "fpm-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.4 " in src and "KERVAX_SETUP_ALWAYS=1" in src and "\r" not in src
    assert 'fpm_master "$P" || continue' in src
    assert '[[ ${exe##*/} =~ ^php-fpm([0-9]+([.][0-9]+)*)?$ ]]' in src
    assert '[ -L "$cur" ] && return 0' in src
    assert '../usr/share/zoneinfo/*) f=$(in_root "$p" "/${t#../}")' in src
    assert 'os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW | CLOEXEC, dir_fd=fd)' in src
    assert 'os.open(parts[-1], os.O_RDONLY | NOFOLLOW | CLOEXEC, dir_fd=dfd)' in src
    assert 'pidfd = os.open(pid, os.O_RDONLY | DIRECTORY | NOFOLLOW | CLOEXEC, dir_fd=procfd)' in src
    assert 'os.open("root", os.O_RDONLY | DIRECTORY | CLOEXEC, dir_fd=pidfd)' in src
    assert 'os.path.join(PROC, pid, "root")' not in src
    assert 'safe_cat "$P" "$conf" > "$cf"' in src and 'safe_snap "$m" "$f" "$ff" "$CAP"' in src
    assert 'safe_zone "$p" /etc/localtime "$zf"' in src and 'os.readlink(parts[-1], dir_fd=dfd)' in src
    assert 'printf \'%s\\tnopython\\n\'' in src
    # падение - сигнал сбоя, а не убийство: 9 и 15 - таймаут, OOM и перезагрузка
    assert "exited on signal (4|6|7|8|11) " in src and "-e \\047exited on signal\\047" in src
    # журнал ядра по курсору, без курсора - с прошлого прогона; курсор из состояния - только своими символами
    assert '"--after-cursor=$kc"' in src and '"--since=@$kt"' in src and "*[!A-Za-z0-9=\\;]*) kc=\"\" ;;" in src
    assert "(php|lsphp)[A-Za-z0-9._-]*\\[[0-9]+\\]:? +(segfault at|general protection|trap )" in src
    # обратный слэш в строке лога удваивается так, что mawk его не теряет: иначе отчет не JSON
    assert '_jn = split(s, _jp, "\\\\")' in src
    # смещения больше 2 ГиБ: mawk пишет 7.1983e+09 в строке и режет %d на 2^31; strtonum в mawk нет
    assert 'sprintf("%.0f", localfrom + 1)' in src and 'function hex(s,' in src and "strtonum" not in src
    # include разворачивается на своем месте: [www] после include перекрывает пулы из include
    assert 'for f in "$d"/${inc##*/}; do [ -f "$f" ] && [ ! -L "$f" ] && { cat "$f"; echo; } >> "$TMPD/conf"; done' in src
    # путь из конфига контейнера не уводит за его корень и не попадает в команду как есть
    assert 'case "$path" in *..*|*[!A-Za-z0-9._/@+-]*|"") return 0' in src
    assert "elog !~ /\\.\\./" in src
    # конфиг с CRLF: имена секций без \r
    assert '{ gsub(/\\r/, "") }' in src


def test_rootio_never_follows_a_container_symlink(tmp_path):
    if os.name != "posix":
        pytest.skip("dir_fd + O_NOFOLLOW проверяется на Linux")
    rootio = re.search(r'cat > "\$TMPD/rootio.py" <<\'PY\'\n(.*?)\nPY\n', SRC, re.S).group(1)
    script = tmp_path / "rootio.py"
    script.write_text(rootio, encoding="utf-8")
    proc = tmp_path / "proc"
    root = proc / "101" / "root"
    outside = tmp_path / "host"
    root.mkdir(parents=True)
    outside.mkdir()
    (outside / "secret").write_text("HOST-SECRET", encoding="utf-8")
    (root / "etc").symlink_to(outside, target_is_directory=True)
    env = {**os.environ, "KERVAX_FPM_PROC_ROOT": str(proc)}
    bad = subprocess.run([sys.executable, script, "cat", "101", "/etc/secret", "4096"], env=env,
                         capture_output=True, text=True, timeout=15)
    assert bad.returncode != 0 and "HOST-SECRET" not in bad.stdout
    (root / "etc").unlink()
    (root / "etc").mkdir()
    (root / "etc" / "safe.conf").write_text("[www]", encoding="utf-8")
    good = subprocess.run([sys.executable, script, "cat", "101", "/etc/safe.conf", "4096"], env=env,
                          capture_output=True, text=True, timeout=15)
    assert good.returncode == 0 and good.stdout == "[www]"


def test_rootio_copies_only_a_container_zoneinfo_target(tmp_path):
    if os.name != "posix":
        pytest.skip("readlinkat + O_NOFOLLOW проверяется на Linux")
    rootio = re.search(r'cat > "\$TMPD/rootio.py" <<\'PY\'\n(.*?)\nPY\n', SRC, re.S).group(1)
    script = tmp_path / "rootio.py"
    script.write_text(rootio, encoding="utf-8")
    proc = tmp_path / "proc"
    root = proc / "101" / "root"
    host_zone = Path("/usr/share/zoneinfo/Europe/Moscow")
    if not host_zone.is_file():
        pytest.skip("нет системной зоны Europe/Moscow")
    zone = root / "usr/share/zoneinfo/Europe/Moscow"
    zone.parent.mkdir(parents=True)
    zone.write_bytes(host_zone.read_bytes())
    (root / "etc").mkdir()
    localtime = root / "etc/localtime"
    localtime.symlink_to("../usr/share/zoneinfo/Europe/Moscow")
    env = {**os.environ, "KERVAX_FPM_PROC_ROOT": str(proc)}
    copied = tmp_path / "copied.tz"
    ok = subprocess.run([sys.executable, script, "zone", "101", "/etc/localtime", str(copied), "1048576"],
                        env=env, capture_output=True, text=True, timeout=15)
    assert ok.returncode == 0 and copied.read_bytes() == host_zone.read_bytes()
    off = subprocess.run(["date", "+%z"], env={**os.environ, "TZ": f":{copied}"},
                         capture_output=True, text=True, timeout=15)
    assert off.returncode == 0 and off.stdout.strip() == "+0300"
    localtime.unlink()
    (root / "etc/shadow").write_text("HOST-SECRET", encoding="utf-8")
    localtime.symlink_to("/etc/shadow")
    copied.unlink()
    bad = subprocess.run([sys.executable, script, "zone", "101", "/etc/localtime", str(copied), "1048576"],
                         env=env, capture_output=True, text=True, timeout=15)
    assert bad.returncode != 0 and not copied.exists()
    localtime.unlink()
    localtime.write_bytes(host_zone.read_bytes())
    regular = subprocess.run([sys.executable, script, "zone", "101", "/etc/localtime", str(copied), "1048576"],
                             env=env, capture_output=True, text=True, timeout=15)
    assert regular.returncode == 0 and copied.read_bytes() == host_zone.read_bytes()


def test_last_reached_in_text():
    # время последнего упора - от времени отчета: старые строки видно сразу
    x = _pool("container shop-php", reached=80, reached_ts=TS - 300)
    s = _srv(_rep([x]))
    p = fp.problems(s, NOW)
    assert p[0]["why"] == "за час пул 80 раз уперся в pm.max_children = 5, последний раз 5 мин назад"
    assert p[0]["last"] == "5 мин назад"
    assert fp.tg_short(p)[1][0] == "Пул: www в shop-php, заняты все 5 воркеров, последний упор 5 мин назад"
    row = fp.view(s, NOW)["pools"][0]
    assert row["reached_ts"] == TS - 300 and row["why"].endswith("последний раз 5 мин назад")
    assert fp.last_reached(_pool("a", reached_ts=TS - 6 * 86400), TS) == "6 дн назад"
    assert fp.last_reached(_pool("a", reached_ts=TS - 7200), TS) == "2 ч назад"
    # helper до 0.3 времени не пишет - текст прежний
    assert fp.why(_pool("container a", reached=62)) == "за час пул 62 раза уперся в pm.max_children = 5"


# ---- helper целиком: фальшивый /proc с двумя контейнерами, у обоих один путь лога ----

SRC = (Path(__file__).resolve().parents[2] / "agent" / "fpm-setup.sh").read_text(encoding="utf-8")
BODY = re.search(r"<<'HELPER_EOF'\n(.*?)\nHELPER_EOF\n", SRC, re.S).group(1)
LOG = "/var/log/php7.4-fpm.log"
APP, APP2 = "a" * 64, "b" * 64
W1, W2 = "container " + APP[:12], "container " + APP2[:12]
MON = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _bash() -> str:
    bash = shutil.which("bash")
    if not bash or not shutil.which("awk") or (os.name == "nt" and "system32" in bash.lower()):
        pytest.skip("нет bash или awk")
    return bash


def _posix(p) -> str:
    """Путь для bash: C:/x -> /c/x (двоеточие в PATH делит каталоги)"""
    s = str(p).replace("\\", "/")
    return f"/{s[0].lower()}{s[2:]}" if re.match(r"^[A-Za-z]:/", s) else s


def _fpm_line(ts: float, tzo: int = 0, what="server reached pm.max_children setting (5), consider raising it") -> str:
    d = datetime.fromtimestamp(ts + tzo, timezone.utc)
    return f"[{d.day:02d}-{MON[d.month - 1]}-{d.year} {d:%H:%M:%S}] WARNING: [pool www] {what}\n"


def _write(p: Path, text: str, mode="w"):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, mode, encoding="utf-8", newline="\n") as f:
        f.write(text)


class _Node:
    """Две копии одного образа: мастер php-fpm 101 в контейнере app, 202 в app2 (TZ=EST5, -5 ч),
    error_log у обоих /var/log/php7.4-fpm.log внутри своего корня"""

    def __init__(self, d: Path):
        self.d, self.proc, stubs = d, d / "proc", d / "stubs"
        for pid, cid, env in (("101", APP, "HOSTNAME=app\0"), ("202", APP2, "TZ=EST5\0HOSTNAME=app2\0")):
            _write(self.proc / pid / "cmdline", "php-fpm: master process (/usr/local/etc/php-fpm.conf)\0")
            _write(self.proc / pid / "cgroup", f"0::/system.slice/docker-{cid}.scope\n")
            _write(self.proc / pid / "environ", env)
            _write(self.proc / pid / "root/usr/local/etc/php-fpm.conf",
                   f"[global]\nerror_log = {LOG}\n\n[www]\nlisten = 9000\npm = dynamic\npm.max_children = 5\n")
        stub = {"ps": "case \"$*\" in\n*ppid*) printf '  111   101 php-fpm: pool www\\n  211   202 php-fpm: pool www\\n' ;;\n"
                      "*) printf '  101 php-fpm: master process (x)\\n  202 php-fpm: master process (x)\\n' ;;\nesac\n",
                "docker": "exit 1\n", "journalctl": "exit 1\n"}
        for n, body in stub.items():
            _write(stubs / n, "#!/bin/sh\n" + body)
            os.chmod(stubs / n, 0o755)
        body = BODY.replace("export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                            'export PATH="$KVX_STUBS:/usr/bin:/bin"')
        body = body.replace("exec 9>/run/kervax-fpm.lock\nflock -n 9 || exit 0\n", "")
        # Этот тест подменяет /proc обычными файлами, где нет magic-link exe; проверка exe отдельно выше.
        body = body.replace('  fpm_master "$P" || continue\n', "")
        body = body.replace('"/proc/$', '"$KVX_PROC/$').replace("printf '/proc/%s/root%s' \"$p\"",
                                                                  "printf '%s/%s/root%s' \"$KVX_PROC\" \"$p\"")
        if os.name == "nt":
            marker = 'safe_snap() { [ -n "$SAFE_PY" ] && "$SAFE_PY" -I "$TMPD/rootio.py" snap "$1" "$2" "$3" "$4"; }\n'
            fake = r'''safe_cat() { cat "$KVX_PROC/$1/root$2"; }
safe_glob() { for f in "$KVX_PROC/$1/root$2"/$3; do [ -f "$f" ] && { cat "$f"; echo; }; done; }
safe_snap() {
  src="$KVX_PROC/$1/root$2"; ino=$(stat -c %i "$src") || return; size=$(stat -c %s "$src") || return
  if [ "$size" -gt "$4" ]; then base=$((size - $4)); else base=0; fi
  tail -c "$((size - base))" "$src" > "$3" || return
  printf '%s\t%s\t%s\n' "$ino" "$size" "$base"
}
'''
            assert marker in body
            body = body.replace(marker, marker + fake + 'safe_zone() { return 1; }\n')
        assert "/proc/$" not in body and "flock" not in body
        _write(d / "helper.sh", body)
        self.env = {**os.environ, "KVX_PROC": _posix(self.proc), "KVX_STUBS": _posix(stubs),
                    "KERVAX_FPM_PROC_ROOT": _posix(self.proc), "KERVAX_FPM_PY": _posix(Path(sys.executable)),
                    "KERVAX_FPM_OUT": _posix(d / "fpm.json"), "KERVAX_FPM_STATE": _posix(d / "fpm.state")}

    def log(self, pid: str) -> Path:
        return self.proc / pid / "root" / LOG.lstrip("/")

    def run(self) -> dict:
        out = subprocess.run([_bash(), _posix(self.d / "helper.sh")], env=self.env, capture_output=True, text=True,
                             timeout=60, encoding="utf-8")
        assert out.returncode == 0, out.stderr
        rep = json.loads((self.d / "fpm.json").read_text(encoding="utf-8"))
        return {p["where"]: p for p in rep["pools"]}

    def state(self) -> str:
        return (self.d / "fpm.state").read_text(encoding="utf-8")


def test_helper_two_containers_one_log_path(tmp_path):
    _bash()
    n = _Node(tmp_path)
    now = time.time()
    week = now - 7 * 86400
    old = _fpm_line(week) + _fpm_line(week + 60)
    _write(n.log("101"), "[x] NOTICE: ready to handle connections\n" + old)
    _write(n.log("202"), "[x] NOTICE: fpm is running, pid 1\n[x] NOTICE: ready to handle connections\n" + old)
    # состояние 0.2: одна строка O на оба файла и насчитанные по ней упоры
    ino = os.stat(n.log("101")).st_ino
    _write(tmp_path / "fpm.state", f"O\t{LOG}\t{ino}\t0\nF\t{W1}|www\t{int(week)}\n"
                                   f"B\t{W1}|www\t{int(now) // 600}:68\t\t\t\n")
    p = n.run()
    # миграция: позиции и корзины 0.2 выброшены, новые логи с конца - старые строки не в счет
    assert p[W1]["reached"] == 0 and p[W2]["reached"] == 0 and p[W1]["reached_d"] == 0
    st = n.state()
    assert st.startswith("V\t2\n") and f"O\t{LOG}\t" not in st
    assert f"O\t{W1}|{LOG}\t" in st and f"O\t{W2}|{LOG}\t" in st
    # новые упоры: у app по UTC, у app2 по его местному времени (-5 ч)
    t1 = time.time()
    _write(n.log("101"), _fpm_line(t1) * 3, "a")
    _write(n.log("202"), _fpm_line(t1, -5 * 3600), "a")
    p = n.run()
    assert p[W1]["reached"] == 3 and p[W2]["reached"] == 1
    assert abs(p[W1]["reached_ts"] - t1) < 5 and abs(p[W2]["reached_ts"] - t1) < 5
    assert p[W1]["reached_last"].startswith("server reached pm.max_children setting (5)")
    # прогон без новых строк: ни один лог не перечитан с начала (раньше - каждую минуту)
    p = n.run()
    assert (p[W1]["reached"], p[W2]["reached"]) == (3, 1) and (p[W1]["reached_d"], p[W2]["reached_d"]) == (3, 1)
    # app2 пересоздал лог (новый inode): с начала, но только строки новее последней посчитанной
    t2 = time.time()
    tmp = n.log("202").with_name("new.log")
    _write(tmp, old + _fpm_line(t1 - 600, -5 * 3600) + _fpm_line(t2, -5 * 3600))
    os.replace(tmp, n.log("202"))
    p = n.run()
    assert (p[W1]["reached"], p[W2]["reached"]) == (3, 2)


def test_helper_line_time():
    bash = _bash()
    fn = re.search(r"\nAWKFN='\n(.*?)\n'\n", BODY, re.S).group(1)
    msg = "WARNING: [pool www] server reached pm.max_children setting (5), consider raising it"
    lines = [
        # Docker json-file: время "time" (UTC), а не местное в тексте
        '{"log":"[30-Oct-2026 10:15:02] ' + msg + '\\n","stream":"stderr","time":"2026-10-30T07:15:02.123456789Z"}',
        # строка лога пода (CRI) со своим поясом
        "2026-10-30T10:15:02.123+03:00 stderr F [30-Oct-2026 10:15:02] " + msg,
        # файл FPM в контейнере: местное время, пояс +3
        "[30-Oct-2026 10:15:02] " + msg,
        "[01-Mar-2028 03:00:01.123456] NOTICE: fpm is running",
        "no time here",
    ]
    d = Path(tempfile.mkdtemp())
    try:
        _write(d / "f.awk", fn + "\n{ print ltime($0, msg($0), tzo) }\n")
        _write(d / "in.txt", "\n".join(lines) + "\n")
        out = subprocess.run([bash, "-c", 'LC_ALL=C awk -v tzo=10800 -f "$1" "$2"', "x", _posix(d / "f.awk"),
                              _posix(d / "in.txt")], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        t = int(datetime(2026, 10, 30, 7, 15, 2, tzinfo=timezone.utc).timestamp())
        leap = int(datetime(2028, 3, 1, 0, 0, 1, tzinfo=timezone.utc).timestamp())
        assert [int(float(x)) for x in out.stdout.split()] == [t, t, t, leap, -1]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_helper_offset_key_and_state():
    # позиция - по месту и пути, старое состояние без V 2 теряет позиции и корзины
    assert 'printf \'%s|%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n\' "$w" "$f" "$ff" "$st" "$tzo" "$w" "$base"' in BODY
    assert "if (!(f in off)) { nl[f] = now; next }" in BODY and "if (tl < lw || tl <= now - 86400) continue" in BODY
    assert "grep -q '^V\t2$' \"$STATE\"" in BODY and "$1 != \"O\" && $1 != \"B\"" in BODY
    # строки времени без mktime (его нет в mawk)
    assert "mktime(" not in BODY and "strftime(" not in BODY
