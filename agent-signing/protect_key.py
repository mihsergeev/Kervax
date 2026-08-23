#!/usr/bin/env python3
"""Зашифровать приватный ключ подписи пасфразой (разово, keypair НЕ меняется).

  python agent-signing/protect_key.py

После этого kervax-agent.key — зашифрованный PEM: файл сам по себе бесполезен,
подписать релиз можно только зная пасфразу. Пасфразу храни в пароль-менеджере
(рядом с бэкапом ключа). Публичный ключ и уже установленные агенты НЕ затрагиваются.
"""
import os
import sys

from cryptography.hazmat.primitives import serialization

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _signing import PRIV, PUB, ask_passphrase, load_signing_key, pub_b64  # noqa: E402


def main():
    data = open(PRIV, "rb").read()
    if b"PRIVATE KEY" in data:
        # уже зашифрован — предложим сменить пасфразу
        print("Ключ уже зашифрован. Введу текущую, затем новую пасфразу.")
    key = load_signing_key()  # спросит текущую пасфразу, если PEM

    # сверим, что пубключ не «уплыл»
    if os.path.exists(PUB):
        want = open(PUB).read().strip()
        if pub_b64(key.public_key()) != want:
            sys.exit("Публичный ключ не совпал с kervax-agent.pub — прерываю.")

    pw = ask_passphrase("Новая пасфраза для ключа подписи: ", confirm=True)
    if not pw:
        sys.exit("Пустая пасфраза недопустима.")

    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(pw),
    )
    with open(PRIV, "wb") as f:
        f.write(pem)
    try:
        os.chmod(PRIV, 0o600)
    except OSError:
        pass
    print(f"Готово: {PRIV} теперь зашифрован пасфразой (PKCS8 PEM).")
    print("Сохрани пасфразу в пароль-менеджер. Без неё релизы не подписать.")


if __name__ == "__main__":
    main()
