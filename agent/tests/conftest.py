"""Shared pytest fixtures across the agent test suite.

Currently hosts the Postgres testcontainers fixtures used by
`test_store_schema.py` and `test_store_writer.py`. Keeping them here
avoids spinning up two containers when the modules run in the same
session.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio

try:
    from testcontainers.postgres import PostgresContainer
except ImportError:  # pragma: no cover — extras not installed
    PostgresContainer = None  # type: ignore[misc, assignment]


@pytest.fixture(scope="session")
def _postgres_container() -> Iterator[str]:
    """Session-scoped Postgres container. Yields the admin DSN.
    Skips the consumer test if Docker isn't reachable — clearer than a
    cryptic connection error from asyncpg."""
    if PostgresContainer is None:
        pytest.skip("testcontainers[postgres] not installed")
    try:
        container = PostgresContainer("postgres:16-alpine", driver=None)
        container.start()
    except Exception as e:  # noqa: BLE001 — Docker daemon missing, etc.
        pytest.skip(f"Postgres container unavailable (Docker not running?): {e}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest_asyncio.fixture
async def fresh_db_dsn(_postgres_container: str) -> AsyncIterator[str]:
    """Function-scoped fresh database (via CREATE DATABASE) — drops on
    teardown so tests don't bleed schema state into each other."""
    admin_dsn = _postgres_container
    db_name = f"t_{uuid.uuid4().hex[:16]}"

    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()

    prefix = admin_dsn.rsplit("/", 1)[0] if "/" in admin_dsn else admin_dsn
    target_dsn = f"{prefix}/{db_name}"

    try:
        yield target_dsn
    finally:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1",
                db_name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await conn.close()
