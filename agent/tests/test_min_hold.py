"""Min-hold anti-churn gate coverage.

Two layers under test:
  1. `agent.sandbox.position_ledger` — the entry-time ledger that ages each
     held non-stable coin across cycles (stamp new, preserve existing, drop
     gone, restart the clock on re-entry).
  2. `agent.validate.rules.check_min_hold` / `validate(..., allow_exits=)` —
     the gate that rejects a voluntary exit of a non-stable coin younger than
     `MIN_HOLD_HOURS`, while exempting same-coin rotation, stables, the danger
     (allow_exits) path, and risk_flags cycles.

Reuses the synthetic Snapshot/Decision builders from `test_validate_rules`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent.bybit_oracle.bybit_client import PerpPosition
from agent.reason.schema import Hedge
from agent.sandbox import position_ledger as pl
from agent.validate.rules import check_min_hold, validate

from tests.test_validate_rules import _decision, _product, _snapshot, _venue


# ─── Ledger ──────────────────────────────────────────────────────────────────


def _ns(*, earn=None, lm=None, alpha=None, perp=None) -> SimpleNamespace:
    return SimpleNamespace(
        earn_positions=earn or [],
        lm_positions=lm or [],
        alpha_positions=alpha or [],
        perp_positions=perp or [],
    )


def test_ledger_stamps_new_coin_at_age_zero(tmp_path) -> None:
    snap = _ns(earn=[{"coin": "TON", "amount": "5"}])
    ages = pl.update_ledger_and_ages(
        snap, now=datetime(2026, 6, 10, 12, tzinfo=UTC), path=tmp_path / "l.json"
    )
    assert ages == {"TON": 0.0}


def test_ledger_preserves_first_seen_across_cycles(tmp_path) -> None:
    p = tmp_path / "l.json"
    snap = _ns(earn=[{"coin": "TON", "amount": "5"}])
    t0 = datetime(2026, 6, 10, tzinfo=UTC)
    pl.update_ledger_and_ages(snap, now=t0, path=p)
    ages = pl.update_ledger_and_ages(snap, now=t0 + timedelta(hours=10), path=p)
    assert ages["TON"] == pytest.approx(10.0)


def test_ledger_drops_gone_coin_and_restarts_clock_on_reentry(tmp_path) -> None:
    p = tmp_path / "l.json"
    held = _ns(earn=[{"coin": "TON", "amount": "5"}])
    gone = _ns()
    t0 = datetime(2026, 6, 10, tzinfo=UTC)
    pl.update_ledger_and_ages(held, now=t0, path=p)
    pl.update_ledger_and_ages(gone, now=t0 + timedelta(hours=5), path=p)
    # Re-entry 20h after the ORIGINAL stamp: clock restarts, not 20h old.
    ages = pl.update_ledger_and_ages(held, now=t0 + timedelta(hours=20), path=p)
    assert ages["TON"] == 0.0


def test_ledger_excludes_stables(tmp_path) -> None:
    snap = _ns(earn=[{"coin": "USDC", "amount": "50"}, {"coin": "USDT", "amount": "5"}])
    ages = pl.update_ledger_and_ages(
        snap, now=datetime(2026, 6, 10, tzinfo=UTC), path=tmp_path / "l.json"
    )
    assert ages == {}


def test_ledger_skips_zero_amount_earn(tmp_path) -> None:
    snap = _ns(earn=[{"coin": "TON", "amount": "0"}])
    ages = pl.update_ledger_and_ages(
        snap, now=datetime(2026, 6, 10, tzinfo=UTC), path=tmp_path / "l.json"
    )
    assert ages == {}


def test_held_nonstable_tracks_perp_short() -> None:
    snap = _ns(perp=[PerpPosition(symbol="TONUSDT", side="Sell", size="3")])
    assert pl.held_nonstable_coins(snap) == {"TON"}


def test_held_nonstable_lm_takes_base_coin_only() -> None:
    snap = _ns(lm=[{"coin": "ETH/USDC"}])
    assert pl.held_nonstable_coins(snap) == {"ETH"}


def test_held_nonstable_ignores_flat_perp() -> None:
    snap = _ns(perp=[PerpPosition(symbol="TONUSDT", side="None", size="0")])
    assert pl.held_nonstable_coins(snap) == set()


# ─── check_min_hold / validate ───────────────────────────────────────────────


def _snap(ages: dict[str, float]):
    """Snapshot whose only non-stable product is TON (flex `ton1`), with the
    given per-coin ages injected as the loop would."""
    s = _snapshot(
        flex_products=[
            _product("ton1", "FlexibleSaving", coin="TON", effective_apr="0.5"),
        ]
    )
    s.held_coin_ages = ages
    return s


def test_blocks_young_voluntary_exit() -> None:
    d = _decision(venues=[_venue("cash_usdc", 1.0)])
    ok, msg = check_min_hold(d, _snap({"TON": 5.0}))
    assert ok is False
    assert msg is not None and "TON" in msg


def test_allows_when_coin_still_held() -> None:
    # Same coin retained via a flex pick — no full exit, no round-trip paid.
    d = _decision(
        venues=[_venue("cash_usdc", 0.5), _venue("bybit_flex", 0.5, [("ton1", 1.0)])]
    )
    assert check_min_hold(d, _snap({"TON": 5.0})) == (True, None)


def test_allows_when_coin_retained_via_hedge() -> None:
    d = _decision(
        venues=[_venue("cash_usdc", 1.0)],
        hedges=[Hedge(coin="TON", notional_usd=10.0)],
    )
    assert check_min_hold(d, _snap({"TON": 5.0})) == (True, None)


def test_allows_exit_once_old_enough() -> None:
    d = _decision(venues=[_venue("cash_usdc", 1.0)])
    assert check_min_hold(d, _snap({"TON": 50.0})) == (True, None)


def test_ignores_stable_ages() -> None:
    d = _decision(venues=[_venue("cash_usdc", 1.0)])
    assert check_min_hold(d, _snap({"USDC": 1.0})) == (True, None)


def test_no_ages_fails_open() -> None:
    d = _decision(venues=[_venue("cash_usdc", 1.0)])
    assert check_min_hold(d, _snap({})) == (True, None)


def test_validate_rejects_young_exit_with_min_hold_error() -> None:
    d = _decision(venues=[_venue("cash_usdc", 1.0)])
    ok, errs = validate(d, _snap({"TON": 5.0}))
    assert ok is False
    assert any("min-hold" in e for e in errs)


def test_validate_allow_exits_skips_min_hold() -> None:
    d = _decision(venues=[_venue("cash_usdc", 1.0)])
    _, errs = validate(d, _snap({"TON": 5.0}), allow_exits=True)
    assert not any("min-hold" in e for e in errs)


def test_validate_risk_flags_cycle_skips_min_hold() -> None:
    d = _decision(venues=[_venue("cash_usdc", 1.0)], risk_flags=["peg_break"])
    _, errs = validate(d, _snap({"TON": 5.0}))
    assert not any("min-hold" in e for e in errs)
