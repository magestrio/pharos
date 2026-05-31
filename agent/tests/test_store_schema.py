"""Tests for the schema migration runner (`data-store.2`).

Runs against a real Postgres via `testcontainers[postgres]`. Container
is session-scoped (one startup per pytest invocation, ~5s overhead);
each test gets a fresh database to isolate state. If Docker is
unavailable the whole module is skipped — no silent green tests when
the underlying engine isn't reachable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.sandbox.store.pool import open_pool
from agent.sandbox.store.schema import apply_migrations

# Postgres container + per-test DB fixtures live in `conftest.py` so
# `test_store_writer.py` can share them without spinning up a second
# container.


# ─── tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_migrations_fresh_init_creates_all_tables(
    fresh_db_dsn: str,
) -> None:
    """Apply migrations against an empty DB; verify every table from
    `0001_initial.sql` exists + `schema_migrations` records the version."""
    async with open_pool(fresh_db_dsn) as pool:
        applied = await apply_migrations(pool)
        assert applied == ["0001_initial"]

        async with pool.acquire() as conn:
            tables = {
                r["table_name"]
                for r in await conn.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            }
        expected = {
            "schema_migrations",
            "cycles",
            "snapshots",
            "decisions",
            "positions_snapshot",
            "events",
            "executions",
        }
        assert expected.issubset(tables), (
            f"missing tables: {expected - tables}"
        )

        async with pool.acquire() as conn:
            versions = {
                r["version"]
                for r in await conn.fetch("SELECT version FROM schema_migrations")
            }
        assert versions == {"0001_initial"}


@pytest.mark.asyncio
async def test_apply_migrations_idempotent_on_repeat(fresh_db_dsn: str) -> None:
    """Second call returns empty + leaves the DB unchanged."""
    async with open_pool(fresh_db_dsn) as pool:
        first = await apply_migrations(pool)
        second = await apply_migrations(pool)
        third = await apply_migrations(pool)
        assert first == ["0001_initial"]
        assert second == []
        assert third == []
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM schema_migrations")
        assert count == 1


@pytest.mark.asyncio
async def test_apply_migrations_rejects_unknown_future_version(
    fresh_db_dsn: str,
) -> None:
    """If the DB already has a version this build doesn't know about,
    the runner MUST refuse to start — that's the canary for "deployed
    a newer image earlier, rolled back, code is now stale"."""
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        # Manually inject a future version that no file backs.
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ($1)",
                "9999_from_the_future",
            )
        with pytest.raises(RuntimeError, match="unknown to this build"):
            await apply_migrations(pool)


@pytest.mark.asyncio
async def test_apply_migrations_partial_state_skips_already_applied(
    fresh_db_dsn: str, tmp_path: Path
) -> None:
    """When the DB has 0001 marked applied but not 0002, only 0002
    runs. Guards the "we shipped a second migration after the first
    rollout" case."""
    fake_dir = tmp_path / "migs"
    fake_dir.mkdir()
    (fake_dir / "0001_initial.sql").write_text(
        "CREATE TABLE t1 (id INT PRIMARY KEY);"
    )
    (fake_dir / "0002_followup.sql").write_text(
        "ALTER TABLE t1 ADD COLUMN name TEXT;"
    )

    async with open_pool(fresh_db_dsn) as pool:
        # Simulate the partial state: 0001 already applied (table exists,
        # version marked), 0002 hasn't run yet.
        async with pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE schema_migrations ("
                "  version TEXT PRIMARY KEY, "
                "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            await conn.execute("CREATE TABLE t1 (id INT PRIMARY KEY)")
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ('0001_initial')"
            )

        applied = await apply_migrations(pool, migrations_dir=fake_dir)
        assert applied == ["0002_followup"]

        async with pool.acquire() as conn:
            cols = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 't1'"
                )
            }
        assert cols == {"id", "name"}


def test_open_pool_without_dsn_or_env_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No DSN argument + no DATABASE_URL → fail fast with a clear msg."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    async def _check() -> None:
        async with open_pool():
            pass

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        asyncio.run(_check())
