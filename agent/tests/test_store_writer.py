"""Tests for the cycle writer (`data-store.3`).

Uses the session-scoped Postgres container + per-test fresh DB
fixtures from `conftest.py`. Each test applies the canonical
migrations against its fresh DB, then exercises `record_cycle` with
crafted outcomes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agent.sandbox.store.pool import open_pool
from agent.sandbox.store.schema import apply_migrations
from agent.sandbox.store.writer import record_cycle


def _full_outcome() -> dict:
    """Outcome representing a healthy executed cycle. Mirrors the
    shape `run_one_cycle` produces."""
    return {
        "started_at": "2026-05-29T16:02:11.000000+00:00",
        "finished_at": "2026-05-29T16:02:14.500000+00:00",
        "snapshot_filename": "20260529T160211Z.json",
        "decision_filename": "20260529T160211Z.json",
        "result": "executed",
        "wake_reason": "heartbeat",
        "confidence": 0.7,
        "expected_apr_pct": 4.5,
        "actions_planned": 2,
        "actions_executed": 2,
        "actions": [
            {
                "kind": "subscribe_earn",
                "category": "FlexibleSaving",
                "product_id": "1131",
                "coin": "USD1",
                "amount": "50",
                "status": "ok",
                "error": None,
            },
            {
                "kind": "open_perp_hedge",
                "category": None,
                "product_id": None,
                "coin": "TON",
                "amount": "20",
                "status": "ok",
                "error": None,
            },
        ],
    }


def _full_snapshot() -> dict:
    return {
        "captured_at": "2026-05-29T16:02:11.000000+00:00",
        "wallet": {"total_equity_usd": "200"},
        "earn_positions": [
            {"productId": "1131", "coin": "USD1", "amount": "100",
             "category": "FlexibleSaving"},
            {"productId": "1", "coin": "USDT", "amount": "0"},  # zero — skipped
        ],
        "lm_positions": [
            {"positionId": "LM-9", "coin": "ETH", "baseAmount": "0.5"},
        ],
        "alpha_positions": [
            {"tokenCode": "DEX_123", "symbol": "FOO", "amount": "10"},
        ],
        "perp_positions": [
            {"symbol": "TONUSDT", "size": "12.0"},
        ],
        "products": {},
    }


def _full_decision() -> dict:
    return {
        "thesis": "test cycle",
        "venues": [{"venue_id": "cash_usdc", "weight": 1.0}],
        "hedges": [],
        "confidence": 0.7,
        "risk_flags": [],
        "notes": [],
        "expected_blended_apr_pct": 4.5,
        "_meta": {
            "wake_reason": "heartbeat",
            "snapshot_filename": "20260529T160211Z.json",
        },
    }


@pytest.mark.asyncio
async def test_record_cycle_writes_all_tables(fresh_db_dsn: str) -> None:
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        ok = await record_cycle(
            pool,
            outcome=_full_outcome(),
            raw_snapshot=_full_snapshot(),
            raw_decision=_full_decision(),
        )
        assert ok is True

        async with pool.acquire() as conn:
            cycle = await conn.fetchrow("SELECT * FROM cycles")
            assert cycle["result"] == "executed"
            assert cycle["wake_reason"] == "heartbeat"
            assert cycle["confidence"] == pytest.approx(0.7)
            assert cycle["actions_planned"] == 2
            cycle_ts = cycle["cycle_ts"]

            snap = await conn.fetchrow("SELECT * FROM snapshots")
            assert snap["cycle_ts"] == cycle_ts
            assert snap["payload"]["wallet"]["total_equity_usd"] == "200"

            dec = await conn.fetchrow("SELECT * FROM decisions")
            assert dec["cycle_ts"] == cycle_ts
            assert dec["payload"]["confidence"] == 0.7

            positions = await conn.fetch(
                "SELECT venue, product_id, coin, amount, amount_usd "
                "FROM positions_snapshot ORDER BY venue, product_id"
            )
            venues = sorted({r["venue"] for r in positions})
            # Earn rows are split by Bybit `category` into venue-specific
            # buckets so the web doesn't have to know about Bybit's
            # internal taxonomy. FlexibleSaving → bybit_flex, etc.
            assert venues == ["bybit_alpha", "bybit_flex", "bybit_lm", "perp"]
            # Zero-amount USDT row from earn_positions was skipped
            earn_rows = [r for r in positions if r["venue"] == "bybit_flex"]
            assert len(earn_rows) == 1
            assert earn_rows[0]["coin"] == "USD1"
            assert earn_rows[0]["amount"] == Decimal("100")
            # USD1 is a stablecoin → priced 1:1 USD.
            assert earn_rows[0]["amount_usd"] == Decimal("100")

            execs = await conn.fetch(
                "SELECT idx, status, action FROM executions ORDER BY idx"
            )
            assert [r["idx"] for r in execs] == [0, 1]
            assert all(r["status"] == "ok" for r in execs)
            assert execs[0]["action"]["kind"] == "subscribe_earn"


@pytest.mark.asyncio
async def test_record_cycle_idempotent_on_repeat(fresh_db_dsn: str) -> None:
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        outcome = _full_outcome()
        first = await record_cycle(
            pool, outcome=outcome, raw_snapshot=_full_snapshot(),
            raw_decision=_full_decision(),
        )
        second = await record_cycle(
            pool, outcome=outcome, raw_snapshot=_full_snapshot(),
            raw_decision=_full_decision(),
        )
        assert first is True
        assert second is False  # ON CONFLICT DO NOTHING → skip signaled

        async with pool.acquire() as conn:
            n_cycles = await conn.fetchval("SELECT COUNT(*) FROM cycles")
            n_snaps = await conn.fetchval("SELECT COUNT(*) FROM snapshots")
            n_positions = await conn.fetchval(
                "SELECT COUNT(*) FROM positions_snapshot"
            )
            n_execs = await conn.fetchval("SELECT COUNT(*) FROM executions")
        assert n_cycles == 1
        assert n_snaps == 1
        # 1 earn (non-zero) + 1 lm + 1 alpha + 1 perp = 4 positions
        assert n_positions == 4
        assert n_execs == 2


@pytest.mark.asyncio
async def test_record_cycle_handles_error_outcome_no_snapshot(
    fresh_db_dsn: str,
) -> None:
    """A cycle that crashed before `collect_snapshot` returned has no
    snapshot_filename. The cycles row should still land (audit trail)
    and snapshots/decisions stay empty."""
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        outcome = {
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "result": "error",
            "wake_reason": "heartbeat",
            "error": "RuntimeError: Bybit auth blew up",
        }
        ok = await record_cycle(pool, outcome=outcome)
        assert ok is True

        async with pool.acquire() as conn:
            cycle = await conn.fetchrow("SELECT * FROM cycles")
            assert cycle["result"] == "error"
            assert "Bybit auth blew up" in cycle["error"]
            assert await conn.fetchval("SELECT COUNT(*) FROM snapshots") == 0
            assert await conn.fetchval("SELECT COUNT(*) FROM decisions") == 0
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM positions_snapshot"
            ) == 0


@pytest.mark.asyncio
async def test_record_cycle_skips_when_no_resolvable_cycle_ts(
    fresh_db_dsn: str,
) -> None:
    """If outcome has neither a snapshot_filename nor a parseable
    started_at, record_cycle returns False without inserting."""
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        ok = await record_cycle(pool, outcome={"result": "error"})
        assert ok is False
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM cycles") == 0


@pytest.mark.asyncio
async def test_record_cycle_extracts_wake_event_reason(
    fresh_db_dsn: str,
) -> None:
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        outcome = _full_outcome()
        outcome["wake_reason"] = "event:price_drift"
        ok = await record_cycle(
            pool,
            outcome=outcome,
            raw_snapshot=_full_snapshot(),
            raw_decision=_full_decision(),
        )
        assert ok is True
        async with pool.acquire() as conn:
            wake_reason = await conn.fetchval(
                "SELECT wake_reason FROM cycles"
            )
        assert wake_reason == "event:price_drift"


# ───────────────── event writer + cross-link (.4) ─────────────────────


def _sample_event(kind: str = "price_drift", severity: str = "P0") -> dict:
    return {
        "ts": "2026-05-29T16:02:11.000000+00:00",
        "kind": kind,
        "severity": severity,
        "position_id": "perp:TONUSDT",
        "coin": "TON",
        "baseline": {"entry_mark_price": "1.78"},
        "current": {"mark_price": "1.65"},
        "threshold": {"max_drift_pct": "0.05"},
        "message": "TON mark drifted -7.30%",
    }


@pytest.mark.asyncio
async def test_record_event_inserts_and_returns_id(fresh_db_dsn: str) -> None:
    from agent.sandbox.store.writer import record_event

    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        ev_id = await record_event(pool, _sample_event())
        assert isinstance(ev_id, int) and ev_id > 0
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM events WHERE id = $1", ev_id)
        assert row["kind"] == "price_drift"
        assert row["severity"] == "P0"
        assert row["coin"] == "TON"
        assert row["payload"]["message"] == "TON mark drifted -7.30%"
        assert row["triggered_cycle_ts"] is None  # not yet linked


@pytest.mark.asyncio
async def test_record_event_returns_none_on_missing_ts(fresh_db_dsn: str) -> None:
    from agent.sandbox.store.writer import record_event

    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        ev = _sample_event()
        ev.pop("ts")
        result = await record_event(pool, ev)
        assert result is None
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM events")
        assert count == 0


@pytest.mark.asyncio
async def test_record_cycle_links_triggered_event_ids(fresh_db_dsn: str) -> None:
    """record_cycle(..., triggered_event_ids=[id1, id2]) stamps those
    events with triggered_cycle_ts inside the same transaction."""
    from agent.sandbox.store.writer import record_event

    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        id_a = await record_event(pool, _sample_event(kind="price_drift"))
        id_b = await record_event(
            pool,
            {**_sample_event(kind="funding_flip"),
             "ts": "2026-05-29T16:02:12.000000+00:00"},
        )
        id_unrelated = await record_event(
            pool,
            {**_sample_event(kind="peg_drift", severity="P1"),
             "ts": "2026-05-29T15:00:00.000000+00:00"},
        )

        ok = await record_cycle(
            pool,
            outcome=_full_outcome(),
            raw_snapshot=_full_snapshot(),
            raw_decision=_full_decision(),
            triggered_event_ids=[id_a, id_b],
        )
        assert ok is True

        async with pool.acquire() as conn:
            cycle_ts = await conn.fetchval("SELECT cycle_ts FROM cycles")
            linked = await conn.fetch(
                "SELECT id FROM events WHERE triggered_cycle_ts = $1 "
                "ORDER BY id",
                cycle_ts,
            )
            unrelated_row = await conn.fetchrow(
                "SELECT triggered_cycle_ts FROM events WHERE id = $1",
                id_unrelated,
            )

        assert [r["id"] for r in linked] == sorted([id_a, id_b])
        # The unrelated peg_drift event stays NULL — only the cycle's
        # own wake events were linked.
        assert unrelated_row["triggered_cycle_ts"] is None


@pytest.mark.asyncio
async def test_record_cycle_no_link_when_event_ids_empty(
    fresh_db_dsn: str,
) -> None:
    """triggered_event_ids=None and =[] both skip the UPDATE — no errors,
    no spurious linking."""
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        # None
        await record_cycle(
            pool,
            outcome=_full_outcome(),
            raw_snapshot=_full_snapshot(),
            raw_decision=_full_decision(),
            triggered_event_ids=None,
        )
        # [] — second call same cycle_ts hits the ON CONFLICT path,
        # which returns False before touching the UPDATE. To exercise
        # the empty-list branch cleanly use a separate fresh cycle.
        outcome2 = _full_outcome()
        outcome2["snapshot_filename"] = "20260529T170000Z.json"
        outcome2["started_at"] = "2026-05-29T17:00:00.000000+00:00"
        await record_cycle(
            pool,
            outcome=outcome2,
            raw_snapshot=_full_snapshot(),
            raw_decision=_full_decision(),
            triggered_event_ids=[],
        )
        async with pool.acquire() as conn:
            cycles_count = await conn.fetchval("SELECT COUNT(*) FROM cycles")
        assert cycles_count == 2
