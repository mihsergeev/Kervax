#!/usr/bin/env python3
"""Одноразовая генерация Ed25519-ключпары для подписи релизов агента.

  python agent-signing/keygen.py            # ключ шифруется пасфразой (рекомендуется)
  python agent-signing/keygen.py --plain    # без пасфразы (потом можно protect_key.py)

Приватный ключ — КОРЕНЬ ДОВЕРИЯ: НИКОГДА не в git (см. .gitignore), НИКОГДА на
панели, хранится офлайн + бэкап (и пасфраза) в пароль-менеджер. Публичный ключ
вшивается в agent/main.go (const updatePubKeyB64). Запускать ОДИН раз.
"""
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _signing import PRIV, PUB, ask_passphrase, priv_raw_b64, pub_b64  # noqa: E402


def main():
    plain = "--plain" in sys.argv
    if os.path.exists(PRIV):
        sys.exit(f"ОТКАЗ: {PRIV} уже существует — не перезатираю ключ.")

    key = ed25519.Ed25519PrivateKey.generate()
    if plain:
        with open(PRIV, "w", encoding="utf-8") as f:
            f.write(priv_raw_b64(key) + "\n")
    else:
        pw = ask_passphrase("Пасфраза для ключа подписи: ", confirm=True)
        if not pw:
            sys.exit("Пустая пасфраза недопустима (или используй --plain).")
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(pw),
        )
        with open(PRIV, "wb") as f:
            f.write(pem)
    try:
        os.chmod(PRIV, 0o600)
    except OSError:
        pass

    pb = pub_b64(key.public_key())
    with open(PUB, "w", encoding="utf-8") as f:
        f.write(pb + "\n")

    print("Ключпара создана" + (" (без пасфразы)" if plain else " (зашифрована пасфразой)"))
    print(f"  приватный: {PRIV}  (0600, В GITIGNORE — забэкапь ключ И пасфразу!)")
    print(f"  публичный: {PUB}")
    print("\nВшить в agent/main.go:")
    print(f'  const updatePubKeyB64 = "{pb}"')


if __name__ == "__main__":
    main()
