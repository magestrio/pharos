"""Tests for the file → DB backfill (`data-store.7`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.sandbox.store.backfill import backfill
from agent.sandbox.store.pool import open_pool
from agent.sandbox.store.schema import apply_migrations


def _seed_sandbox(root: Path) -> None:
    """Lay out a minimal sandbox tree on disk: cycle_log + snapshots +
    decisions + events. Two cycles, two events."""
    snapshots_dir = root / "snapshots"
    decisions_dir = root / "decisions"
    events_dir = root / "events"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    decisions_dir.mkdir(parents=True, exist_ok=True)
    events_dir.mkdir(parents=True, exist_ok=True)

    cycle_a = {
        "started_at": "2026-05-29T16:02:11+00:00",
        "finished_at": "2026-05-29T16:02:14+00:00",
        "snapshot_filename": "20260529T160211Z.json",
        "decision_filename": "20260529T160211Z.json",
        "result": "executed",
        "wake_reason": "heartbeat",
        "confidence": 0.7,
        "expected_apr_pct": 4.5,
        "actions_planned": 1,
        "actions_executed": 1,
        "actions": [
            {"kind": "subscribe_earn", "category": "FlexibleSaving",
             "product_id": "1131", "coin": "USD1", "amount": "50",
             "status": "ok", "error": None}
        ],
    }
    cycle_b = {
        "started_at": "2026-05-29T17:00:00+00:00",
        "finished_at": "2026-05-29T17:00:03+00:00",
        "snapshot_filename": "20260529T170000Z.json",
        "decision_filename": "20260529T170000Z.json",
        "result": "ok",
        "wake_reason": "event:price_drift",
        "confidence": 0.8,
        "expected_apr_pct": 5.1,
        "actions_planned": 0,
        "actions_executed": 0,
    }
    (root / "cycle_log.jsonl").write_text(
        json.dumps(cycle_a) + "\n" + json.dumps(cycle_b) + "\n"
    )

    snap = {
        "captured_at": "2026-05-29T16:02:11+00:00",
        "wallet": {"total_equity_usd": "200"},
        "earn_positions": [
            {"productId": "1131", "coin": "USD1", "amount": "100",
             "category": "FlexibleSaving"},
        ],
        "lm_positions": [],
        "alpha_positions": [],
        "perp_positions": [],
        "products": {},
    }
    (snapshots_dir / "20260529T160211Z.json").write_text(json.dumps(snap))
    (snapshots_dir / "20260529T170000Z.json").write_text(json.dumps(snap))

    dec = {
        "thesis": "test",
        "venues": [{"venue_id": "cash_usdc", "weight": 1.0}],
        "hedges": [],
        "confidence": 0.7,
        "risk_flags": [],
        "notes": [],
        "expected_blended_apr_pct": 4.5,
    }
    (decisions_dir / "20260529T160211Z.json").write_text(json.dumps(dec))
    (decisions_dir / "20260529T170000Z.json").write_text(json.dumps(dec))

    events = [
        {
            "ts": "2026-05-29T16:55:00+00:00", "kind": "price_drift",
            "severity": "P0", "position_id": "perp:TONUSDT", "coin": "TON",
            "baseline": {}, "current": {}, "threshold": {},
            "message": "TON drifted -7%",
        },
        {
            "ts": "2026-05-29T16:56:00+00:00", "kind": "funding_flip",
            "severity": "P0", "position_id": "perp:TONUSDT", "coin": "TON",
            "baseline": {}, "current": {}, "threshold": {},
            "message": "funding flipped",
        },
    ]
    (events_dir / "20260529.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )


@pytest.mark.asyncio
async def test_backfill_happy_path(fresh_db_dsn: str, tmp_path: Path) -> None:
    _seed_sandbox(tmp_path)
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        stats = await backfill(pool, sandbox_dir=tmp_path)

        assert stats["cycles_processed"] == 2
        assert stats["cycles_inserted"] == 2
        assert stats["cycles_skipped"] == 0
        assert stats["events_processed"] == 2
        assert stats["events_inserted"] == 2

        async with pool.acquire() as conn:
            cycles = await conn.fetch(
                "SELECT wake_reason, result FROM cycles ORDER BY started_at"
            )
            assert [(r["wake_reason"], r["result"]) for r in cycles] == [
                ("heartbeat", "executed"),
                ("event:price_drift", "ok"),
            ]
            positions = await conn.fetchval(
                "SELECT COUNT(*) FROM positions_snapshot"
            )
            assert positions == 2  # one earn position per cycle
            execs = await conn.fetchval(
                "SELECT COUNT(*) FROM executions"
            )
            assert execs == 1  # only cycle_a had an action
            events = await conn.fetch(
                "SELECT kind FROM events ORDER BY event_ts"
            )
            assert [r["kind"] for r in events] == [
                "price_drift", "funding_flip"
            ]


@pytest.mark.asyncio
async def test_backfill_cycles_idempotent(
    fresh_db_dsn: str, tmp_path: Path
) -> None:
    _seed_sandbox(tmp_path)
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        await backfill(pool, sandbox_dir=tmp_path, include_events=False)
        # Re-run — cycles all skipped via ON CONFLICT
        stats = await backfill(pool, sandbox_dir=tmp_path, include_events=False)
        assert stats["cycles_processed"] == 2
        assert stats["cycles_inserted"] == 0
        assert stats["cycles_skipped"] == 2
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM cycles")
        assert count == 2


@pytest.mark.asyncio
async def test_backfill_no_events_flag(
    fresh_db_dsn: str, tmp_path: Path
) -> None:
    _seed_sandbox(tmp_path)
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        stats = await backfill(pool, sandbox_dir=tmp_path, include_events=False)
        assert stats["events_processed"] == 0
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM events")
        assert count == 0


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_write(
    fresh_db_dsn: str, tmp_path: Path
) -> None:
    _seed_sandbox(tmp_path)
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        stats = await backfill(pool, sandbox_dir=tmp_path, dry_run=True)
        # Counts still update (dry-run measures what WOULD happen)
        assert stats["cycles_processed"] == 2
        assert stats["events_processed"] == 2
        # No inserts performed
        assert stats["cycles_inserted"] == 0
        assert stats["events_inserted"] == 0
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM cycles") == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM events") == 0


@pytest.mark.asyncio
async def test_backfill_missing_files_gracefully_handled(
    fresh_db_dsn: str, tmp_path: Path
) -> None:
    """Cycle log row points at a snapshot file that doesn't exist on
    disk — backfill should still insert the cycles row with snapshot=
    NULL rather than crash."""
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "decisions").mkdir()
    cycle = {
        "started_at": "2026-05-29T16:02:11+00:00",
        "finished_at": "2026-05-29T16:02:14+00:00",
        "snapshot_filename": "missing.json",
        "decision_filename": "missing.json",
        "result": "ok",
        "wake_reason": "heartbeat",
        "confidence": 0.5,
        "expected_apr_pct": 0.0,
    }
    (tmp_path / "cycle_log.jsonl").write_text(json.dumps(cycle) + "\n")

    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        stats = await backfill(pool, sandbox_dir=tmp_path)
        assert stats["cycles_inserted"] == 1
        async with pool.acquire() as conn:
            snap = await conn.fetchval("SELECT COUNT(*) FROM snapshots")
            dec = await conn.fetchval("SELECT COUNT(*) FROM decisions")
        # No snapshot/decision row when the file is missing
        assert snap == 0
        assert dec == 0


@pytest.mark.asyncio
async def test_backfill_skips_malformed_jsonl_lines(
    fresh_db_dsn: str, tmp_path: Path
) -> None:
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "decisions").mkdir()
    good_cycle = {
        "started_at": "2026-05-29T16:02:11+00:00",
        "finished_at": "2026-05-29T16:02:14+00:00",
        "snapshot_filename": "x.json",
        "decision_filename": "x.json",
        "result": "ok",
        "wake_reason": "heartbeat",
    }
    (tmp_path / "cycle_log.jsonl").write_text(
        "not json at all\n\n"
        + json.dumps(good_cycle) + "\n"
        + "{also broken\n"
    )

    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        stats = await backfill(pool, sandbox_dir=tmp_path, include_events=False)
        # Only the one valid row counted
        assert stats["cycles_processed"] == 1
        assert stats["cycles_inserted"] == 1
