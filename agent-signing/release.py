#!/usr/bin/env python3
"""Собрать + подписать релиз агента Kervax. Одна команда:

  python agent-signing/release.py 1.8

Делает:
  0. PREFLIGHT: версия в agent/main.go == релизной; ключ подписи соответствует
     kervax-agent.pub.
  1. Собирает статические бинари amd64/arm64 с ВШИТЫМ пубключом
     (-X main.updatePubKeyB64=<pub>) — ровно так же, как их соберёт панель.
     Приоритет — локальный Docker (сборка = подпись); фолбэк — build-хост по ssh.
  2. Считает sha256/размер, пишет канонический manifest.json, подписывает
     офлайн-ключом и само-проверяет подпись.
  3. Кладёт в agent-dist/ ТОЛЬКО manifest.json + manifest.sig (бинари панель
     собирает сама из исходников тем же способом → хеши совпадают, reproducible).

ВАЖНО: панель должна собирать агента с ТЕМ ЖЕ пубключом — задай в .env
KERVAX_AGENT_PUBKEY = содержимое kervax-agent.pub (иначе агенты отвергнут релиз).
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _signing import (  # noqa: E402
    AGENT, DIST, HERE, load_signing_key, pub_b64, read_agent_version, read_pubkey,
)

GO_IMAGE = "golang:1.22-alpine"
ARCHES = ("amd64", "arm64")
BUILD_HOST_FILE = os.path.join(HERE, "build-host.txt")


def win(p: str) -> str:
    return os.path.abspath(p).replace("\\", "/")


def ldflags(pubkey: str) -> str:
    return f"-s -w -buildid= -X main.updatePubKeyB64={pubkey}"


def docker_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode == 0
    except FileNotFoundError:
        return False


def build_local(pubkey: str, out: str):
    print("  сборка локальным Docker (сборка = подпись)")
    for arch in ARCHES:
        subprocess.run([
            "docker", "run", "--rm",
            "-v", f"{win(AGENT)}:/src:ro", "-v", f"{win(out)}:/out", "-w", "/src",
            "-e", "CGO_ENABLED=0", "-e", "GOOS=linux", "-e", f"GOARCH={arch}",
            GO_IMAGE, "go", "build", "-trimpath", f"-ldflags={ldflags(pubkey)}",
            "-o", f"/out/kervax-agent-{arch}", ".",
        ], check=True)


def build_remote(pubkey: str, out: str, host: str, keyfile: str):
    print(f"! Локальный Docker недоступен → сборка на build-хосте {host}")
    print("  ВНИМАНИЕ: build-хост в цепочке доверия (собирает подписываемые байты).")
    print("  Для максимума доверия подними локальный Docker.")
    base = ["-i", keyfile, "-o", "StrictHostKeyChecking=no"]
    ssh = lambda c: subprocess.run(["ssh", *base, host, c], check=True)  # noqa: E731
    scp = lambda a, cwd=None: subprocess.run(["scp", *base, *a], check=True, cwd=cwd)
    ssh("rm -rf /tmp/kervax-rel && mkdir -p /tmp/kervax-rel/src /tmp/kervax-rel/out")
    scp(["go.mod", "main.go", f"{host}:/tmp/kervax-rel/src/"], cwd=AGENT)
    for arch in ARCHES:
        ssh(
            # docker без прав → sudo -n (build-хост может быть не root)
            'DOCKER=$(docker info >/dev/null 2>&1 && echo docker || echo "sudo -n docker"); '
            "$DOCKER run --rm -v /tmp/kervax-rel/src:/src:ro "
            "-v /tmp/kervax-rel/out:/out -w /src "
            f"-e CGO_ENABLED=0 -e GOOS=linux -e GOARCH={arch} {GO_IMAGE} "
            f"go build -trimpath -ldflags='{ldflags(pubkey)}' -o /out/kervax-agent-{arch} ."
        )
        scp([f"{host}:/tmp/kervax-rel/out/kervax-agent-{arch}", "."], cwd=out)
    ssh("rm -rf /tmp/kervax-rel")


def build_all(pubkey: str, out: str):
    if docker_available():
        build_local(pubkey, out)
        return
    host = ""
    if os.path.exists(BUILD_HOST_FILE):
        host = open(BUILD_HOST_FILE, encoding="utf-8").read().strip()
    host = os.environ.get("KERVAX_BUILD_SSH", host)
    if not host:
        sys.exit(
            "Локальный Docker не запущен и build-хост не задан.\n"
            "  → запусти Docker Desktop (безопаснее), ИЛИ\n"
            f"  → впиши ssh-цель в {BUILD_HOST_FILE} (напр. root@203.0.113.10)"
        )
    keyfile = os.path.expanduser(
        os.environ.get("KERVAX_BUILD_SSH_KEY", "~/.ssh/ms_dev_ed25519")
    )
    build_remote(pubkey, out, host, keyfile)


def sha256_size(path: str):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if len(args) != 1:
        sys.exit("Использование: release.py <версия>  (напр. 1.8)")
    version = args[0].strip().lstrip("v")

    # --- PREFLIGHT ---
    ver_const = read_agent_version()
    if ver_const != version:
        sys.exit(
            f"agent/main.go const version = \"{ver_const}\", а релиз {version}. Синхронизируй."
        )
    key = load_signing_key()
    pubkey = read_pubkey()
    if pub_b64(key.public_key()) != pubkey:
        sys.exit("Ключ подписи не соответствует kervax-agent.pub — прерываю.")
    print(f"Релиз агента {version}  (preflight ок)")

    # --- BUILD (во временный каталог; панель соберёт свои бинари тем же способом) ---
    os.makedirs(DIST, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        build_all(pubkey, tmp)
        artifacts = {}
        for arch in ARCHES:
            path = os.path.join(tmp, f"kervax-agent-{arch}")
            if not os.path.exists(path):
                sys.exit(f"Сборка не дала {path}")
            digest, size = sha256_size(path)
            artifacts[arch] = {"sha256": digest, "size": size}
            print(f"  [{arch}] sha256={digest[:16]}… size={size}")
            # бинарь — в agent-dist рядом с манифестом: панель раздаёт его на
            # /api/agent/download/<arch>, а sha обязан совпадать с манифестом
            shutil.copy2(path, os.path.join(DIST, f"kervax-agent-{arch}"))

    manifest = {
        "version": version,
        "released_at": datetime.now(timezone.utc).isoformat(),
        "min_agent": "1.0",
        "artifacts": artifacts,
    }
    blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    sig = key.sign(blob)
    key.public_key().verify(sig, blob)  # само-проверка

    with open(os.path.join(DIST, "manifest.json"), "wb") as f:
        f.write(blob)
    with open(os.path.join(DIST, "manifest.sig"), "w", encoding="utf-8") as f:
        f.write(base64.b64encode(sig).decode() + "\n")

    print("\nПодписано и само-проверено ✓")
    print(f"  {os.path.join(DIST, 'manifest.json')}")
    print(f"  {os.path.join(DIST, 'manifest.sig')}")
    print(f"\nУбедись, что в .env панели:  KERVAX_AGENT_PUBKEY={pubkey}")
    print("Затем задеплой панель и в UI: Серверы → Canary одну ноду → «Обновить все».")


if __name__ == "__main__":
    main()
