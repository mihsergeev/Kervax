"""Одноразовый результат команды между воркером агента и ожидающим запросом."""

import json

from app import backup
from app.config import get_settings

PRIVATE_ACTIONS = frozenset({"get_creds", "get_client_creds", "provision_client", "provision_new"})
SEALED_PREFIX = "sealed:v1:"


def seal(command_id: int, output: str) -> str:
    payload = backup.seal({"command_id": command_id, "output": output}, get_settings())
    return SEALED_PREFIX + json.dumps(payload)


def unseal(command_id: int, result: str) -> str:
    if result.startswith(SEALED_PREFIX):
        envelope = json.loads(result[len(SEALED_PREFIX):])
        if isinstance(envelope, dict) and isinstance(envelope.get("encrypted"), dict):
            payload = backup.unseal(envelope, get_settings())
            if payload.get("command_id") == command_id and isinstance(payload.get("output"), str):
                return payload["output"]
    raise ValueError("данные команды недоступны, повторите операцию")
