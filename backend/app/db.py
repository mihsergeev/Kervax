from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Request
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def create_engine_and_factory(
    db_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    url = make_url(db_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    if url.get_backend_name() == "postgresql":
        # pool_pre_ping: после рестарта db соединения из пула мертвые, без проверки каждое
        # один раз отдавало ошибку в запрос. command_timeout: запрос, вставший на блокировку
        # (миграция с ALTER TABLE во время выкладки), иначе ждал вечно и вешал цикл
        # планировщика при живом процессе и свежем пульсе
        engine = create_async_engine(
            db_url,
            pool_pre_ping=True,
            pool_recycle=3600,
            connect_args={"command_timeout": 300},
        )
    else:
        engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session
