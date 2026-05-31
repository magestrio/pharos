"""asyncpg connection pool for the vault8004 cycle store
(`data-store.2`).

Tiny wrapper around `asyncpg.create_pool` that reads `DATABASE_URL`
from the environment when no explicit DSN is passed. The agent's
long-running loop opens one pool at startup via `async with
open_pool() as pool` and shares it across writer/reader call sites.
Tests open their own pool against a per-test database.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

DEFAULT_MIN_SIZE = 1
DEFAULT_MAX_SIZE = 5


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup. Registers JSON/JSONB codecs so both the
    writer (`record_cycle` passes Python dicts) and downstream readers
    (web/API) see dicts on the wire, not opaque strings."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


@asynccontextmanager
async def open_pool(
    dsn: str | None = None,
    *,
    min_size: int = DEFAULT_MIN_SIZE,
    max_size: int = DEFAULT_MAX_SIZE,
) -> AsyncIterator[asyncpg.Pool]:
    """Context-manager wrapper. Closes the pool on exit (including
    propagated exceptions). If `dsn` is omitted, reads `DATABASE_URL`
    from the env — fails fast with a clear message if unset.
    """
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "open_pool() called with no DSN and DATABASE_URL is not set in "
            "the environment. Set DATABASE_URL to a Postgres connection "
            "string (e.g. postgres://user:pass@host:5432/dbname)."
        )
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        init=_init_connection,
    )
    try:
        yield pool
    finally:
        await pool.close()
