"""Сжатый бинарь агента для самообновления (агент 2.18+): отдается, только если распаковывается
ровно в подписанный."""
import gzip
import hashlib
import json

import httpx
import pytest

from app import config


@pytest.fixture
async def dist_client(tmp_path, monkeypatch):
    dist = tmp_path / "agent"
    dist.mkdir()
    binary = b"kervax-agent " * 100000
    (dist / "kervax-agent-amd64").write_bytes(binary)
    (dist / "kervax-agent-amd64.gz").write_bytes(gzip.compress(binary, 9))
    (dist / "manifest.json").write_text(json.dumps({"version": "2.18", "artifacts": {
        "amd64": {"sha256": hashlib.sha256(binary).hexdigest(), "size": len(binary)}}}))
    monkeypatch.setenv("KERVAX_DB_URL", f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}")
    monkeypatch.setenv("KERVAX_DATA_DIR", (tmp_path / "data").as_posix())
    monkeypatch.setenv("KERVAX_JWT_SECRET", "test-secret-0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("KERVAX_AGENT_DIST_DIR", dist.as_posix())
    config.get_settings.cache_clear()
    from app.api import servers
    servers._gz_cache.clear()
    from app.main import create_app
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c, dist, binary
    await app.state.engine.dispose()
    config.get_settings.cache_clear()


async def test_gz_served_with_head_and_range(dist_client):
    c, dist, binary = dist_client
    gz = (dist / "kervax-agent-amd64.gz").read_bytes()
    head = await c.head("/api/agent/download-gz/amd64")
    assert head.status_code == 200 and int(head.headers["content-length"]) == len(gz)
    part = await c.get("/api/agent/download-gz/amd64", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == gz[:100]
    full = await c.get("/api/agent/download-gz/amd64")
    assert gzip.decompress(full.content) == binary
    assert len(gz) * 2 < len(binary)  # ради этого все и затевалось
    assert (await c.get("/api/agent/download-gz/mips")).status_code == 404
    # arm64 в этой раздаче нет вовсе
    assert (await c.head("/api/agent/download-gz/arm64")).status_code == 404


async def test_stale_gz_is_not_served(dist_client):
    """Архив прошлой сборки не сходится с манифестом: 404, агент возьмет несжатый."""
    c, dist, binary = dist_client
    (dist / "kervax-agent-amd64.gz").write_bytes(gzip.compress(binary + b"old", 9))
    assert (await c.head("/api/agent/download-gz/amd64")).status_code == 404
    (dist / "kervax-agent-amd64.gz").write_bytes(b"not a gzip at all")
    assert (await c.get("/api/agent/download-gz/amd64")).status_code == 404
