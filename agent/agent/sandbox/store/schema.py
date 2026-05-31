"""Migration runner for the cycle store (`data-store.2`).

Discovers `migrations/*.sql` files lex-sorted, applies each unseen one
inside a transaction, records the version in `schema_migrations`. Safe
to run on every agent startup — idempotent re-runs are a no-op.

The runner deliberately refuses to start when the database has a
version the code doesn't know about (i.e. the operator deployed a
newer image earlier and is now running an older binary against the
same DB). That state means either the code needs an upgrade or the DB
needs a rollback — both operator decisions, never silently overridden.
"""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Version-tracking table. Bootstrapped on first run if missing — chicken-
# and-egg solved by always running this CREATE before consulting the
# table.
_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     TEXT PRIMARY KEY,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _list_migration_files(migrations_dir: Path) -> list[Path]:
    """Return migration files in apply order. Filename convention:
    `<NNNN>_<description>.sql` — lex-sorted ascending."""
    if not migrations_dir.is_dir():
        return []
    return sorted(migrations_dir.glob("*.sql"))


def _version_of(path: Path) -> str:
    """Extract the version slug from a migration filename. For
    `0001_initial.sql` this is `"0001_initial"` — the filename minus
    `.sql` — so a renamed file (e.g. typo fix) doesn't silently rerun
    as a "new" migration. The full stem is the version."""
    return path.stem


async def apply_migrations(
    pool: asyncpg.Pool,
    *,
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR,
) -> list[str]:
    """Apply any not-yet-applied migrations. Returns the list of
    versions newly applied (empty list when DB is already up-to-date).

    Raises `RuntimeError` when the DB has versions the code doesn't
    know about — caller is the agent loop's startup; refuse-to-start
    is the desired behavior.
    """
    files = _list_migration_files(migrations_dir)
    known_versions = [_version_of(p) for p in files]
    known_set = set(known_versions)

    applied_now: list[str] = []
    async with pool.acquire() as conn:
        # Bootstrap the version table itself. CREATE TABLE IF NOT EXISTS
        # is safe to re-run; no transaction needed.
        await conn.execute(_BOOTSTRAP_SQL)

        # Discover what's already applied.
        rows = await conn.fetch("SELECT version FROM schema_migrations")
        db_versions = {r["version"] for r in rows}

        # Refuse if DB has anything we don't know about.
        unknown = db_versions - known_set
        if unknown:
            raise RuntimeError(
                "schema_migrations contains version(s) unknown to this "
                f"build: {sorted(unknown)}. Either upgrade the code or "
                "roll back the database. Refusing to start."
            )

        # Apply the rest in order.
        for path in files:
            version = _version_of(path)
            if version in db_versions:
                continue
            sql = path.read_text()
            log.info("applying migration %s", version)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations(version) VALUES ($1)",
                    version,
                )
            applied_now.append(version)

    return applied_now
