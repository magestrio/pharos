"""Tests for the event watcher (`event-driven-rebalance.2`).

Strategy: each checker is a pure function (HeldPosition + current value
→ EventRecord | None), so threshold tests don't need any client mocks.
`poll_once` is exercised with a mocked Bybit client + monkey-patched
peg fetcher.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent.sandbox import watcher
from agent.sandbox.watcher import (
    EventRecord,
    HeldPosition,
    Thresholds,
    WatcherBaseline,
    _perp_to_paired_position,
    check_da_settlement,
    check_funding_flip,
    check_lm_liq_distance,
    check_new_hold_to_earn,
    check_peg_drift,
    check_perp_liq_distance,
    check_price_drift,
    check_yield_jump,
    poll_once,
    prune_closed_positions,
    read_baseline,
    update_baseline_from_snapshot,
    write_baseline,
    write_events,
)
from types import SimpleNamespace

# ───────────────────────── price drift (event #1) ─────────────────────

def _hedged_pos(entry: str = "100.0") -> HeldPosition:
    return HeldPosition(
        position_id="perp:TONUSDT",
        venue="perp",
        coin="TON",
        entry_mark_price=Decimal(entry),
    )


def test_price_drift_below_threshold_does_not_fire():
    # 4% drift, threshold 5% — no event
    assert check_price_drift(_hedged_pos("100"), Decimal("104")) is None
    assert check_price_drift(_hedged_pos("100"), Decimal("96")) is None


def test_price_drift_at_or_above_threshold_fires_p0():
    ev = check_price_drift(_hedged_pos("100"), Decimal("105"))
    assert ev is not None
    assert ev.kind == "price_drift"
    assert ev.severity == "P0"
    assert ev.coin == "TON"
    ev_down = check_price_drift(_hedged_pos("100"), Decimal("94.99"))
    assert ev_down is not None
    assert ev_down.severity == "P0"


def test_price_drift_skips_stables():
    stable = HeldPosition(
        position_id="earn:1",
        venue="earn",
        coin="USDC",
        entry_mark_price=Decimal("1.0"),
    )
    # +10% would be massive but stable filter takes precedence
    assert check_price_drift(stable, Decimal("1.10")) is None


def test_price_drift_no_baseline_returns_none():
    pos = HeldPosition(position_id="perp:X", venue="perp", coin="X")
    assert check_price_drift(pos, Decimal("100")) is None


# ───────────────────────── funding flip (event #2) ────────────────────

def _funded_pos(rate: str) -> HeldPosition:
    return HeldPosition(
        position_id="perp:TONUSDT",
        venue="perp",
        coin="TON",
        last_funding_rate=Decimal(rate),
    )


def test_funding_flip_fires_on_sign_change():
    ev = check_funding_flip(_funded_pos("0.0005"), Decimal("-0.0003"))
    assert ev is not None
    assert ev.kind == "funding_flip"
    assert ev.severity == "P0"


def test_funding_flip_no_event_when_same_sign():
    assert check_funding_flip(_funded_pos("0.0005"), Decimal("0.0010")) is None
    assert check_funding_flip(_funded_pos("-0.0005"), Decimal("-0.0001")) is None


def test_funding_flip_epsilon_filter_suppresses_noise():
    # Both sides inside ±epsilon — flip through zero is meaningless
    assert check_funding_flip(_funded_pos("0.00005"), Decimal("-0.00005")) is None
    # Baseline strong, current sub-epsilon → still suppressed (rate is
    # decaying through zero, not a real flip)
    assert check_funding_flip(_funded_pos("0.0005"), Decimal("-0.00005")) is None


def test_funding_flip_skips_stables_and_missing_baseline():
    stable = HeldPosition(
        position_id="earn:USDC", venue="earn", coin="USDC",
        last_funding_rate=Decimal("0.0005"),
    )
    assert check_funding_flip(stable, Decimal("-0.0005")) is None
    no_base = HeldPosition(position_id="perp:X", venue="perp", coin="X")
    assert check_funding_flip(no_base, Decimal("-0.0005")) is None


# ───────────────────────── peg drift (event #3) ───────────────────────

def test_peg_drift_below_50bps_does_not_fire():
    assert check_peg_drift(Decimal("0.9952")) is None  # -48 bps
    assert check_peg_drift(Decimal("1.0049")) is None  # +49 bps


def test_peg_drift_at_or_above_50bps_fires_p0():
    ev_down = check_peg_drift(Decimal("0.994"))  # -60 bps
    assert ev_down is not None and ev_down.severity == "P0"
    ev_up = check_peg_drift(Decimal("1.0051"))  # +51 bps
    assert ev_up is not None and ev_up.severity == "P0"


# ───────────────────────── DA settlement (event #4) ───────────────────

def _da_pos(settle_ts: int) -> HeldPosition:
    return HeldPosition(
        position_id="advance_earn:DA-1",
        venue="advance_earn",
        coin="BTC",
        settle_time_ts=settle_ts,
    )


def test_da_settlement_no_event_when_window_wide():
    now = 1_700_000_000
    far = now + 60 * 60  # 1h away → outside 30min window
    assert check_da_settlement(_da_pos(far), now) is None


def test_da_settlement_p1_within_30min_p0_within_10min():
    now = 1_700_000_000
    in_25min = now + 25 * 60
    ev = check_da_settlement(_da_pos(in_25min), now)
    assert ev is not None and ev.severity == "P1"
    in_5min = now + 5 * 60
    ev2 = check_da_settlement(_da_pos(in_5min), now)
    assert ev2 is not None and ev2.severity == "P0"


def test_da_settlement_skips_past_settle_time():
    now = 1_700_000_000
    assert check_da_settlement(_da_pos(now - 1), now) is None


# ───────────────────────── new H2E (event #5) ─────────────────────────

def test_new_h2e_fires_on_new_id():
    ev = check_new_hold_to_earn(["A", "B"], ["A", "B", "C"])
    assert ev is not None
    assert ev.severity == "P1"
    assert "C" in ev.current["new_ids"]


def test_new_h2e_no_event_when_unchanged_or_shrinking():
    assert check_new_hold_to_earn(["A", "B"], ["A", "B"]) is None
    assert check_new_hold_to_earn(["A", "B"], ["A"]) is None


# ───────────────────────── yield jump (event #6) ──────────────────────

def _earn_pos(baseline_bps: str) -> HeldPosition:
    return HeldPosition(
        position_id="earn:1131",
        venue="earn",
        coin="USD1",
        last_measured_yield_bps=Decimal(baseline_bps),
    )


def test_yield_jump_fires_above_2x_when_baseline_meaningful():
    ev = check_yield_jump(_earn_pos("600"), Decimal("1500"))  # 2.5x of 600
    assert ev is not None and ev.severity == "P1"


def test_yield_jump_suppressed_below_min_baseline():
    # 1bps → 10bps is 10x but baseline below noise floor → no event
    assert check_yield_jump(_earn_pos("100"), Decimal("1000")) is None


def test_yield_jump_no_event_at_just_below_2x():
    assert check_yield_jump(_earn_pos("600"), Decimal("1199")) is None  # 1.998x


# ───────────────────────── LM liq distance (event #7) ─────────────────

def _lm_pos() -> HeldPosition:
    return HeldPosition(
        position_id="lm:LM-9",
        venue="lm",
        coin="ETH",
        last_liq_distance=Decimal("0.25"),
    )


def test_lm_liq_distance_no_event_when_safe():
    assert check_lm_liq_distance(_lm_pos(), Decimal("0.20")) is None
    assert check_lm_liq_distance(_lm_pos(), Decimal("0.11")) is None


def test_lm_liq_distance_fires_p0_below_10pct():
    ev = check_lm_liq_distance(_lm_pos(), Decimal("0.10"))
    assert ev is not None and ev.severity == "P0"
    ev2 = check_lm_liq_distance(_lm_pos(), Decimal("0.05"))
    assert ev2 is not None and ev2.severity == "P0"


# ───────────────────────── perp liq distance (event #8) ────────────────

def _perp_short_pos() -> HeldPosition:
    return HeldPosition(
        position_id="perp:LITUSDT",
        venue="perp",
        coin="LIT",
        entry_mark_price=Decimal("1.71"),
        last_liq_distance=Decimal("0.95"),
    )


def test_perp_liq_distance_safe_when_far():
    """Fresh 1x hedge: mark=$1.71, liq=$3.33 → distance ~0.95, no event."""
    pos = _perp_short_pos()
    ev = check_perp_liq_distance(pos, Decimal("1.71"), Decimal("3.33"))
    assert ev is None


def test_perp_liq_distance_safe_just_above_threshold():
    """Distance 0.51 — above 0.50 threshold, no event."""
    pos = _perp_short_pos()
    ev = check_perp_liq_distance(pos, Decimal("1.00"), Decimal("1.51"))
    assert ev is None


def test_perp_liq_distance_fires_p0_at_threshold():
    """Distance exactly 0.50 → fires (≤ threshold)."""
    pos = _perp_short_pos()
    ev = check_perp_liq_distance(pos, Decimal("1.00"), Decimal("1.50"))
    assert ev is not None
    assert ev.kind == "perp_liquidation_distance"
    assert ev.severity == "P0"
    assert ev.coin == "LIT"
    assert "LIT short liq distance" in ev.message


def test_perp_liq_distance_fires_p0_well_inside():
    """Distance 0.20 — well inside the close window."""
    pos = _perp_short_pos()
    ev = check_perp_liq_distance(pos, Decimal("2.00"), Decimal("2.40"))
    assert ev is not None and ev.severity == "P0"


def test_perp_liq_distance_skips_when_liq_below_mark():
    """Long-position shape (liq < mark) — we only auto-hedge with
    shorts; treat as no-signal rather than misfire."""
    pos = _perp_short_pos()
    ev = check_perp_liq_distance(pos, Decimal("2.00"), Decimal("1.00"))
    assert ev is None


def test_perp_liq_distance_skips_on_zero_mark():
    """Defensive — divide-by-zero guard."""
    pos = _perp_short_pos()
    ev = check_perp_liq_distance(pos, Decimal("0"), Decimal("1.50"))
    assert ev is None


def test_perp_liq_distance_skips_on_zero_liq():
    """Bybit returns empty string / 0 for fresh-flat / uncomputed rows.
    Should be a no-op, not a false P0."""
    pos = _perp_short_pos()
    ev = check_perp_liq_distance(pos, Decimal("1.50"), Decimal("0"))
    assert ev is None


# ───────────────────────── baseline IO ────────────────────────────────

def test_baseline_roundtrip(tmp_path: Path):
    path = tmp_path / "baseline.json"
    b = WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[_hedged_pos("100"), _lm_pos()],
        known_h2e_product_ids=["A", "B"],
    )
    write_baseline(b, path)
    loaded = read_baseline(path)
    assert loaded is not None
    assert len(loaded.positions) == 2
    assert loaded.known_h2e_product_ids == ["A", "B"]


def test_read_baseline_missing_returns_none(tmp_path: Path):
    assert read_baseline(tmp_path / "nope.json") is None


def test_read_baseline_corrupt_json_returns_none(tmp_path: Path):
    # state-5: a half-written / garbage file must degrade to None, not raise.
    path = tmp_path / "baseline.json"
    path.write_text("{not valid json")
    assert read_baseline(path) is None


def test_read_baseline_invalid_schema_returns_none(tmp_path: Path):
    # state-5: parseable JSON that doesn't match the model degrades too.
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"captured_at": "not-a-date", "positions": 42}))
    assert read_baseline(path) is None


# ───────────────────────── post-execute prune (watcher-2) ─────────────

def _ok_result(kind: str, **action_fields):
    action = SimpleNamespace(
        kind=kind,
        product_id=action_fields.get("product_id", ""),
        coin=action_fields.get("coin", ""),
        position_id=action_fields.get("position_id"),
    )
    return SimpleNamespace(status="ok", action=action)


def test_prune_closed_positions_drops_executed_exits():
    baseline = WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[
            _earn_pos("600"),          # earn:1131
            _lm_pos(),                 # lm:LM-9
            _hedged_pos("100"),        # perp:TONUSDT, coin TON
            HeldPosition(position_id="earn:9999", venue="earn", coin="USDT"),
        ],
        known_h2e_product_ids=["H1", "H2"],
    )
    results = [
        _ok_result("redeem_earn", product_id="1131"),
        _ok_result("redeem_lm", position_id="LM-9"),
        _ok_result("close_perp", coin="TON"),
    ]
    pruned = prune_closed_positions(baseline, results)
    kept_ids = {p.position_id for p in pruned.positions}
    assert kept_ids == {"earn:9999"}
    assert pruned.known_h2e_product_ids == ["H1", "H2"]


def test_prune_closed_positions_keeps_failed_and_unrelated():
    baseline = WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[_earn_pos("600"), _lm_pos()],
    )
    results = [
        SimpleNamespace(  # failed redeem — position still live, keep it
            status="error",
            action=SimpleNamespace(
                kind="redeem_earn", product_id="1131", coin="USD1",
                position_id=None,
            ),
        ),
        _ok_result("subscribe_earn", product_id="1131"),  # non-exit kind
    ]
    pruned = prune_closed_positions(baseline, results)
    assert {p.position_id for p in pruned.positions} == {"earn:1131", "lm:LM-9"}


def test_prune_closed_positions_drops_alpha_by_coin():
    baseline = WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[
            HeldPosition(position_id="alpha:WIF-token", venue="alpha", coin="WIF"),
            HeldPosition(position_id="earn:1", venue="earn", coin="USDC"),
        ],
    )
    pruned = prune_closed_positions(
        baseline, [_ok_result("alpha_redeem", coin="wif")]
    )
    assert {p.position_id for p in pruned.positions} == {"earn:1"}


def test_prune_closed_positions_noop_returns_same_object():
    baseline = WatcherBaseline(
        captured_at=datetime.now(UTC), positions=[_lm_pos()]
    )
    assert prune_closed_positions(baseline, []) is baseline


def test_update_baseline_from_snapshot_extracts_fields(tmp_path: Path):
    path = tmp_path / "baseline.json"
    snap = {
        "captured_at": "2026-05-29T16:00:00+00:00",
        "earn_positions": [
            {"productId": "1131", "coin": "USD1", "amount": "100",
             "measured_yield_bps": "750"},
            # Zero-amount row — excluded
            {"productId": "1", "coin": "USDT", "amount": "0"},
        ],
        "perp_positions": [
            {"symbol": "TONUSDT", "markPrice": "1.78",
             "fundingRate": "0.0002"},
        ],
        "lm_positions": [
            {"positionId": "LM-9", "coin": "ETH",
             "liquidation_distance_pct": "0.22"},
        ],
        "alpha_positions": [],
        "products": {"HoldToEarn": [{"product_id": "H2E-1"}]},
    }
    b = update_baseline_from_snapshot(snap, path=path)
    venues = {p.venue for p in b.positions}
    assert venues == {"earn", "perp", "lm"}
    assert "H2E-1" in b.known_h2e_product_ids
    # Atomic write left the file in place
    assert path.exists()
    raw = json.loads(path.read_text())
    assert raw["snapshot_filename"] is None


# ───────────────────────── poll_once integration ──────────────────────

class _FakeTicker:
    def __init__(self, symbol: str, mark: str, funding: str):
        self.symbol = symbol
        self.markPrice = mark
        self.fundingRate = funding

    def model_dump(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "markPrice": self.markPrice,
            "fundingRate": self.fundingRate,
        }


@pytest.mark.asyncio
async def test_poll_once_fires_price_drift_and_funding_flip(monkeypatch):
    """End-to-end: baseline has a TON perp at 100/+0.0005; tickers come
    back at 110/-0.0005 → both price drift and funding flip fire."""
    baseline = WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[
            HeldPosition(
                position_id="perp:TONUSDT",
                venue="perp",
                coin="TON",
                entry_mark_price=Decimal("100"),
                last_funding_rate=Decimal("0.0005"),
            )
        ],
        known_h2e_product_ids=["A"],
    )
    client = AsyncMock()
    client.get_tickers = AsyncMock(
        return_value=[_FakeTicker("TONUSDT", "110", "-0.0005")]
    )
    client.list_hold_to_earn_products = AsyncMock(
        return_value=[{"productId": "A"}]
    )

    # Suppress real network call to coingecko
    async def _peg_stub() -> Decimal | None:
        return Decimal("1.0")

    monkeypatch.setattr(watcher, "_fetch_peg_usd", _peg_stub)

    events = await poll_once(client, baseline)
    kinds = {e.kind for e in events}
    assert "price_drift" in kinds
    assert "funding_flip" in kinds
    # No peg drift (price exactly 1.0) and no new H2E (set unchanged)
    assert "peg_drift" not in kinds
    assert "new_hold_to_earn" not in kinds


@pytest.mark.asyncio
async def test_poll_once_no_positions_still_runs_global(monkeypatch):
    """Empty baseline: peg + H2E still polled, position-keyed checkers
    skipped."""
    baseline = WatcherBaseline(captured_at=datetime.now(UTC))
    client = AsyncMock()
    client.list_hold_to_earn_products = AsyncMock(
        return_value=[{"productId": "NEW-1"}]
    )

    async def _peg_stub() -> Decimal | None:
        return Decimal("0.99")  # -100 bps depeg

    monkeypatch.setattr(watcher, "_fetch_peg_usd", _peg_stub)
    events = await poll_once(client, baseline)
    kinds = {e.kind for e in events}
    assert kinds == {"peg_drift", "new_hold_to_earn"}
    # get_tickers NOT called because no non-stable coins to watch
    client.get_tickers.assert_not_called()


@pytest.mark.asyncio
async def test_poll_once_perp_liq_distance_fires_companion_pick_invalidated(monkeypatch):
    """A hedge short nearing liquidation fires `perp_liquidation_distance` AND a
    companion `pick_invalidated` for the PAIRED Earn → the loop's credit-free
    auto-close shuts BOTH legs without an LLM cycle."""
    from types import SimpleNamespace
    baseline = WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[
            HeldPosition(position_id="perp:IOUSDT", venue="perp", coin="IO",
                         entry_mark_price=Decimal("1.0")),
            HeldPosition(position_id="earn:407", venue="earn", coin="IO"),
        ],
        known_h2e_product_ids=["A"],
    )
    client = AsyncMock()
    client.get_tickers = AsyncMock(
        return_value=[_FakeTicker("IOUSDT", "1.0", "0.0001")]
    )
    client.list_hold_to_earn_products = AsyncMock(return_value=[{"productId": "A"}])
    # IO short: mark 1.0, liq 1.4 → distance 0.40 ≤ 0.50 threshold → Event #8.
    client.get_positions = AsyncMock(return_value=[
        SimpleNamespace(symbol="IOUSDT", side="Sell", size="50",
                        markPrice="1.0", liqPrice="1.4", coin="IO")
    ])

    async def _peg_stub() -> Decimal | None:
        return Decimal("1.0")

    monkeypatch.setattr(watcher, "_fetch_peg_usd", _peg_stub)

    events = await poll_once(client, baseline)
    kinds = [e.kind for e in events]
    assert "perp_liquidation_distance" in kinds
    companions = [
        e for e in events
        if e.kind == "pick_invalidated" and e.position_id == "earn:407"
    ]
    assert len(companions) == 1
    assert companions[0].severity == "P0"


def test_update_baseline_from_snapshot_extracts_lm_product_id(tmp_path: Path):
    """LM baseline rows carry the catalog productId so the perp→paired
    resolver can hand the auto-close path a pid the bybit_lm pick keys on."""
    path = tmp_path / "baseline.json"
    snap = {
        "captured_at": "2026-06-09T16:00:00+00:00",
        "lm_positions": [
            {"positionId": "LM-9", "productId": "24", "coin": "ETH",
             "liquidation_distance_pct": "0.22"},
        ],
    }
    b = update_baseline_from_snapshot(snap, path=path)
    lm = next(p for p in b.positions if p.venue == "lm")
    assert lm.position_id == "lm:LM-9"
    assert lm.product_id == "24"
    assert lm.coin == "ETH"


def test_perp_to_paired_position_resolves_earn_lm_none():
    """Resolver returns the productId of the paired Earn/LM (Earn wins when
    both held), or ('', '') for a coin with no held yield leg (carry)."""
    earn = HeldPosition(position_id="earn:407", venue="earn", coin="IO")
    lm = HeldPosition(position_id="lm:LM-9", venue="lm", coin="ETH",
                      product_id="24")
    base = WatcherBaseline(captured_at=datetime.now(UTC), positions=[earn, lm])
    assert _perp_to_paired_position("IOUSDT", base) == ("earn", "407")
    assert _perp_to_paired_position("ETHUSDT", base) == ("lm", "24")
    assert _perp_to_paired_position("SOLUSDT", base) == ("", "")
    # LM with no productId can't be matched back to a pick → not resolved.
    lm_no_pid = HeldPosition(position_id="lm:LM-1", venue="lm", coin="ARB")
    base2 = WatcherBaseline(captured_at=datetime.now(UTC), positions=[lm_no_pid])
    assert _perp_to_paired_position("ARBUSDT", base2) == ("", "")


@pytest.mark.asyncio
async def test_poll_once_lm_paired_perp_near_liq_fires_pick_invalidated(monkeypatch):
    """An LM base-leg hedge nearing liquidation fires a companion
    `pick_invalidated` carrying the LM PRODUCTID (not the positionId) so the
    auto-close drops the bybit_lm pick → REDEEM_LM."""
    from types import SimpleNamespace
    baseline = WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[
            HeldPosition(position_id="perp:ETHUSDT", venue="perp", coin="ETH",
                         entry_mark_price=Decimal("2000")),
            HeldPosition(position_id="lm:LM-9", venue="lm", coin="ETH",
                         product_id="24"),
        ],
        known_h2e_product_ids=["A"],
    )
    client = AsyncMock()
    client.get_tickers = AsyncMock(
        return_value=[_FakeTicker("ETHUSDT", "2000", "0.0001")]
    )
    client.list_hold_to_earn_products = AsyncMock(return_value=[{"productId": "A"}])
    # ETH short: mark 2000, liq 2800 → distance 0.40 ≤ 0.50 → Event #8.
    client.get_positions = AsyncMock(return_value=[
        SimpleNamespace(symbol="ETHUSDT", side="Sell", size="1",
                        markPrice="2000", liqPrice="2800", coin="ETH")
    ])

    async def _peg_stub() -> Decimal | None:
        return Decimal("1.0")

    monkeypatch.setattr(watcher, "_fetch_peg_usd", _peg_stub)

    events = await poll_once(client, baseline)
    assert "perp_liquidation_distance" in {e.kind for e in events}
    companions = [
        e for e in events
        if e.kind == "pick_invalidated" and e.position_id == "lm:24"
    ]
    assert len(companions) == 1
    assert companions[0].severity == "P0"


@pytest.mark.asyncio
async def test_poll_once_lm_paired_perp_gone_fires_pick_invalidated(monkeypatch):
    """LM base-leg hedge closed out by Bybit (no longer in the position list)
    → companion `pick_invalidated(lm:<productId>)` so the LP is redeemed."""
    baseline = WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[
            HeldPosition(position_id="perp:ETHUSDT", venue="perp", coin="ETH",
                         entry_mark_price=Decimal("2000")),
            HeldPosition(position_id="lm:LM-9", venue="lm", coin="ETH",
                         product_id="24"),
        ],
        known_h2e_product_ids=["A"],
    )
    client = AsyncMock()
    client.get_tickers = AsyncMock(
        return_value=[_FakeTicker("ETHUSDT", "2000", "0.0001")]
    )
    client.list_hold_to_earn_products = AsyncMock(return_value=[{"productId": "A"}])
    # No ETH short open anymore → "perp gone" branch.
    client.get_positions = AsyncMock(return_value=[])

    async def _peg_stub() -> Decimal | None:
        return Decimal("1.0")

    monkeypatch.setattr(watcher, "_fetch_peg_usd", _peg_stub)

    events = await poll_once(client, baseline)
    companions = [
        e for e in events
        if e.kind == "pick_invalidated" and e.position_id == "lm:24"
    ]
    assert len(companions) == 1
    assert companions[0].severity == "P0"


@pytest.mark.asyncio
async def test_poll_once_carry_perp_near_liq_fires_carry_liq_close(monkeypatch):
    """A near-liq perp with NO paired Earn/LM (funding-carry) fires a distinct
    `carry_liq_close` keyed by coin — NOT a pick_invalidated (no pick to drop).
    """
    from types import SimpleNamespace
    baseline = WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[
            HeldPosition(position_id="perp:SOLUSDT", venue="perp", coin="SOL",
                         entry_mark_price=Decimal("150")),
        ],
        known_h2e_product_ids=["A"],
    )
    client = AsyncMock()
    client.get_tickers = AsyncMock(
        return_value=[_FakeTicker("SOLUSDT", "150", "0.0001")]
    )
    client.list_hold_to_earn_products = AsyncMock(return_value=[{"productId": "A"}])
    client.get_positions = AsyncMock(return_value=[
        SimpleNamespace(symbol="SOLUSDT", side="Sell", size="10",
                        markPrice="150", liqPrice="210", coin="SOL")
    ])

    async def _peg_stub() -> Decimal | None:
        return Decimal("1.0")

    monkeypatch.setattr(watcher, "_fetch_peg_usd", _peg_stub)

    events = await poll_once(client, baseline)
    assert "perp_liquidation_distance" in {e.kind for e in events}
    carry = [e for e in events if e.kind == "carry_liq_close"]
    assert len(carry) == 1
    assert carry[0].coin == "SOL"
    assert carry[0].severity == "P0"
    assert not [e for e in events if e.kind == "pick_invalidated"]


# ───────────────────────── event sink ─────────────────────────────────

def test_write_events_appends_jsonl(tmp_path: Path):
    events = [
        EventRecord(
            ts=datetime.now(UTC),
            kind="price_drift",
            severity="P0",
            coin="TON",
            message="test",
        )
    ]
    written = write_events(events, tmp_path)
    assert written is not None and written.exists()
    line = written.read_text().strip().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["kind"] == "price_drift"
    # Idempotent append: second call adds a second line
    write_events(events, tmp_path)
    assert len(written.read_text().strip().splitlines()) == 2


def test_write_events_empty_input_is_noop(tmp_path: Path):
    assert write_events([], tmp_path) is None


# ───────────────────────── threshold sanity ───────────────────────────

def test_thresholds_match_taxonomy_doc():
    """Guards against drift between the doc and the code. If the .1 doc
    is updated, update these too (or vice versa)."""
    assert Decimal("0.05") == Thresholds.PRICE_DRIFT_PCT
    assert Decimal("0.0001") == Thresholds.FUNDING_EPSILON
    assert Decimal("50") == Thresholds.PEG_DEVIATION_BPS
    assert Thresholds.DA_SETTLEMENT_WINDOW_SEC == 30 * 60
    assert Thresholds.DA_SETTLEMENT_URGENT_SEC == 10 * 60
    assert Decimal("2.0") == Thresholds.YIELD_JUMP_MULTIPLIER
    assert Decimal("500") == Thresholds.YIELD_JUMP_MIN_BASELINE_BPS
    assert Decimal("0.10") == Thresholds.LM_LIQ_DISTANCE_THRESHOLD


# ───────────────────────── pick invalidation (event #9) ───────────────

def _decision_with_invalidate(
    venue_id: str,
    product_id: str,
    invalidate_at: dict | None = None,
) -> dict:
    pick = {"product_id": product_id, "weight": 1.0, "notes": []}
    if invalidate_at is not None:
        pick["invalidate_at"] = invalidate_at
    return {
        "thesis": "test",
        "venues": [
            {"venue_id": "cash_usdc", "weight": 0.3, "picks": []},
            {"venue_id": venue_id, "weight": 0.7, "picks": [pick]},
        ],
        "confidence": 0.7,
        "risk_flags": [],
        "notes": [],
        "expected_blended_apr_pct": 5.0,
        "hedges": [],
    }


def _baseline_with_earn(
    coin: str, product_id: str, entry_mark: str = "2.0"
) -> WatcherBaseline:
    return WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[
            HeldPosition(
                position_id=f"earn:{product_id}",
                venue="earn",
                coin=coin,
            ),
            HeldPosition(
                position_id=f"perp:{coin}USDT",
                venue="perp",
                coin=coin,
                entry_mark_price=Decimal(entry_mark),
            ),
        ],
    )


def test_check_pick_invalidation_returns_empty_when_no_decision():
    from agent.sandbox.watcher import check_pick_invalidation
    baseline = WatcherBaseline(captured_at=datetime.now(UTC))
    events = check_pick_invalidation(
        decision=None, baseline=baseline,
        snapshot_signals={}, peg_dev_bps=None,
    )
    assert events == []


def test_check_pick_invalidation_non_stable_price_default_fires_on_30pct_drop():
    """Category default for non-stable Earn picks: fire when mark drops
    ≥30% from entry. TON entry $2, current $1.30 → 35% drop → fire."""
    from agent.sandbox.watcher import check_pick_invalidation
    decision = _decision_with_invalidate("bybit_onchain", "8")
    baseline = _baseline_with_earn("TON", "8", entry_mark="2.0")
    signals = {"TON": {"mark_price": Decimal("1.30"), "funding_8h": None}}
    events = check_pick_invalidation(
        decision=decision, baseline=baseline,
        snapshot_signals=signals, peg_dev_bps=None,
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "pick_invalidated"
    assert ev.severity == "P0"
    assert ev.coin == "TON"
    assert "price_drift_pct" in ev.threshold


def test_check_pick_invalidation_non_stable_price_default_silent_on_small_drop():
    """Same default at 10% drop — under threshold, no event."""
    from agent.sandbox.watcher import check_pick_invalidation
    decision = _decision_with_invalidate("bybit_onchain", "8")
    baseline = _baseline_with_earn("TON", "8", entry_mark="2.0")
    signals = {"TON": {"mark_price": Decimal("1.80"), "funding_8h": None}}
    events = check_pick_invalidation(
        decision=decision, baseline=baseline,
        snapshot_signals=signals, peg_dev_bps=None,
    )
    assert events == []


def test_check_pick_invalidation_custom_price_below_overrides_default():
    """Operator-set price_below=1.50 absolute floor — fires when mark
    goes below it even when drift-pct default wouldn't."""
    from agent.sandbox.watcher import check_pick_invalidation
    decision = _decision_with_invalidate(
        "bybit_onchain", "8",
        invalidate_at={"price_below": 1.50},
    )
    baseline = _baseline_with_earn("TON", "8", entry_mark="2.0")
    signals = {"TON": {"mark_price": Decimal("1.45"), "funding_8h": None}}
    events = check_pick_invalidation(
        decision=decision, baseline=baseline,
        snapshot_signals=signals, peg_dev_bps=None,
    )
    assert len(events) >= 1
    msgs = [e.message for e in events]
    assert any("price_below" in str(e.threshold) for e in events), msgs


def test_check_pick_invalidation_funding_default_fires_below_neg_2_bps():
    """Raw-funding FALLBACK gate (`funding_8h_below`=-0.0002) for a non-stable
    whose baseline lacks a stored gross APR — fires when funding sustained more
    negative. No dwell_counts passed → fires immediately."""
    from agent.sandbox.watcher import check_pick_invalidation
    decision = _decision_with_invalidate("bybit_onchain", "8")
    baseline = _baseline_with_earn("TON", "8", entry_mark="2.0")
    signals = {
        "TON": {
            "mark_price": Decimal("2.0"),
            "funding_8h": Decimal("-0.00025"),
        }
    }
    events = check_pick_invalidation(
        decision=decision, baseline=baseline,
        snapshot_signals=signals, peg_dev_bps=None,
    )
    funding_events = [
        e for e in events if "funding_8h_below" in e.threshold
    ]
    assert len(funding_events) == 1
    assert funding_events[0].severity == "P0"


def test_check_pick_invalidation_stable_peg_default_fires_above_200bps():
    """Stable USD1 Flex pick — default peg_dev_above_bps=200, fire at 250."""
    from agent.sandbox.watcher import check_pick_invalidation
    decision = _decision_with_invalidate("bybit_flex", "1131")
    baseline = WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[
            HeldPosition(
                position_id="earn:1131", venue="earn", coin="USD1",
            ),
        ],
    )
    events = check_pick_invalidation(
        decision=decision, baseline=baseline,
        snapshot_signals={},
        peg_dev_bps=Decimal("-250"),  # USDC -250 bps from $1 → fires
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "pick_invalidated"
    assert "peg_dev_above_bps" in ev.threshold


def test_check_pick_invalidation_stable_silent_when_peg_within_default():
    """Same setup, peg dev -150 bps — under 200 bps default, no event."""
    from agent.sandbox.watcher import check_pick_invalidation
    decision = _decision_with_invalidate("bybit_flex", "1131")
    baseline = WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[
            HeldPosition(position_id="earn:1131", venue="earn", coin="USD1"),
        ],
    )
    events = check_pick_invalidation(
        decision=decision, baseline=baseline,
        snapshot_signals={},
        peg_dev_bps=Decimal("-150"),
    )
    assert events == []


# ───────────────────── ah.10 net-of-hedge + dwell ─────────────────────


def _baseline_with_earn_gross(
    coin: str, product_id: str, gross_apr: str, entry_mark: str = "2.0"
) -> WatcherBaseline:
    return WatcherBaseline(
        captured_at=datetime.now(UTC),
        positions=[
            HeldPosition(
                position_id=f"earn:{product_id}", venue="earn", coin=coin,
                entry_gross_apr=Decimal(gross_apr),
            ),
            HeldPosition(
                position_id=f"perp:{coin}USDT", venue="perp", coin=coin,
                entry_mark_price=Decimal(entry_mark),
            ),
        ],
    )


def test_net_of_hedge_survives_deep_negative_funding():
    """promptcode-1: a high-gross-APR pick is NOT force-closed by deeply
    negative funding while it stays net-positive. 0.30 gross + (-0.0002×1095 =
    -0.219) funding = +0.081 net > 0 floor → no event."""
    from agent.sandbox.watcher import check_pick_invalidation
    decision = _decision_with_invalidate("bybit_onchain", "8")
    baseline = _baseline_with_earn_gross("TON", "8", "0.30")
    signals = {"TON": {"mark_price": Decimal("2.0"), "funding_8h": Decimal("-0.0002")}}
    events = check_pick_invalidation(
        decision=decision, baseline=baseline, snapshot_signals=signals,
        peg_dev_bps=None,
    )
    assert [e for e in events if e.kind == "pick_invalidated"] == []


def test_net_of_hedge_fires_when_net_negative():
    """A low-gross pick whose funding drags net below 0 fires immediately (no
    dwell_counts passed). 0.05 gross - 0.219 = -0.169 net < 0."""
    from agent.sandbox.watcher import check_pick_invalidation
    decision = _decision_with_invalidate("bybit_onchain", "8")
    baseline = _baseline_with_earn_gross("TON", "8", "0.05")
    signals = {"TON": {"mark_price": Decimal("2.0"), "funding_8h": Decimal("-0.0002")}}
    events = check_pick_invalidation(
        decision=decision, baseline=baseline, snapshot_signals=signals,
        peg_dev_bps=None,
    )
    fired = [e for e in events if e.kind == "pick_invalidated"]
    assert len(fired) == 1
    assert "net_apr_below" in fired[0].threshold
    assert "funding_8h" in fired[0].current  # ah.10 rename
    assert fired[0].severity == "P0"


def test_dwell_requires_consecutive_breaches_and_resets():
    """With dwell_counts passed, a single net breach doesn't fire; the 2nd
    consecutive does; a clean poll resets the counter."""
    from agent.sandbox.watcher import check_pick_invalidation
    decision = _decision_with_invalidate("bybit_onchain", "8")
    baseline = _baseline_with_earn_gross("TON", "8", "0.05")
    breach = {"TON": {"mark_price": Decimal("2.0"), "funding_8h": Decimal("-0.0002")}}
    dwell: dict[str, int] = {}

    ev1 = check_pick_invalidation(
        decision=decision, baseline=baseline, snapshot_signals=breach,
        peg_dev_bps=None, dwell_counts=dwell,
    )
    assert [e for e in ev1 if e.kind == "pick_invalidated"] == []  # 1st breach
    assert dwell["earn:8"] == 1

    ev2 = check_pick_invalidation(
        decision=decision, baseline=baseline, snapshot_signals=breach,
        peg_dev_bps=None, dwell_counts=dwell,
    )
    assert len([e for e in ev2 if e.kind == "pick_invalidated"]) == 1  # 2nd → fire
    assert dwell["earn:8"] == 2

    # A clean poll (positive funding → net positive) resets the counter.
    clean = {"TON": {"mark_price": Decimal("2.0"), "funding_8h": Decimal("0.0001")}}
    check_pick_invalidation(
        decision=decision, baseline=baseline, snapshot_signals=clean,
        peg_dev_bps=None, dwell_counts=dwell,
    )
    assert "earn:8" not in dwell


def test_update_baseline_stores_entry_gross_apr(tmp_path: Path) -> None:
    """The Earn baseline position captures the pick's GROSS Earn APR (snapshot
    pre-hedge base), not the funding-overwritten net `effective_apr`."""
    snap = {
        "captured_at": "2026-06-09T00:00:00+00:00",
        "earn_positions": [{"productId": "8", "coin": "TON", "amount": "100"}],
        "products": {"OnChain": [
            {"product_id": "8", "coin": "TON", "effective_apr": "0.081",
             "effective_apr_gross": "0.30"},
        ]},
    }
    baseline = update_baseline_from_snapshot(snap, path=tmp_path / "b.json")
    earn = next(p for p in baseline.positions if p.position_id == "earn:8")
    assert earn.entry_gross_apr == Decimal("0.30")


def test_dwell_state_roundtrip(tmp_path: Path) -> None:
    from agent.sandbox.watcher import read_dwell_state, write_dwell_state
    p = tmp_path / "dwell.json"
    assert read_dwell_state(p) == {}
    write_dwell_state({"earn:8": 2, "earn:9": 1}, p)
    assert read_dwell_state(p) == {"earn:8": 2, "earn:9": 1}


# ───────────────────── earn redeem settlement (durable exit) ───────────

from decimal import Decimal as _D
from agent.sandbox.watcher import (
    REDEEM_SETTLE_DWELL_POLLS,
    check_earn_redeem_settled,
    _poll_redeem_settlements,
)
from agent.sandbox.redeem_intent import RedeemExitIntent


def _exit_intent(expected="100", baseline_wallet="2", coin="TON",
                 product_id="TON-FLEX", category="FlexibleSaving"):
    return RedeemExitIntent(
        coin=coin,
        product_id=product_id,
        category=category,
        opened_at=datetime.now(UTC),
        expected_redeem_native=_D(expected),
        baseline_wallet_native=_D(baseline_wallet),
        redeem_order_link_id="lnk-1",
        paired_perp_symbol=f"{coin}USDT",
        perp_qty_base=_D(expected),
    )


def test_redeem_settled_earn_row_gone():
    assert check_earn_redeem_settled(_exit_intent(), None, _D("2")) is True


def test_redeem_settled_earn_amount_zero():
    assert check_earn_redeem_settled(_exit_intent(), _D("0"), _D("2")) is True


def test_redeem_not_settled_row_present_no_arrival():
    # row still holds 100, wallet unchanged → not settled
    assert check_earn_redeem_settled(_exit_intent(), _D("100"), _D("2")) is False


def test_redeem_settled_via_wallet_delta():
    # row lingers but 95 of the 100 expected arrived (>90% threshold)
    assert check_earn_redeem_settled(_exit_intent(), _D("100"), _D("97")) is True
    # only 50 arrived → still not settled
    assert check_earn_redeem_settled(_exit_intent(), _D("100"), _D("52")) is False


def _earn_row(product_id="TON-FLEX", amount="100"):
    return SimpleNamespace(productId=product_id, amount=amount)


@pytest.mark.asyncio
async def test_poll_redeem_settlements_dwell_then_fire(tmp_path: Path):
    dwell_path = tmp_path / "dwell.json"
    client = AsyncMock()
    # Earn row gone (settled) on every poll; coin arrived in wallet
    client.get_earn_positions = AsyncMock(return_value=[])
    client.get_account_coin_balance = AsyncMock(return_value=_D("102"))
    intents = [_exit_intent()]

    # Poll 1: settled but dwell=1 < 2 → no event yet
    ev1 = await _poll_redeem_settlements(client, intents, dwell_path)
    assert ev1 == []
    # Poll 2: dwell reaches threshold → fire
    ev2 = await _poll_redeem_settlements(client, intents, dwell_path)
    assert len(ev2) == 1
    assert ev2[0].kind == "earn_redeem_settled"
    assert ev2[0].coin == "TON"
    assert ev2[0].position_id == "earn:TON-FLEX"


@pytest.mark.asyncio
async def test_poll_redeem_settlements_not_settled_no_event(tmp_path: Path):
    dwell_path = tmp_path / "dwell.json"
    client = AsyncMock()
    # Earn row still holds full amount, nothing arrived
    client.get_earn_positions = AsyncMock(return_value=[_earn_row()])
    client.get_account_coin_balance = AsyncMock(return_value=_D("1"))
    ev = await _poll_redeem_settlements(client, [_exit_intent()], dwell_path)
    assert ev == []
    # dwell must NOT accumulate across genuine not-settled polls
    ev2 = await _poll_redeem_settlements(client, [_exit_intent()], dwell_path)
    assert ev2 == []
