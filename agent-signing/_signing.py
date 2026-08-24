"""Общие помощники подписи релизов агента (используют keygen/protect_key/release).

Ничего секретного здесь не хранится — только логика. Приватный ключ живёт в
kervax-agent.key (в идеале зашифрован пасфразой) и НИКОГДА не покидает dev-машину.
"""
import base64
import getpass
import os
import re
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# вывод в UTF-8 независимо от кодовой страницы консоли (Windows cp1251 не тянет →/✓)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AGENT = os.path.join(ROOT, "agent")
DIST = os.path.join(ROOT, "agent-dist")
PRIV = os.path.join(HERE, "kervax-agent.key")
PUB = os.path.join(HERE, "kervax-agent.pub")


def pub_b64(pub: ed25519.Ed25519PublicKey) -> str:
    return base64.b64encode(
        pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()


def priv_raw_b64(priv: ed25519.Ed25519PrivateKey) -> str:
    return base64.b64encode(
        priv.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode()


def ask_passphrase(prompt: str, confirm: bool = False) -> bytes:
    """Пасфраза: из env KERVAX_SIGN_PASSPHRASE (для неинтерактивного запуска) или getpass.
    Интерактивный ввод безопаснее (не попадает в историю/окружение)."""
    env = os.environ.get("KERVAX_SIGN_PASSPHRASE")
    if env is not None:
        return env.encode()
    p = getpass.getpass(prompt)
    if confirm and p != getpass.getpass("Повтори пасфразу: "):
        sys.exit("Пасфразы не совпали.")
    return p.encode()


def load_signing_key() -> ed25519.Ed25519PrivateKey:
    """Загружает приватный ключ: зашифрованный PEM (спросит пасфразу) ИЛИ legacy raw-base64."""
    if not os.path.exists(PRIV):
        sys.exit(f"Нет приватного ключа {PRIV} — сначала keygen.py")
    data = open(PRIV, "rb").read()
    if b"PRIVATE KEY" in data:  # PEM (зашифрован пасфразой)
        pw = ask_passphrase("Пасфраза ключа подписи: ")
        key = serialization.load_pem_private_key(data, password=pw or None)
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            sys.exit("Ключ не Ed25519.")
        return key
    # legacy: незашифрованный raw-base64
    sys.stderr.write(
        "! ВНИМАНИЕ: ключ подписи НЕ зашифрован. Запусти protect_key.py, "
        "чтобы закрыть его пасфразой.\n"
    )
    return ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(data.strip()))


def read_agent_version() -> str:
    """Возвращает const version из agent/main.go (пубключ инжектится при сборке, не в коде)."""
    src = open(os.path.join(AGENT, "main.go"), encoding="utf-8").read()
    v = re.search(r'const version = "([^"]+)"', src)
    if not v:
        sys.exit("Не нашёл const version в agent/main.go")
    return v.group(1)


def read_pubkey() -> str:
    """Публичный ключ подписи (base64) из kervax-agent.pub."""
    if not os.path.exists(PUB):
        sys.exit(f"Нет {PUB} — сначала keygen.py")
    return open(PUB, encoding="utf-8").read().strip()
