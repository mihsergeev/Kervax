"""Пересобрать скрины README одной командой.

Поднимает панель на временной SQLite-базе с демо-данными, раздаёт собранный
фронт, снимает кадры и всё за собой убирает. Реальная панель, реальный фронт —
меняется только содержимое базы.

    cd frontend && npm run build && npm i playwright-core && cd ..
    python docs/make-shots.py

Планировщик выключен намеренно: иначе он пошёл бы проверять example.com по сети
и переписал бы демо-статусы своими.
"""
import http.server
import os
import secrets
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "frontend" / "dist"
API_PORT, WEB_PORT = 8111, 5111
# Панель живёт на 127.0.0.1 ровно на время съёмки, но пароль в исходниках
# выглядит как пароль — генерируем его на каждый запуск и никуда не пишем.
PASSWORD = secrets.token_urlsafe(18)
TMP = Path(tempfile.gettempdir()) / "kervax-shots"

PY = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = ROOT / "backend" / ".venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)


class Handler(http.server.SimpleHTTPRequestHandler):
    """Статика + прокси /api: vite dev не поднимается на сетевом диске (его
    файловый watcher не умеет в SMB), а для съёмки слежение и не нужно."""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DIST), **kw)

    def log_message(self, *a):
        pass

    def _proxy(self, method):
        body = None
        if (n := int(self.headers.get("Content-Length") or 0)):
            body = self.rfile.read(n)
        req = urllib.request.Request(f"http://127.0.0.1:{API_PORT}{self.path}",
                                     data=body, method=method)
        for h in ("Authorization", "Content-Type", "Accept", "Accept-Language"):
            if h in self.headers:
                req.add_header(h, self.headers[h])
        try:
            with urllib.request.urlopen(req) as r:
                data, code, ctype = r.read(), r.status, r.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            data, code, ctype = e.read(), e.code, e.headers.get("Content-Type")
        self.send_response(code)
        self.send_header("Content-Type", ctype or "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api"):
            return self._proxy("GET")
        if "." not in self.path.rsplit("/", 1)[-1]:
            self.path = "/index.html"       # SPA-роутинг
        return super().do_GET()

    do_POST = lambda self: self._proxy("POST")      # noqa: E731
    do_PUT = lambda self: self._proxy("PUT")        # noqa: E731
    do_PATCH = lambda self: self._proxy("PATCH")    # noqa: E731
    do_DELETE = lambda self: self._proxy("DELETE")  # noqa: E731


def env_for(db: Path, data: Path) -> dict:
    return {
        **os.environ,
        "KERVAX_DB_URL": f"sqlite+aiosqlite:///{db.as_posix()}",
        "KERVAX_DATA_DIR": str(data),
        "KERVAX_ADMIN_USER": "admin",
        "KERVAX_ADMIN_PASSWORD": PASSWORD,
        "KERVAX_JWT_SECRET": "demo-secret-0123456789abcdef0123456789abcdef",
        "KERVAX_RUN_SCHEDULER": "0",
        # чтобы панель знала текущие версии helper-скриптов: иначе ей не с чем
        # сравнивать те, что «стоят» на демо-нодах, и раздел «Требует действий»
        # оказался бы пустым не потому, что всё в порядке
        "KERVAX_AGENT_DIST_DIR": str(ROOT / "agent"),
    }


def wait_api(timeout=40) -> None:
    for _ in range(timeout * 4):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/health", timeout=2):
                return
        except OSError:
            time.sleep(0.25)
    raise SystemExit("бэкенд не поднялся")


def main() -> None:
    if not DIST.exists():
        raise SystemExit("нет frontend/dist — соберите фронт: npm run build")
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    web = socketserver.ThreadingTCPServer(("127.0.0.1", WEB_PORT), Handler)
    threading.Thread(target=web.serve_forever, daemon=True).start()

    for lang in ("en", "ru"):
        db, data = TMP / f"demo-{lang}.db", TMP / f"data-{lang}"
        data.mkdir()
        env = env_for(db, data)
        print(f"[{lang}] база…")
        subprocess.run([str(PY), str(ROOT / "docs" / "demo-seed.py"), str(db), lang],
                       env=env, check=True, cwd=ROOT)
        print(f"[{lang}] панель…")
        api = subprocess.Popen(
            [str(PY), "-m", "uvicorn", "app.main:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(API_PORT), "--log-level", "warning"],
            cwd=ROOT / "backend", env=env)
        try:
            wait_api()
            subprocess.run(
                [shutil.which("node") or "node", str(ROOT / "frontend" / "scripts" / "shots.mjs")],
                cwd=ROOT / "frontend",       # playwright-core ставится сюда
                env={**os.environ, "SHOTS_LANG": lang, "SHOTS_PASS": PASSWORD,
                     "SHOTS_BASE": f"http://127.0.0.1:{WEB_PORT}",
                     "SHOTS_OUT": str(ROOT / "docs" / "img")},
                check=True)
        finally:
            api.terminate()
            api.wait(timeout=15)
    web.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    print("готово:", ROOT / "docs" / "img")


if __name__ == "__main__":
    main()
