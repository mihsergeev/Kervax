"""Vector databases flow through the existing backup audit and Services source."""

import pytest

from app.api.servers import _backup_coverage, _db_engine_of
from app.models import Server


@pytest.mark.parametrize(("image", "engine"), [
    ("qdrant/qdrant:v1.15", "Qdrant"),
    ("milvusdb/milvus:v2.6", "Milvus"),
    ("cr.weaviate.io/semitechnologies/weaviate:1.40", "Weaviate"),
    ("chromadb/chroma:1.0", "Chroma"),
    ("ghcr.io/chroma-core/chroma@sha256:abcdef", "Chroma"),
    ("registry.example:5000/chromadb/chroma:1.0", "Chroma"),
])
def test_vector_server_images(image, engine):
    assert _db_engine_of(image) == engine


@pytest.mark.parametrize("image", [
    "qdrant/qdrant-client:latest",
    "registry/qdrant-operator/qdrant:latest",
    "zilliztech/milvus-backup:latest",
    "semitechnologies/weaviate-console:latest",
    "chroma/chroma-exporter:latest",
    "myapp/chroma:latest",
    "notchromadb/chroma:latest",
])
def test_vector_tool_images_not_databases(image):
    assert _db_engine_of(image) is None


def test_vector_audit_deduplicates_process_docker_and_kubernetes():
    rep = {
        "db_engines": ["Qdrant", "Milvus", "Weaviate"],
        "docker": {"containers": [
            {"name": "vectors", "image": "qdrant/qdrant:v1.15", "state": "running"},
            {"name": "backup", "image": "zilliztech/milvus-backup:latest", "state": "running"},
        ]},
        "kube": {"pods": [
            {"ns": "search", "name": "milvus-0", "owner": "StatefulSet", "node": "node-1",
             "image": "milvusdb/milvus:v2.6", "phase": "Running", "ready": True},
            {"ns": "search", "name": "weaviate-0", "owner": "StatefulSet", "node": "node-1",
             "image": "cr.weaviate.io/semitechnologies/weaviate:1.40", "phase": "Running", "ready": True},
            {"ns": "search", "name": "chroma-0", "owner": "StatefulSet", "node": "node-1",
             "image": "ghcr.io/chroma-core/chroma:latest", "phase": "Running", "ready": True},
        ]},
    }
    server = Server(name="node-1", token_hash="x", last_report=rep)
    rows = [row for row in _backup_coverage(server) if row.kind.startswith("db")]
    assert {(row.subject, row.instance) for row in rows} == {
        ("Qdrant", "vectors"),
        ("Milvus", "k8s.search.sts.milvus"),
        ("Weaviate", "k8s.search.sts.weaviate"),
        ("Chroma", "k8s.search.sts.chroma"),
    }
    assert all(row.kind == "db" and row.dump_engine == "" and not row.can_dump for row in rows)
    assert all(row.note for row in rows)
    assert "snapshot API" in next(row for row in rows if row.subject == "Qdrant").note
    assert "milvus-backup" in next(row for row in rows if row.subject == "Milvus").note
    assert "Backup API" in next(row for row in rows if row.subject == "Weaviate").note
    assert "persistent volume" in next(row for row in rows if row.subject == "Chroma").note
    assert next(row for row in rows if row.subject == "Qdrant").container == "vectors"
    assert next(row for row in rows if row.subject == "Milvus").pods == ["search/milvus-0"]


def test_vector_tool_named_container_is_not_counted():
    server = Server(name="node-1", token_hash="x", last_report={
        "docker": {"containers": [{"name": "qdrant-client", "image": "qdrant/qdrant:latest", "state": "running"}]},
    })
    assert not [row for row in _backup_coverage(server) if row.kind.startswith("db")]
