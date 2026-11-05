"""Metadata survives the real report -> persistence -> server detail API path."""

import pytest


@pytest.mark.parametrize("qdrant", [
    {
        "scope": "node", "state": "ok", "version": "1.15.0", "collections_total": 1,
        "points_approximate": True, "collections": [{
            "name": "example", "points_count": 27, "indexed_vectors_count": 54,
            "status": "green", "optimizer_status": "ok",
        }],
    },
    {"scope": "node", "state": "auth_required", "points_approximate": True},
    {"scope": "node", "state": "unavailable", "reason": "timeout", "points_approximate": True},
    {"scope": "node", "state": "ok", "collections_total": 0, "points_approximate": True},
])
async def test_qdrant_metadata_roundtrip(client, auth_headers, qdrant):
    enrolled = await client.post("/api/servers", json={"name": "metadata-lab"}, headers=auth_headers)
    assert enrolled.status_code == 201
    token = enrolled.json()["token"]
    sid = enrolled.json()["server"]["id"]
    services = [
        {"kind": "rabbitmq", "source": "контейнер queue", "total": 1,
         "queues": [{"name": "tasks", "ready": 2}]},
        {"kind": "qdrant", "source": "контейнер vectors", "qdrant": qdrant},
    ]
    report = {"hostname": "metadata-lab", "services": services}
    response = await client.post("/api/agent/report", json=report,
                                 headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    detail = await client.get(f"/api/servers/{sid}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["last_report"]["services"] == services
    # New optional data must not become mandatory for legacy/Windows reporters.
    response = await client.post("/api/agent/report", json={"platform": "windows", "hostname": "metadata-lab"},
                                 headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
