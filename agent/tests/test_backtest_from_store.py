"""Tests for the DB → parquet exporter (`data-store.8`)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from agent.backtest.from_store import export_to_parquet, fetch_cycles_df
from agent.sandbox.store.pool import open_pool
from agent.sandbox.store.schema import apply_migrations
from agent.sandbox.store.writer import record_cycle


def _outcome(ts_stem: str, **overrides) -> dict:
    base = {
        "started_at": f"2026-05-29T{ts_stem[9:11]}:{ts_stem[11:13]}:{ts_stem[13:15]}+00:00",
        "finished_at": f"2026-05-29T{ts_stem[9:11]}:{ts_stem[11:13]}:{ts_stem[13:15]}+00:00",
        "snapshot_filename": f"{ts_stem}.json",
        "decision_filename": f"{ts_stem}.json",
        "result": "executed",
        "wake_reason": "heartbeat",
        "confidence": 0.7,
        "expected_apr_pct": 4.5,
        "actions_planned": 1,
        "actions_executed": 1,
    }
    base.update(overrides)
    return base


def _snapshot() -> dict:
    return {
        "captured_at": "2026-05-29T16:00:00+00:00",
        "wallet": {"total_equity_usd": "200"},
        "earn_positions": [
            {"productId": "1131", "coin": "USD1", "amount": "100",
             "category": "FlexibleSaving"},
        ],
        "lm_positions": [], "alpha_positions": [], "perp_positions": [],
        "products": {},
    }


def _decision(
    thesis: str = "test thesis",
    confidence: float = 0.7,
    venues: list | None = None,
) -> dict:
    if venues is None:
        venues = [
            {"venue_id": "cash_usdc", "weight": 0.3},
            {"venue_id": "bybit_flex", "weight": 0.6,
             "picks": [{"product_id": "1131", "weight": 1.0}]},
            {"venue_id": "bybit_onchain", "weight": 0.1},
        ]
    return {
        "thesis": thesis,
        "venues": venues,
        "hedges": [],
        "confidence": confidence,
        "risk_flags": [],
        "notes": [],
        "expected_blended_apr_pct": 4.5,
        "_validator": {"ok": True, "errors": []},
    }


@pytest.mark.asyncio
async def test_export_writes_both_parquet_files(
    fresh_db_dsn: str, tmp_path: Path
) -> None:
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        await record_cycle(
            pool,
            outcome=_outcome("20260529T160211Z"),
            raw_snapshot=_snapshot(),
            raw_decision=_decision(),
        )
        await record_cycle(
            pool,
            outcome=_outcome("20260529T170000Z",
                             wake_reason="event:price_drift",
                             confidence=0.85, result="ok"),
            raw_snapshot=_snapshot(),
            raw_decision=_decision(thesis="post-drift exit",
                                   confidence=0.85),
        )
        out = await export_to_parquet(pool, tmp_path)
        assert out["cycles"].exists()
        assert out["positions"].exists()

    cycles = pd.read_parquet(out["cycles"])
    assert len(cycles) == 2
    expected_cols = {
        "cycle_ts", "started_at", "finished_at", "result", "wake_reason",
        "confidence", "expected_apr_pct", "actions_planned",
        "actions_executed", "validator_ok", "error", "thesis", "n_venues",
        "top1_venue", "top1_weight",
        "top2_venue", "top2_weight",
        "top3_venue", "top3_weight",
    }
    assert expected_cols.issubset(set(cycles.columns))

    # Sorted ascending by started_at
    assert cycles.iloc[0]["wake_reason"] == "heartbeat"
    assert cycles.iloc[1]["wake_reason"] == "event:price_drift"
    # Top venue by weight should be bybit_flex (0.6)
    assert cycles.iloc[0]["top1_venue"] == "bybit_flex"
    assert cycles.iloc[0]["top1_weight"] == pytest.approx(0.6)
    assert cycles.iloc[0]["n_venues"] == 3
    # Validator ok extracted
    assert bool(cycles.iloc[0]["validator_ok"]) is True

    positions = pd.read_parquet(out["positions"])
    # One earn position per cycle (USD1 amount=100)
    assert len(positions) == 2
    assert set(positions["coin"].unique()) == {"USD1"}
    # Amount preserved as string for NUMERIC fidelity
    assert positions.iloc[0]["amount"] == "100.000000000000000000"


@pytest.mark.asyncio
async def test_export_since_filter_applies_to_both_files(
    fresh_db_dsn: str, tmp_path: Path
) -> None:
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        await record_cycle(
            pool, outcome=_outcome("20260529T140000Z"),
            raw_snapshot=_snapshot(), raw_decision=_decision(),
        )
        await record_cycle(
            pool, outcome=_outcome("20260529T180000Z"),
            raw_snapshot=_snapshot(), raw_decision=_decision(),
        )
        cutoff = datetime(2026, 5, 29, 17, 0, tzinfo=UTC)
        out = await export_to_parquet(pool, tmp_path, since=cutoff)
    cycles = pd.read_parquet(out["cycles"])
    positions = pd.read_parquet(out["positions"])
    assert len(cycles) == 1
    assert len(positions) == 1
    # 18:00 row survives, 14:00 was filtered out
    assert "18:00" in str(cycles.iloc[0]["started_at"])


@pytest.mark.asyncio
async def test_export_empty_db_writes_empty_files(
    fresh_db_dsn: str, tmp_path: Path
) -> None:
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        out = await export_to_parquet(pool, tmp_path)
    cycles = pd.read_parquet(out["cycles"])
    positions = pd.read_parquet(out["positions"])
    assert len(cycles) == 0
    assert len(positions) == 0


@pytest.mark.asyncio
async def test_export_stem_suffix_appears_in_filenames(
    fresh_db_dsn: str, tmp_path: Path
) -> None:
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        out = await export_to_parquet(pool, tmp_path, stem="20260529")
    assert out["cycles"].name == "cycles_20260529.parquet"
    assert out["positions"].name == "positions_20260529.parquet"


@pytest.mark.asyncio
async def test_fetch_cycles_df_handles_missing_decision_payload(
    fresh_db_dsn: str,
) -> None:
    """An error cycle that never wrote a decision row → fetch_cycles_df
    returns the row with empty thesis + n_venues=0, NOT a crash."""
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        await record_cycle(
            pool,
            outcome={
                "started_at": datetime(2026, 5, 29, 16, 0, tzinfo=UTC).isoformat(),
                "finished_at": datetime(2026, 5, 29, 16, 0, tzinfo=UTC).isoformat(),
                "result": "error",
                "wake_reason": "heartbeat",
                "error": "Bybit auth blew up",
            },
        )
        df = await fetch_cycles_df(pool)
    assert len(df) == 1
    assert df.iloc[0]["result"] == "error"
    assert df.iloc[0]["thesis"] == ""
    assert df.iloc[0]["n_venues"] == 0
    assert df.iloc[0]["top1_venue"] is None
