"""One-shot backfill: existing JSON files → Postgres store
(`data-store.7`).

Scans the canonical sandbox directories — `cycle_log.jsonl`,
`snapshots/`, `decisions/`, `events/` — and replays them through
`record_cycle()` and `record_event()`. Idempotent for cycles via
`ON CONFLICT (cycle_ts) DO NOTHING`; events have no natural key, so
re-running with `--include-events` duplicates them (acceptable for a
one-shot, but call out in operator docs).

Run once on first migration so the web view shows historical data,
not just cycles after the data-store epic landed.

CLI:
    # cycles only (safe to re-run)
    python -m agent.sandbox.store.backfill --database-url $DATABASE_URL

    # cycles + events (re-runs duplicate events)
    python -m agent.sandbox.store.backfill --include-events

    # dry-run — scan + count, no DB writes
    python -m agent.sandbox.store.backfill --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg

from agent.sandbox.store.pool import open_pool
from agent.sandbox.store.schema import apply_migrations
from agent.sandbox.store.writer import record_cycle, record_event

log = logging.getLogger(__name__)

DEFAULT_SANDBOX_DIR = Path(__file__).parent.parent  # agent/sandbox/


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield non-empty dict rows from a JSONL file. Malformed lines are
    skipped with a warning rather than crashing the backfill."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            log.warning("skipping malformed JSON in %s: %s", path.name, e)
            continue
        if isinstance(row, dict):
            yield row


def _maybe_load_json(path: Path) -> dict[str, Any] | None:
    """Load a JSON file if it exists. Returns None on missing file or
    malformed JSON — caller is expected to treat as "no data" and
    pass through to `record_cycle` which handles None args."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        log.warning("malformed JSON at %s: %s", path, e)
        return None


async def backfill(
    pool: asyncpg.Pool,
    *,
    sandbox_dir: Path = DEFAULT_SANDBOX_DIR,
    include_events: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    """Replay file-based history into the DB. Returns counts."""
    stats = {
        "cycles_processed": 0,
        "cycles_inserted": 0,
        "cycles_skipped": 0,
        "events_processed": 0,
        "events_inserted": 0,
    }
    cycle_log = sandbox_dir / "state" / "cycle_log.jsonl"
    snapshots_dir = sandbox_dir / "snapshots"
    decisions_dir = sandbox_dir / "decisions"
    events_dir = sandbox_dir / "events"

    for outcome in _iter_jsonl(cycle_log):
        stats["cycles_processed"] += 1
        snap = _maybe_load_json(
            snapshots_dir / (outcome.get("snapshot_filename") or "__missing__")
        )
        dec = _maybe_load_json(
            decisions_dir / (outcome.get("decision_filename") or "__missing__")
        )
        if dry_run:
            continue
        ok = await record_cycle(
            pool, outcome=outcome, raw_snapshot=snap, raw_decision=dec
        )
        if ok:
            stats["cycles_inserted"] += 1
        else:
            stats["cycles_skipped"] += 1

    if include_events and events_dir.is_dir():
        for jsonl_path in sorted(events_dir.glob("*.jsonl")):
            for event in _iter_jsonl(jsonl_path):
                stats["events_processed"] += 1
                if dry_run:
                    continue
                try:
                    ev_id = await record_event(pool, event)
                except Exception as e:  # noqa: BLE001 — keep going on bad rows
                    log.warning("record_event failed: %s — skipping", e)
                    continue
                if ev_id is not None:
                    stats["events_inserted"] += 1

    return stats


def _format_stats(stats: dict[str, int]) -> str:
    return (
        f"  cycles_processed = {stats['cycles_processed']:>6}\n"
        f"  cycles_inserted  = {stats['cycles_inserted']:>6}\n"
        f"  cycles_skipped   = {stats['cycles_skipped']:>6}  "
        f"(already in DB → ON CONFLICT DO NOTHING)\n"
        f"  events_processed = {stats['events_processed']:>6}\n"
        f"  events_inserted  = {stats['events_inserted']:>6}"
    )


async def _async_main(args: argparse.Namespace) -> None:
    sandbox_dir = Path(args.sandbox_dir).resolve()
    if args.dry_run:
        # Dry-run still needs a pool to satisfy the signature, but we
        # never call any write — fake it with the no-op None-pool path.
        # Simpler: build a no-op pool by calling backfill with a real
        # pool but the dry_run flag. We still need a pool object; open
        # one to keep DB-shape paths consistent.
        pass
    async with open_pool(args.database_url) as pool:
        if not args.skip_migrations:
            applied = await apply_migrations(pool)
            log.info("migrations applied this run: %s", applied or "none")
        stats = await backfill(
            pool,
            sandbox_dir=sandbox_dir,
            include_events=args.include_events,
            dry_run=args.dry_run,
        )
    print("backfill complete:")
    print(_format_stats(stats))


def _main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Replay file-based cycle/event history into the Postgres "
            "cycle store. Idempotent for cycles (ON CONFLICT DO "
            "NOTHING); events duplicate on re-run."
        )
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres DSN. Falls back to DATABASE_URL env when omitted.",
    )
    parser.add_argument(
        "--sandbox-dir",
        default=str(DEFAULT_SANDBOX_DIR),
        help=f"Source directory (default {DEFAULT_SANDBOX_DIR}).",
    )
    parser.add_argument(
        "--include-events",
        action="store_true",
        default=True,
        help="Backfill events from events/*.jsonl (default: yes).",
    )
    parser.add_argument(
        "--no-events",
        dest="include_events",
        action="store_false",
        help="Skip event backfill (only cycles).",
    )
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help=(
            "Don't apply migrations on startup. Use when the DB schema "
            "was already set up by the agent loop or another runner."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan files and count, do not write anything.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    _main()
