"""Validator coverage tests (`.29`).

Builds synthetic `Snapshot` + `Decision` fixtures and exercises every
conditional rule in `agent/validate/rules.py`. Each rule gets at least
one positive (passes) and one negative (fails) case. The fixtures live
in-test rather than on disk because they are small, hand-tuned, and
need to be near the assertions that read them.

Live cycles in `.10` and `.28` only exercised a thin slice of the rule
matrix — calm peg, no missing APRs, no non-USD picks, no low-confidence
abort, no hedge requirement triggered. `.29` makes the matrix explicit
so a regression in any rule fails CI rather than waiting to surface
in a live cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from agent.reason.schema import Decision, Hedge, InvalidateAt, Pick, VenueAllocation
from agent.sandbox.snapshot import (
    MarketSnapshot,
    PerpInfo,
    ProductSummary,
    Snapshot,
    UsdcPegSnapshot,
    WalletSnapshot,
)
from agent.validate.rules import (
    CASH_FLOOR,
    FUNDING_FLOOR_HEDGE_ANNUAL,
    PEG_STRESS_BPS,
    PEG_STRESS_STABLES_FLOOR,
    check_capital_flow_simulation,
    check_combined_stable_liquidity,
    check_confidence,
    check_disabled_venues,
    check_effective_pick_cap,
    check_funding_carry_floor,
    check_funding_rate_floor,
    check_hedges_for_non_usd_picks,
    check_lm_leverage_forbidden,
    check_lm_leverage_size_cap,
    check_lm_residual_naked_exposure,
    check_lm_stable_preference,
    check_lockup_cap,
    check_min_stake,
    check_no_double_carry_hedge,
    check_no_missing_apr_source,
    check_peg_stress,
    check_picks_required,
    check_product_ids_in_snapshot,
    check_risk_flags,
    check_stable_earn_funding,
    check_stable_spend_cap,
    check_venue_caps,
    check_venue_floors,
    validate,
)
from agent.validate.rules import _held_lm_usd_by_product


# ─── Fixture factories ──────────────────────────────────────────────────────


def _peg(deviation_bps: float | None) -> UsdcPegSnapshot:
    return UsdcPegSnapshot(
        price_usd=Decimal("1.0") if deviation_bps is None else None,
        deviation_bps=None if deviation_bps is None else Decimal(str(deviation_bps)),
        fetched_at=datetime.now(UTC),
    )


def _product(
    product_id: str,
    category: str,
    coin: str = "USDC",
    effective_apr: str = "0.05",
    apr_source: str = "estimate_apr",
    notes: list[str] | None = None,
    redeem_lockup_minutes: int | None = None,
    fixed_term_days: int | None = None,
    min_subscribe_usd: str | None = None,
    effective_apr_net_holding: str | None = None,
) -> ProductSummary:
    return ProductSummary(
        category=category,
        product_id=product_id,
        coin=coin,
        effective_apr=Decimal(effective_apr),
        apr_source=apr_source,
        base_apr_string=None,
        redeem_lockup_minutes=redeem_lockup_minutes,
        fixed_term_days=fixed_term_days,
        min_subscribe_usd=(
            Decimal(min_subscribe_usd) if min_subscribe_usd is not None else None
        ),
        effective_apr_net_holding=(
            Decimal(effective_apr_net_holding)
            if effective_apr_net_holding is not None
            else None
        ),
        notes=notes or [],
    )


def _snapshot(
    *,
    deviation_bps: float | None = -3.0,
    flex_products: list[ProductSummary] | None = None,
    onchain_products: list[ProductSummary] | None = None,
    lm_products: list[ProductSummary] | None = None,
    perp_market: dict[str, PerpInfo] | None = None,
    total_equity_usd: str = "100",
    liquid_usdc_usd: str = "0",
    liquid_usdt_usd: str = "0",
    earn_positions: list[dict[str, Any]] | None = None,
    lm_positions: list[dict[str, Any]] | None = None,
) -> Snapshot:
    """Build a Snapshot with all the bells the validator reads. Defaults
    yield a calm regime — peg fine, one stable in flex, one stable in
    onchain, one unleveraged LM pair — so tests can override only the
    field they care about."""
    products: dict[str, list[ProductSummary]] = {
        "FlexibleSaving": flex_products
        or [_product("1131", "FlexibleSaving", coin="USD1", effective_apr="0.0752", apr_source="estimate_apr")],
        "OnChain": onchain_products
        or [_product("26", "OnChain", coin="USDC", effective_apr="0.04")],
        "LiquidityMining": lm_products
        or [_product("24", "LiquidityMining", coin="ETH/USDC", effective_apr="0.025", apr_source="apy_e8", notes=["max_leverage=1"])],
    }
    return Snapshot(
        captured_at=datetime.now(UTC),
        wallet=WalletSnapshot(
            total_equity_usd=Decimal(total_equity_usd),
            liquid_usdc_usd=Decimal(liquid_usdc_usd),
            liquid_usdt_usd=Decimal(liquid_usdt_usd),
        ),
        earn_positions=earn_positions or [],
        lm_positions=lm_positions or [],
        products=products,
        market=MarketSnapshot(),
        perp_market=perp_market or {},
        usdc_peg=_peg(deviation_bps),
        errors=[],
    )


def _perp(
    coin: str,
    *,
    mark: str = "2.0",
    min_notional: str = "0.5",
    funding_rate_7d_avg: str | None = None,
    funding_interval_hours: str | None = None,
    qty_step: str | None = None,
    min_order_qty: str = "0.1",
) -> PerpInfo:
    """Build a PerpInfo for validator tests. `funding_interval_hours=None`
    leaves the field unset, triggering the validator's 8h fallback (same
    arithmetic the pre-2026-06-03 per-period code used). Pass `"4"` to
    exercise 4h funding cadences (memecoin / high-vol perps)."""
    return PerpInfo(
        symbol=f"{coin.upper()}USDT",
        funding_rate_8h=Decimal("0.0001"),
        funding_rate_7d_avg=(
            Decimal(funding_rate_7d_avg)
            if funding_rate_7d_avg is not None
            else None
        ),
        funding_interval_hours=(
            Decimal(funding_interval_hours)
            if funding_interval_hours is not None
            else None
        ),
        mark_price=Decimal(mark),
        orderbook_depth_50bps_usd=Decimal("100000"),
        min_order_qty=Decimal(min_order_qty),
        min_notional_usd=Decimal(min_notional),
        max_leverage=Decimal("50"),
        qty_step=Decimal(qty_step) if qty_step is not None else None,
    )


def _venue(
    venue_id: str,
    weight: float,
    picks: list[tuple[str, float]] | None = None,
) -> VenueAllocation:
    return VenueAllocation(
        venue_id=venue_id,  # type: ignore[arg-type]
        weight=weight,
        picks=[Pick(product_id=pid, weight=w) for pid, w in (picks or [])],
    )


def _decision(
    *,
    venues: list[VenueAllocation] | None = None,
    confidence: float = 0.7,
    risk_flags: list[str] | None = None,
    hedges: list[Hedge] | None = None,
    expected_apr: float = 4.0,
    thesis: str = "Calm regime; anchor on cash + USD1 promo + tiny LM.",
) -> Decision:
    """Default decision is validator-clean: cash 60%, flex 40% USD1@1.0
    (stable per-product cap is 0.40)."""
    venues = venues or [
        _venue("cash_usdc", 0.6),
        _venue("bybit_flex", 0.4, [("1131", 1.0)]),
    ]
    return Decision(
        thesis=thesis,
        venues=venues,
        hedges=hedges or [],
        confidence=confidence,
        risk_flags=risk_flags or [],
        notes=[],
        expected_blended_apr_pct=expected_apr,
    )


# ─── Decision-only checks ───────────────────────────────────────────────────


def test_check_disabled_venues_passes_when_no_disabled_venues_used() -> None:
    """Safety net for future flips — no venue is currently `enabled=False`,
    but the check must still pass on a clean decision."""
    d = _decision()
    assert check_disabled_venues(d) == (True, None)


def test_check_disabled_venues_safety_when_a_venue_is_flagged_off(monkeypatch) -> None:
    """Temporarily flip a venue to disabled to exercise the rule —
    keeps the safety net under test even with all venues currently
    enabled in production config."""
    from agent.reason.venues import VENUE_REGISTRY
    monkeypatch.setattr(
        VENUE_REGISTRY["aave_v3_usdc"], "enabled", False, raising=False
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("aave_v3_usdc", 0.5),
        ]
    )
    ok, msg = check_disabled_venues(d)
    assert ok is False
    assert "aave_v3_usdc" in (msg or "")


def test_aave_v3_usdc_with_nonzero_weight_fails_via_zero_cap() -> None:
    """`.37a`: aave_v3_usdc is `enabled=True` but `max_weight=0` until
    `.37b` wires execute. Any non-zero pick must be rejected by
    `check_venue_caps`, not `check_disabled_venues`."""
    from agent.validate.rules import check_venue_caps
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("aave_v3_usdc", 0.5),
        ]
    )
    ok, msg = check_venue_caps(d)
    assert ok is False
    assert "aave_v3_usdc" in (msg or "")


def test_check_venue_caps_passes_at_max_weight() -> None:
    # bybit_flex.max_weight is 0.70; right at cap should pass.
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.30),
            _venue("bybit_flex", 0.70, [("1131", 1.0)]),
        ]
    )
    assert check_venue_caps(d) == (True, None)


def test_check_venue_caps_fails_above_max_weight() -> None:
    # bybit_onchain.max_weight is 0.70; pushing to 0.80 must fail.
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.20),
            _venue("bybit_onchain", 0.80, [("26", 1.0)]),
        ]
    )
    ok, msg = check_venue_caps(d)
    assert ok is False
    assert "bybit_onchain" in (msg or "")


def test_check_venue_floors_fails_below_cash_floor() -> None:
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.05),  # below 0.10 floor
            _venue("bybit_flex", 0.45, [("1131", 1.0)]),
            _venue("bybit_onchain", 0.40, [("26", 1.0)]),
            _venue("bybit_lm", 0.10, [("24", 1.0)]),
        ]
    )
    ok, msg = check_venue_floors(d)
    assert ok is False
    assert "cash_usdc" in (msg or "")


def test_check_picks_required_fails_when_venue_has_no_picks() -> None:
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5),  # missing picks
        ]
    )
    ok, msg = check_picks_required(d)
    assert ok is False
    assert "bybit_flex" in (msg or "")


def test_check_picks_required_fails_when_cash_has_picks() -> None:
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5, [("ghost", 1.0)]),  # cash is single-pool
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    ok, msg = check_picks_required(d)
    assert ok is False
    assert "cash_usdc" in (msg or "")


def test_check_effective_pick_cap_fails_when_non_stable_oversizes() -> None:
    """Non-stable picks (e.g. TON OnChain) capped at 0.60 (.66).
    Effective bybit_onchain=0.65 × pick.weight=1.0 = 0.65 > 0.60 cap."""
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.35),
            _venue("bybit_onchain", 0.65, [("8", 1.0)]),
        ]
    )
    s = _snapshot(
        onchain_products=[
            _product("8", "OnChain", coin="TON", effective_apr="0.18"),
        ]
    )
    ok, msg = check_effective_pick_cap(d, s)
    assert ok is False
    assert "bybit_onchain/8" in (msg or "")


def test_check_effective_pick_cap_passes_stable_at_60pct() -> None:
    """2026-06-08 (.66): stable Earn per-product cap is 0.60. A single USD1
    pick at exactly the cap passes — concentration into the best stable is
    the intended behavior."""
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.40),
            _venue("bybit_flex", 0.60, [("1131", 1.0)]),  # USD1, eff 0.60
        ]
    )
    s = _snapshot(
        flex_products=[
            _product(
                "1131", "FlexibleSaving",
                coin="USD1", effective_apr="0.075",
                apr_source="estimate_apr",
            ),
        ]
    )
    ok, errs = validate(d, s)
    assert ok, errs


def test_check_effective_pick_cap_fails_stable_above_60pct() -> None:
    """0.60 IS the stable cap — go above and it fails."""
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.35),
            _venue("bybit_flex", 0.65, [("1131", 1.0)]),  # USD1, eff 0.65
        ]
    )
    s = _snapshot(
        flex_products=[
            _product(
                "1131", "FlexibleSaving",
                coin="USD1", effective_apr="0.075",
                apr_source="estimate_apr",
            ),
        ]
    )
    ok, msg = check_effective_pick_cap(d, s)
    assert ok is False
    assert "bybit_flex/1131" in (msg or "")


def test_check_effective_pick_cap_passes_when_split() -> None:
    """Split still works — pre-fix behavior preserved for non-stable
    picks where the cap matters most."""
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.40),
            _venue(
                "bybit_flex",
                0.60,
                [("1131", 0.5), ("1", 0.5)],
            ),
        ]
    )
    s = _snapshot(
        flex_products=[
            _product("1131", "FlexibleSaving", coin="USD1", effective_apr="0.075", apr_source="estimate_apr"),
            _product("1", "FlexibleSaving", coin="USDT", effective_apr="0.015"),
        ]
    )
    ok, errs = validate(d, s)
    assert ok, errs


def test_check_confidence_fails_below_threshold() -> None:
    d = _decision(confidence=0.39)
    ok, msg = check_confidence(d)
    assert ok is False
    assert "confidence" in (msg or "")


def test_check_risk_flags_fails_when_non_empty() -> None:
    d = _decision(risk_flags=["depeg-suspected"])
    ok, msg = check_risk_flags(d)
    assert ok is False
    assert "depeg-suspected" in (msg or "")


# ─── Snapshot-aware checks ──────────────────────────────────────────────────


def test_check_peg_stress_fails_when_depegged_and_no_stable_floor() -> None:
    # deviation -150 bps → stress; cash+flex must be >= 0.50.
    s = _snapshot(deviation_bps=-150.0)
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.20),
            _venue("bybit_onchain", 0.40, [("26", 1.0)]),
            _venue("bybit_lm", 0.30, [("24", 1.0)]),
            _venue("bybit_flex", 0.10, [("1131", 1.0)]),
        ]
    )
    ok, msg = check_peg_stress(d, s)
    assert ok is False
    assert f"{PEG_STRESS_BPS}" in (msg or "") or "peg" in (msg or "").lower()


def test_check_peg_stress_passes_when_stables_meet_floor() -> None:
    s = _snapshot(deviation_bps=-150.0)
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.30),
            _venue("bybit_flex", 0.30, [("1131", 1.0)]),
            _venue("bybit_onchain", 0.30, [("26", 1.0)]),
            _venue("bybit_lm", 0.10, [("24", 1.0)]),
        ]
    )
    assert check_peg_stress(d, s) == (True, None)


def test_check_peg_stress_excludes_nonstable_flex_share() -> None:
    """validator-2: a flex venue holding a NON-stable pick (ID) doesn't count
    toward the fast-redeem floor. cash 0.30 + flex 0.30 (ID) → stable share
    0.30 < 0.50 → reject. The old full-flex count read 0.60 and passed."""
    flex = [_product("9001", "FlexibleSaving", coin="ID", apr_source="estimate_apr")]
    s = _snapshot(deviation_bps=-150.0, flex_products=flex)
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.30),
            _venue("bybit_flex", 0.30, [("9001", 1.0)]),
            _venue("bybit_onchain", 0.40, [("26", 1.0)]),
        ]
    )
    ok, msg = check_peg_stress(d, s)
    assert ok is False
    assert "stable share" in (msg or "")


def test_check_peg_stress_counts_partial_stable_flex_share() -> None:
    """A flex venue split 50/50 stable/non-stable contributes only its stable
    half. cash 0.20 + flex 0.60×0.5 (USD1) = 0.50 → meets the floor."""
    flex = [
        _product("1131", "FlexibleSaving", coin="USD1", apr_source="estimate_apr"),
        _product("9001", "FlexibleSaving", coin="ID", apr_source="estimate_apr"),
    ]
    s = _snapshot(deviation_bps=-150.0, flex_products=flex)
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.20),
            _venue("bybit_flex", 0.60, [("1131", 0.5), ("9001", 0.5)]),
            _venue("bybit_onchain", 0.20, [("26", 1.0)]),
        ]
    )
    assert check_peg_stress(d, s) == (True, None)


def test_check_peg_stress_fails_closed_on_null_deviation() -> None:
    # null peg data treated as triggered — fail-closed.
    s = _snapshot(deviation_bps=None)
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.20),
            _venue("bybit_onchain", 0.70, [("26", 1.0)]),
            _venue("bybit_flex", 0.10, [("1131", 1.0)]),
        ]
    )
    ok, _ = check_peg_stress(d, s)
    assert ok is False


def test_check_product_ids_in_snapshot_fails_on_hallucinated_id() -> None:
    s = _snapshot()
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("9999", 1.0)]),  # not in snapshot
        ]
    )
    ok, msg = check_product_ids_in_snapshot(d, s)
    assert ok is False
    assert "9999" in (msg or "")


def test_check_lockup_cap_rejects_pick_above_7_days() -> None:
    """ATOM OnChain product 9 has 36000 min ≈ 25-day lockup. Picker
    occasionally selects it despite the prompt rule — validator
    enforces 7-day hard cap so live execute can't lock funds long."""
    s = _snapshot(
        onchain_products=[
            _product("9", "OnChain", coin="ATOM",
                     effective_apr="0.17",
                     redeem_lockup_minutes=36000),  # 25 days
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_onchain", 0.5, [("9", 1.0)]),
        ]
    )
    ok, msg = check_lockup_cap(d, s)
    assert ok is False
    assert "9" in (msg or "")
    assert "25" in (msg or "") or "36000" in (msg or "")


def test_check_lockup_cap_passes_for_4_day_lockup() -> None:
    """TON OnChain 4 days (5760 min) is within cap — allowed."""
    s = _snapshot(
        onchain_products=[
            _product("8", "OnChain", coin="TON",
                     effective_apr="0.18",
                     redeem_lockup_minutes=5760),  # 4 days
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_onchain", 0.5, [("8", 1.0)]),
        ]
    )
    ok, _ = check_lockup_cap(d, s)
    assert ok is True


def test_check_lockup_cap_rejects_fixed_term_above_7_days() -> None:
    """OnChain Fixed-term product with term=30d locks principal past the
    weekly horizon. `redeem_lockup_minutes` (post-redeem processing) is
    None/short, so only the `fixed_term_days` gate catches it."""
    s = _snapshot(
        onchain_products=[
            _product("42", "OnChain", coin="ETH",
                     effective_apr="0.22",
                     redeem_lockup_minutes=None,
                     fixed_term_days=30),
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_onchain", 0.5, [("42", 1.0)]),
        ]
    )
    ok, msg = check_lockup_cap(d, s)
    assert ok is False
    assert "42" in (msg or "")
    assert "fixed-term" in (msg or "")
    assert "30" in (msg or "")


def test_check_lockup_cap_passes_for_fixed_term_within_7_days() -> None:
    """A 5-day Fixed-term OnChain product unwinds before the next weekly
    rebalance — within cap, allowed."""
    s = _snapshot(
        onchain_products=[
            _product("43", "OnChain", coin="SOL",
                     effective_apr="0.19",
                     fixed_term_days=5),
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_onchain", 0.5, [("43", 1.0)]),
        ]
    )
    ok, _ = check_lockup_cap(d, s)
    assert ok is True


def test_check_lockup_cap_passes_when_no_lockup_field() -> None:
    """FlexibleSaving rows typically have redeem_lockup_minutes=None →
    treated as instant-redeem and allowed through."""
    s = _snapshot(
        flex_products=[
            _product("1131", "FlexibleSaving", coin="USD1",
                     effective_apr="0.07",
                     redeem_lockup_minutes=None),
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    ok, _ = check_lockup_cap(d, s)
    assert ok is True


def test_check_no_missing_apr_source_fails_when_pick_is_missing() -> None:
    s = _snapshot(
        flex_products=[
            _product("1131", "FlexibleSaving", coin="USD1", apr_source="missing"),
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    ok, msg = check_no_missing_apr_source(d, s)
    assert ok is False
    assert "1131" in (msg or "")


def test_check_lm_leverage_size_cap_fails_when_oversize_for_leverage() -> None:
    """5x LM pick at full bybit_lm cap (30%) → effective 30% > 0.30/5 = 6%
    cap → reject. Operator change 2026-05-29: leveraged LM allowed but
    size scales down with leverage."""
    s = _snapshot(
        lm_products=[
            _product("99", "LiquidityMining", coin="NEAR/USDT", apr_source="apy_e8", notes=["max_leverage=5"]),
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.30, [("99", 1.0)]),  # effective 30%, cap 6%
        ]
    )
    ok, msg = check_lm_leverage_size_cap(d, s)
    assert ok is False
    assert "leverage=5" in (msg or "")
    assert "cap" in (msg or "")


def test_check_lm_leverage_size_cap_passes_when_sized_under_leverage_cap() -> None:
    """5x LM pick at 5% effective (within 6% cap) → pass."""
    s = _snapshot(
        lm_products=[
            _product("99", "LiquidityMining", coin="NEAR/USDT", apr_source="apy_e8", notes=["max_leverage=5"]),
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.95),
            _venue("bybit_lm", 0.05, [("99", 1.0)]),  # effective 5%, cap 6%
        ]
    )
    assert check_lm_leverage_size_cap(d, s) == (True, None)


def test_check_lm_leverage_size_cap_passes_unleveraged_at_full_cap() -> None:
    """1x LM pick at 30% (bybit_lm.max_weight) → effective 30%, cap 30% → pass."""
    s = _snapshot(
        lm_products=[
            _product("24", "LiquidityMining", coin="ETH/USDC", apr_source="apy_e8", notes=["max_leverage=1"]),
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.30, [("24", 1.0)]),
        ]
    )
    assert check_lm_leverage_size_cap(d, s) == (True, None)


def test_check_lm_leverage_size_cap_passes_when_no_lm_picks() -> None:
    s = _snapshot()
    d = _decision()  # no LM venue used
    assert check_lm_leverage_size_cap(d, s) == (True, None)


def test_check_lm_leverage_forbidden_rejects_above_1x() -> None:
    """(.66) Any LM pick with max_leverage>1 is rejected outright — a
    leveraged LP on a volatile token is speculative directional risk."""
    s = _snapshot(
        lm_products=[
            _product("99", "LiquidityMining", coin="TIA/USDT", apr_source="apy_e8", notes=["max_leverage=5"]),
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.95),
            _venue("bybit_lm", 0.05, [("99", 1.0)]),  # tiny size, still rejected
        ]
    )
    ok, msg = check_lm_leverage_forbidden(d, s)
    assert ok is False
    assert "max_leverage=5" in (msg or "")


def test_check_lm_leverage_forbidden_allows_1x_and_missing() -> None:
    """1x pairs and pairs with no max_leverage note (treated as 1x) pass."""
    s = _snapshot(
        lm_products=[
            _product("24", "LiquidityMining", coin="ETH/USDC", apr_source="apy_e8", notes=["max_leverage=1"]),
            _product("25", "LiquidityMining", coin="BTC/USDC", apr_source="apy_e8", notes=[]),
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.30, [("24", 0.5), ("25", 0.5)]),
        ]
    )
    assert check_lm_leverage_forbidden(d, s) == (True, None)


def test_validate_rejects_leveraged_lm_end_to_end() -> None:
    """Full validate() rejects a leveraged LM pick (integration of the new
    forbidden gate)."""
    s = _snapshot(
        lm_products=[
            _product("99", "LiquidityMining", coin="TIA/USDT", apr_source="apy_e8", notes=["max_leverage=5"]),
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.94),
            _venue("bybit_lm", 0.06, [("99", 1.0)]),  # within size cap 0.30/5, but leveraged
        ]
    )
    ok, errs = validate(d, s)
    assert ok is False
    assert any("unleveraged" in e for e in errs)


def test_check_hedges_for_non_usd_picks_fails_without_hedge() -> None:
    s = _snapshot(
        onchain_products=[
            _product("8", "OnChain", coin="TON", effective_apr="0.18"),
        ]
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.6),
            _venue("bybit_onchain", 0.40, [("8", 1.0)]),
        ]
    )
    ok, msg = check_hedges_for_non_usd_picks(d, s)
    assert ok is False
    assert "TON" in (msg or "")


def test_check_hedges_for_non_usd_picks_passes_when_perp_feasible() -> None:
    """Auto-hedge era: rule passes when perp pair exists AND pick_usd
    clears `min_notional_usd`. `decision.hedges` is informational."""
    s = _snapshot(
        onchain_products=[
            _product("8", "OnChain", coin="TON", effective_apr="0.18"),
        ],
        perp_market={"TON": _perp("TON", min_notional="1.0")},
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.6),
            _venue("bybit_onchain", 0.40, [("8", 1.0)]),
        ],
        hedges=[Hedge(coin="TON", notional_usd=-40.0)],
    )
    assert check_hedges_for_non_usd_picks(d, s) == (True, None)


def test_check_hedges_for_non_usd_picks_passes_for_stable_pick() -> None:
    s = _snapshot()  # default onchain pick is USDC stable
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.6),
            _venue("bybit_onchain", 0.40, [("26", 1.0)]),
        ]
    )
    assert check_hedges_for_non_usd_picks(d, s) == (True, None)


def test_check_hedges_rejects_lm_base_without_perp() -> None:
    """LM base leg is auto-hedged now: an ETH/USDC pick with no ETH perp
    is un-hedgeable and must be rejected, not opened as a naked LP base."""
    s = _snapshot(perp_market={})  # default LM ETH/USDC, no perp
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("24", 1.0)]),
        ]
    )
    ok, msg = check_hedges_for_non_usd_picks(d, s)
    assert ok is False
    assert "bybit_lm" in (msg or "") and "ETH" in (msg or "")


def test_check_hedges_accepts_lm_base_with_perp() -> None:
    """ETH/USDC pick with an ETH perp whose min_notional the base leg
    clears → hedgeable → pass. Base leg = 100 × 0.3 × 0.5 = $15 ≥ $1."""
    s = _snapshot(perp_market={"ETH": _perp("ETH", min_notional="1.0")})
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("24", 1.0)]),
        ]
    )
    assert check_hedges_for_non_usd_picks(d, s) == (True, None)


def test_check_hedges_rejects_lm_base_below_min_notional() -> None:
    """The hedge is sized on HALF the pick (the base leg). Base leg
    $15 < perp min_notional $20 → reject. Guards the half-notional math:
    the full $30 pick would clear $20, so a reject here proves the half."""
    s = _snapshot(perp_market={"ETH": _perp("ETH", min_notional="20")})
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("24", 1.0)]),
        ]
    )
    ok, msg = check_hedges_for_non_usd_picks(d, s)
    assert ok is False
    assert "base leg" in (msg or "")


def test_capital_flow_counts_lm_base_leg_margin() -> None:
    """A non-stable LM pick now commits stake + perp margin on the base
    half. cash 0.10 + onchain-stable 0.60 + LM 0.30 commits
    60 + (30 + 30×0.5×1.05) = 105.75 > allowable 90 → reject. Under the
    old face-value treatment it committed exactly 90 and passed."""
    s = _snapshot(perp_market={"ETH": _perp("ETH", min_notional="1.0")})
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.10),
            _venue("bybit_onchain", 0.60, [("26", 1.0)]),
            _venue("bybit_lm", 0.30, [("24", 1.0)]),
        ]
    )
    ok, msg = check_capital_flow_simulation(d, s)
    assert ok is False
    assert "bybit_lm" in (msg or "")


def test_held_lm_usd_reconstructs_without_principal_liquidity_value() -> None:
    """validator-3: when Bybit omits `principalLiquidityValue`, the held-LM
    reader rebuilds the principal from `quote + base × currentPrice` instead
    of collapsing to 0 (which re-tripped every LM net-new screen)."""
    s = _snapshot(lm_positions=[{
        "productId": "24",
        "principalQuoteAmount": "15",
        "principalBaseAmount": "7.5",
        "currentPrice": "2.0",
    }])
    held = _held_lm_usd_by_product(s)
    assert held["24"] == pytest.approx(30.0)  # 15 + 7.5 × 2.0


def test_capital_flow_nets_held_lm() -> None:
    """validator-4: a held LM kept at its current size commits nothing fresh,
    so the same allocation that fails when opening cold (105.75 > 90) passes
    when the LP is already held — net_new ≈ 0 → no stake, no margin. Held
    value comes via quote+base reconstruction (no principalLiquidityValue)."""
    s = _snapshot(
        perp_market={"ETH": _perp("ETH", min_notional="1.0")},
        lm_positions=[{
            "productId": "24",
            "principalQuoteAmount": "15",
            "principalBaseAmount": "7.5",
            "currentPrice": "2.0",  # held $30 == LM target
        }],
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.10),
            _venue("bybit_onchain", 0.60, [("26", 1.0)]),
            _venue("bybit_lm", 0.30, [("24", 1.0)]),
        ]
    )
    assert check_capital_flow_simulation(d, s) == (True, None)


def test_check_funding_floor_rejects_lm_when_hedge_bleeds() -> None:
    """LM net-of-hedge gate: a high-gross LP whose base-leg hedge nets
    negative (ranker stored it in effective_apr_net_hedge) is rejected,
    just like a bleeding Flex/OnChain pick."""
    lm = _product("24", "LiquidityMining", coin="ETH/USDC", apr_source="apy_e8", notes=["max_leverage=1"])
    lm.effective_apr_net_hedge = Decimal("-0.03")  # base funding bled the LP
    s = _snapshot(lm_products=[lm], perp_market={"ETH": _perp("ETH", funding_rate_7d_avg="-0.0004")})
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("24", 1.0)]),
        ]
    )
    ok, msg = check_funding_rate_floor(d, s)
    assert ok is False
    assert "bybit_lm" in (msg or "")


def test_check_funding_floor_accepts_lm_when_hedge_profitable() -> None:
    """LM whose net-of-hedge yield stays positive passes the floor."""
    lm = _product("24", "LiquidityMining", coin="ETH/USDC", apr_source="apy_e8", notes=["max_leverage=1"])
    lm.effective_apr_net_hedge = Decimal("0.06")  # LP gross survives funding
    s = _snapshot(lm_products=[lm], perp_market={"ETH": _perp("ETH", funding_rate_7d_avg="0.00005")})
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("24", 1.0)]),
        ]
    )
    assert check_funding_rate_floor(d, s) == (True, None)


def _lm_pref_snapshot(lm_net: str, stable_apr: str = "0.0386", lm_positions=None):
    """Snapshot for LM-stable-preference tests: one ETH/USDC LM with an
    explicit net-of-hedge APR, one USDC stable alternative, ETH perp."""
    lm = _product("24", "LiquidityMining", coin="ETH/USDC", apr_source="apy_e8", notes=["max_leverage=1"])
    lm.effective_apr_net_hedge = Decimal(lm_net)
    s = _snapshot(
        lm_products=[lm],
        flex_products=[_product("100", "FlexibleSaving", coin="USDC", effective_apr="0.02")],
        onchain_products=[_product("26", "OnChain", coin="USDC", effective_apr=stable_apr)],
        perp_market={"ETH": _perp("ETH")},
    )
    if lm_positions is not None:
        s.lm_positions = lm_positions
    return s


def test_check_lm_stable_pref_passes_when_edge_clears_margin() -> None:
    """LM hedged net 5.78% vs best stable 3.86% → edge 1.92% ≥ 1.5% → pass."""
    s = _lm_pref_snapshot(lm_net="0.0578")
    d = _decision(venues=[_venue("cash_usdc", 0.7), _venue("bybit_lm", 0.3, [("24", 1.0)])])
    assert check_lm_stable_preference(d, s) == (True, None)


def test_check_lm_stable_pref_rejects_thin_edge() -> None:
    """LM net 4.5% vs stable 3.86% → edge 0.64% < 1.5% → reject NEW pick
    (extra IL + hedge-maintenance risk for no real return over the stable)."""
    s = _lm_pref_snapshot(lm_net="0.045")
    d = _decision(venues=[_venue("cash_usdc", 0.7), _venue("bybit_lm", 0.3, [("24", 1.0)])])
    ok, msg = check_lm_stable_preference(d, s)
    assert ok is False
    assert "bybit_lm" in (msg or "") and "margin" in (msg or "")


def test_check_lm_stable_pref_exempts_held_position() -> None:
    """A thin-edge LM that's already HELD (net-new < min) is exempt — the
    gate steers NEW deployment only, never force-exits a standing LP."""
    held = [{"productId": "24", "positionId": "9001", "principalLiquidityValue": "60", "status": "Active"}]
    s = _lm_pref_snapshot(lm_net="0.045", lm_positions=held)  # target $30 < held $60
    d = _decision(venues=[_venue("cash_usdc", 0.7), _venue("bybit_lm", 0.3, [("24", 1.0)])])
    assert check_lm_stable_preference(d, s) == (True, None)


def test_check_lm_stable_pref_stable_base_exempt() -> None:
    """A stable-base LM (USDC/USDT) carries no IL/hedge premium to justify,
    so the margin gate doesn't apply even if its yield trails the stable."""
    lm = _product("70", "LiquidityMining", coin="USDC/USDT", apr_source="apy_e8")
    lm.effective_apr_net_hedge = Decimal("0.01")
    s = _snapshot(
        lm_products=[lm],
        onchain_products=[_product("26", "OnChain", coin="USDC", effective_apr="0.0386")],
    )
    d = _decision(venues=[_venue("cash_usdc", 0.7), _venue("bybit_lm", 0.3, [("70", 1.0)])])
    assert check_lm_stable_preference(d, s) == (True, None)


def test_capital_flow_stable_base_lm_commits_face_only() -> None:
    """A stable-base LM pair (hypothetical USDC/USDT) carries no perp
    hedge, so it commits at face: 60 + 30 = 90 == allowable → pass."""
    s = _snapshot(
        lm_products=[
            _product("70", "LiquidityMining", coin="USDC/USDT", apr_source="apy_e8"),
        ],
        perp_market={"ETH": _perp("ETH", min_notional="1.0")},
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.10),
            _venue("bybit_onchain", 0.60, [("26", 1.0)]),
            _venue("bybit_lm", 0.30, [("70", 1.0)]),
        ]
    )
    assert check_capital_flow_simulation(d, s) == (True, None)


# ─── Aggregate happy + sad paths ────────────────────────────────────────────


def test_validate_aggregates_multiple_failures_in_one_pass() -> None:
    """The aggregator does NOT short-circuit — when a single decision
    violates several rules, all errors come back in one pass so the
    operator can debug them together."""
    # OnChain pick is TON (non-stable) so the 0.50 effective cap bites.
    s = _snapshot(
        deviation_bps=-150.0,
        onchain_products=[
            _product("8", "OnChain", coin="TON", effective_apr="0.18"),
        ],
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
    )
    d = _decision(
        confidence=0.3,
        risk_flags=["flag-a"],
        venues=[
            _venue("cash_usdc", 0.05),  # below 0.10 floor
            # Non-stable TON pick at 0.60 → > 0.50 effective cap
            _venue("bybit_onchain", 0.60, [("8", 1.0)]),
            _venue("bybit_flex", 0.35, [("1131", 1.0)]),
        ],
    )
    ok, errors = validate(d, s)
    assert ok is False
    assert len(errors) >= 4
    joined = " | ".join(errors)
    assert "confidence" in joined
    assert "risk_flags" in joined
    assert "cash_usdc" in joined  # floor
    assert "bybit_onchain" in joined  # effective pick cap (non-stable)


def test_validate_clean_decision_passes() -> None:
    s = _snapshot()
    d = _decision()
    ok, errors = validate(d, s)
    assert ok, errors


# ─── Hedge rules (.31) ──────────────────────────────────────────────────────


def _hedged_decision(
    hedge_notional: float = -50.0,
    *,
    onchain_pick_weight: float = 1.0,
    onchain_venue_weight: float = 0.5,
) -> Decision:
    """TON OnChain pick + matching TON short hedge. Defaults size hedge
    against a $50 pick on a $100 book."""
    return Decision(
        thesis="Hedged TON OnChain at 18% with short perp leg.",
        venues=[
            _venue("cash_usdc", 1.0 - onchain_venue_weight),
            _venue("bybit_onchain", onchain_venue_weight, [("8", onchain_pick_weight)]),
        ],
        hedges=[Hedge(coin="TON", notional_usd=hedge_notional)],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=12.0,
    )


def _onchain_ton(product_id: str = "8") -> list[ProductSummary]:
    return [_product(product_id, "OnChain", coin="TON", effective_apr="0.18")]


def test_check_hedges_for_non_usd_picks_fails_when_pick_below_perp_min_notional() -> None:
    """Non-stable OnChain pick must clear perp `min_notional_usd` to be
    hedgeable. After 2026-05-29: hedge size = pick USD, so the validator
    checks `pick_usd >= min_notional_usd` instead of `hedge.notional_usd`."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", min_notional="100.0")},
    )
    # Pick USD = 100 * 0.5 * 1.0 = 50 < 100 floor → un-hedgeable.
    d = _hedged_decision(hedge_notional=-50.0)
    ok, msg = check_hedges_for_non_usd_picks(d, s)
    assert ok is False
    assert "below" in (msg or "")
    assert "min_notional" in (msg or "")


def test_check_hedges_for_non_usd_picks_fails_when_perp_market_missing() -> None:
    """No perp_market entry for the coin → can't hedge → reject pick."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={},
    )
    d = _hedged_decision(hedge_notional=-50.0)
    ok, msg = check_hedges_for_non_usd_picks(d, s)
    assert ok is False
    assert "no perp_market" in (msg or "")


def test_check_hedges_for_non_usd_picks_uses_decimal_on_borderline_size() -> None:
    """`check_hedges_for_non_usd_picks` is Decimal-based — a pick sized
    EXACTLY at the perp `min_notional_usd` must pass without a
    float-precision off-by-cents reject. Pre-fix the same case could
    flip pass/fail depending on `total_book * float(weight)` rounding
    at large notionals."""
    # total_equity 100 × venue 1.0 × pick 1.0 = 100 USD pick → exactly
    # equal to a 100 USD min_notional → passes (strict `<` comparison).
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", min_notional="100.0")},
    )
    d = _hedged_decision(hedge_notional=-100.0, onchain_venue_weight=1.0)
    assert check_hedges_for_non_usd_picks(d, s) == (True, None)


def test_check_funding_rate_floor_fails_when_7d_avg_below_floor() -> None:
    """Persistent negative funding → hedge is net cost → exit pick."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="-0.0005")},
    )
    d = _hedged_decision(hedge_notional=-50.0)
    ok, msg = check_funding_rate_floor(d, s)
    assert ok is False
    assert "funding" in (msg or "")
    assert "TON" in (msg or "")


def test_check_funding_rate_floor_exempts_held_position_kept() -> None:
    """Regression (`bybit-sandbox.65`, prod 2026-06-07): a held non-stable
    position the LLM KEEPS at its current size must NOT be rejected for
    sub-floor funding. The live blocker was a TON OnChain stake in
    `Processing` status (un-redeemable) with funding -30%/yr — the prompt
    forces holding Processing picks, but the floor demanded an impossible
    exit, stranding every cycle as skipped:invalid. The floor gates only
    NEW/grown exposure (net-new >= MIN_ACTION)."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", funding_rate_7d_avg="-0.0005")},
        earn_positions=[_held_ton("13"), _held_ton("12")],  # 25 TON × $2 = $50 held
    )
    d = _hedged_decision(hedge_notional=-50.0)  # onchain 0.5 × $100 = $50 target == held
    assert check_funding_rate_floor(d, s) == (True, None)


def test_check_funding_rate_floor_fires_when_growing_held_below_floor() -> None:
    """Growing a sub-floor position adds fresh funding bleed → still
    rejected. Held $20, target $50 → net-new $30 (>= MIN_ACTION) → fire."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", funding_rate_7d_avg="-0.0005")},
        earn_positions=[_held_ton("10")],  # 10 TON × $2 = $20 held
    )
    d = _hedged_decision(hedge_notional=-50.0)  # $50 target → +$30 new
    ok, msg = check_funding_rate_floor(d, s)
    assert ok is False
    assert "TON" in (msg or "")


def test_check_funding_rate_floor_passes_when_positive() -> None:
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.00012")},
    )
    d = _hedged_decision(hedge_notional=-50.0)
    assert check_funding_rate_floor(d, s) == (True, None)


def test_check_funding_rate_floor_passes_when_missing() -> None:
    """No 7d avg available → no signal → don't block."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON")},  # no funding_rate_7d_avg
    )
    d = _hedged_decision(hedge_notional=-50.0)
    assert check_funding_rate_floor(d, s) == (True, None)


def test_check_funding_rate_floor_passes_at_threshold() -> None:
    """Exactly at the floor passes (strict-less-than comparison).

    The floor is annualized (`FUNDING_FLOOR_HEDGE_ANNUAL = -10.95%/year`);
    its per-period equivalent at the default 8h cadence is -0.0001 per
    period. `_perp` doesn't set `funding_interval_hours`, so the
    validator's `_annual_funding` falls back to 8h — same arithmetic
    the pre-2026-06-03 per-period comparison did.
    """
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={
            "TON": _perp("TON", funding_rate_7d_avg="-0.0001")
        },
    )
    d = _hedged_decision(hedge_notional=-50.0)
    assert check_funding_rate_floor(d, s) == (True, None)


def test_check_funding_rate_floor_passes_high_apr_despite_negative_funding() -> None:
    """2026-06-08 (the yield fix): a high Earn APR that more than covers a
    deeply-negative funding cost is a PRIME controllable-risk pick, NOT a
    reject. The OLD raw-funding floor killed it (funding -54.75%/yr <
    -10.95%), forcing the agent into ~3% stables. Now gated on the NET:
    gross 100% Earn + (-54.75%) funding - 1.8% friction = +43.5% net > 0
    → PASS. (Live: ME 101% Earn / -37% funding ⇒ +64% net delta-neutral.)"""
    s = _snapshot(
        onchain_products=[
            _product("8", "OnChain", coin="TON", effective_apr="1.0")
        ],
        perp_market={"TON": _perp("TON", mark="2.0", funding_rate_7d_avg="-0.0005")},
    )
    d = _hedged_decision(hedge_notional=-50.0)  # net-new TON pick, net +43.5%
    assert check_funding_rate_floor(d, s) == (True, None)


def test_check_funding_rate_floor_rejects_net_negative_hedge() -> None:
    """Net-NEGATIVE hedges still rejected: a low Earn APR the funding cost
    overwhelms (gross 5% - 54.75% funding - 1.8% = -51.5% net ≤ 0)."""
    s = _snapshot(
        onchain_products=[
            _product("8", "OnChain", coin="TON", effective_apr="0.05")
        ],
        perp_market={"TON": _perp("TON", mark="2.0", funding_rate_7d_avg="-0.0005")},
    )
    d = _hedged_decision(hedge_notional=-50.0)
    ok, msg = check_funding_rate_floor(d, s)
    assert ok is False
    assert "net-of-hedge" in (msg or "") and "TON" in (msg or "")
    # Sanity: the rate above exactly equals the annualized floor when
    # annualized at the 8h default. Guards against future floor changes.
    assert float(Decimal("-0.0001") * Decimal("1095")) == pytest.approx(
        FUNDING_FLOOR_HEDGE_ANNUAL
    )


def test_aggregate_validate_passes_hedged_non_usd_pick() -> None:
    """End-to-end: validator accepts a TON OnChain pick when the hedge
    is sized correctly and the perp market entry clears min-notional.
    `bybit_onchain.max_weight=0.40` (registry), so the pick goes at 40%
    of book → $40 hedge target ±20% tolerance. CONFIRMED apr_source
    (`apr_history`) so the unconfirmed-estimate probe cap doesn't apply —
    a measured/historical rate may be sized to the venue cap."""
    s = _snapshot(
        onchain_products=[
            _product("8", "OnChain", coin="TON", effective_apr="0.18",
                     apr_source="apr_history")
        ],
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        # liquid covers $40 spot + $42 perp margin
        liquid_usdc_usd="60",
        liquid_usdt_usd="30",
    )
    d = _hedged_decision(
        hedge_notional=-40.0,
        onchain_venue_weight=0.40,
    )
    ok, errors = validate(d, s)
    assert ok, errors


# ─── check_stable_spend_cap (2026-06-03) ───────────────────────────────────


def test_check_stable_spend_cap_passes_with_no_non_stable_picks() -> None:
    """All-stable decision (cash + USD1 flex) bypasses the cap entirely —
    nothing to count, no constraint to enforce."""
    s = _snapshot(liquid_usdc_usd="50", liquid_usdt_usd="0")
    d = _decision()  # default: cash 50% + flex USD1 50%
    assert check_stable_spend_cap(d, s) == (True, None)


def test_check_stable_spend_cap_passes_when_liquid_unset() -> None:
    """No liquid data (legacy fixture / pre-pivot) → no-op, matches the
    executor-side `_enforce_*_budget` early-out semantics."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        liquid_usdc_usd="0",
        liquid_usdt_usd="0",
    )
    d = _hedged_decision(onchain_venue_weight=0.40)
    assert check_stable_spend_cap(d, s) == (True, None)


def test_check_stable_spend_cap_fails_when_demand_exceeds_supply() -> None:
    """Non-stable pick at $40 → demand $40 spot + $42 margin = $82.
    Liquid stables $50 → cap exceeded, reject."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        liquid_usdc_usd="20",
        liquid_usdt_usd="30",
    )
    d = _hedged_decision(onchain_venue_weight=0.40)
    ok, msg = check_stable_spend_cap(d, s)
    assert not ok
    assert msg is not None
    assert "non-stable spend" in msg
    assert "exceeds liquid stables" in msg


def test_check_stable_spend_cap_passes_when_supply_sufficient() -> None:
    """Same pick at $40 (demand $82) but liquid stables $100 → fits."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        liquid_usdc_usd="60",
        liquid_usdt_usd="40",
    )
    d = _hedged_decision(onchain_venue_weight=0.40)
    assert check_stable_spend_cap(d, s) == (True, None)


def test_check_stable_spend_cap_ignores_stable_picks() -> None:
    """Stable Earn picks (USDT/USD1) don't count — they're funded by USDC
    Sell swap and capped by the executor's USDC budget separately."""
    s = _snapshot(liquid_usdc_usd="1", liquid_usdt_usd="0")
    # Even a tiny $1 liquid pool passes because the picks are stable
    d = _decision()  # cash + USD1 flex
    assert check_stable_spend_cap(d, s) == (True, None)


def test_check_stable_spend_cap_skips_pick_without_perp_market() -> None:
    """Picks lacking a perp pair are rejected by
    `check_hedges_for_non_usd_picks`; this rule must not double-count
    them as demand (or it'd produce a confusing duplicate error)."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={},  # no TON perp → un-hedgeable
        liquid_usdc_usd="1",  # absurdly low, but no demand counted
        liquid_usdt_usd="0",
    )
    d = _hedged_decision(onchain_venue_weight=0.40)
    # stable_spend_cap passes (no demand). The actual rejection comes
    # from check_hedges_for_non_usd_picks elsewhere.
    assert check_stable_spend_cap(d, s) == (True, None)


def _held_ton(amount: str, *, product_id: str = "8", status: str = "Active") -> dict:
    """One OnChain TON earn-position row (Bybit shape: native `amount`)."""
    return {
        "productId": product_id,
        "coin": "TON",
        "amount": amount,
        "category": "OnChain",
        "status": status,
    }


def test_check_stable_spend_cap_holds_existing_position_no_new_spend() -> None:
    """Regression (`bybit-sandbox.65`): a pick KEPT at its currently-held
    size funds nothing from the liquid pool, so it must pass even when the
    held position dwarfs liquid stables. Pre-fix this gross-counted the
    held TON as fresh $40 spend + $42 margin and rejected every hold,
    stranding the agent in a `skipped:invalid` loop on a fully-deployed
    small vault."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        liquid_usdc_usd="15",  # far below the held position's $40
        liquid_usdt_usd="0",
        # 20 TON × $2.0 mark = $40 held — matches the 40% target below
        earn_positions=[_held_ton("12"), _held_ton("8")],  # summed = 20 TON
    )
    d = _hedged_decision(onchain_venue_weight=0.40)  # $100 × 0.40 = $40 target
    assert check_stable_spend_cap(d, s) == (True, None)


def test_check_stable_spend_cap_counts_only_the_increase_over_held() -> None:
    """Only `target − held` draws on the liquid pool. Held $20, target $40
    → net-new $20 (spot) + $21 (margin) = $41 > $15 liquid → reject, and
    the error names the net-new figure, not the gross target."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        liquid_usdc_usd="15",
        liquid_usdt_usd="0",
        earn_positions=[_held_ton("10")],  # 10 TON × $2.0 = $20 held
    )
    d = _hedged_decision(onchain_venue_weight=0.40)  # $40 target
    ok, msg = check_stable_spend_cap(d, s)
    assert not ok
    assert msg is not None
    assert "$20.00 new" in msg
    assert "held $20.00" in msg
    assert "exceeds liquid stables" in msg


# ─── check_stable_earn_funding (2026-06-07, `bybit-sandbox.65`) ────────────


def _held(category: str, pid: str, coin: str, amount: str, *, status: str = "") -> dict:
    """One earn-position row (Bybit shape)."""
    return {
        "productId": pid,
        "coin": coin,
        "amount": amount,
        "category": category,
        "status": status,
    }


def _stable_onchain_products() -> list[ProductSummary]:
    return [
        _product("25", "OnChain", coin="USDT", effective_apr="0.0374"),
        _product("26", "OnChain", coin="USDC", effective_apr="0.0338"),
    ]


def test_check_stable_earn_funding_rejects_over_commit_vs_liquid() -> None:
    """Regression (prod 2026-06-07, `retCode=180016`): the LLM keeps USD1
    Flex and still subscribes ~$50 of fresh OnChain USDC/USDT against $6
    liquid. NEW stable spend must fit liquid + freed-by-redeem."""
    s = _snapshot(
        flex_products=[_product("1131", "FlexibleSaving", coin="USD1", effective_apr="0.0085")],
        onchain_products=_stable_onchain_products(),
        total_equity_usd="78",
        liquid_usdc_usd="5.94",
        liquid_usdt_usd="0.01",
        earn_positions=[_held("FlexibleSaving", "1131", "USD1", "15.6")],  # kept, frees nothing
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.10),
            _venue("bybit_flex", 0.20, [("1131", 1.0)]),       # keep USD1 (~$15.6, delta≈0)
            _venue("bybit_onchain", 0.70, [("25", 0.5), ("26", 0.5)]),  # +$27 each new
        ]
    )
    ok, msg = check_stable_earn_funding(d, s)
    assert not ok
    assert "new stable Earn spend" in (msg or "")
    assert "180016" in (msg or "")


def test_check_stable_earn_funding_allows_funded_rotation() -> None:
    """Dropping USD1 frees its redeemable capital, which funds a new USDC
    OnChain subscribe — the executor redeems before it subscribes, so the
    rotation IS fundable and must pass."""
    s = _snapshot(
        flex_products=[_product("1131", "FlexibleSaving", coin="USD1", effective_apr="0.0085")],
        onchain_products=_stable_onchain_products(),
        total_equity_usd="78",
        liquid_usdc_usd="6",
        liquid_usdt_usd="0",
        earn_positions=[_held("FlexibleSaving", "1131", "USD1", "40")],  # dropped → frees $40
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.49),
            _venue("bybit_onchain", 0.51, [("26", 1.0)]),  # ~$40 new USDC, funded by USD1 redeem + $6 liquid
        ]
    )
    assert check_stable_earn_funding(d, s) == (True, None)


def test_check_stable_earn_funding_processing_source_not_freeable() -> None:
    """A `Processing` stable stake can't be redeemed in time, so dropping
    it does NOT free capital for a same-cycle subscribe → reject."""
    s = _snapshot(
        flex_products=[_product("1131", "FlexibleSaving", coin="USD1", effective_apr="0.0085")],
        onchain_products=_stable_onchain_products(),
        total_equity_usd="78",
        liquid_usdc_usd="6",
        liquid_usdt_usd="0",
        # held USDC OnChain $40 but Processing → not freeable
        earn_positions=[_held("OnChain", "26", "USDC", "40", status="Processing")],
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.49),
            _venue("bybit_onchain", 0.51, [("25", 1.0)]),  # +$40 new USDT, only $6 freeable
        ]
    )
    ok, msg = check_stable_earn_funding(d, s)
    assert not ok
    assert "freed-by-redeem $0.00" in (msg or "")


def test_check_stable_earn_funding_slow_onchain_redeem_not_freeable() -> None:
    """`.63`: dropping a SETTLED (redeemable) OnChain stable still doesn't
    free capital this cycle — an OnChain redeem settles ~4d out, so it can't
    fund a same-cycle subscribe. Validator must exclude it (mirrors the
    executor defer). Same shape as `allows_funded_rotation` but the freed
    source is OnChain (slow) instead of FlexibleSaving (fast) → must REJECT."""
    s = _snapshot(
        flex_products=[_product("1131", "FlexibleSaving", coin="USD1", effective_apr="0.0085")],
        onchain_products=_stable_onchain_products(),
        total_equity_usd="78",
        liquid_usdc_usd="6",
        liquid_usdt_usd="0",
        # held USDC OnChain $40, SETTLED (redeemable) — but OnChain is slow.
        earn_positions=[_held("OnChain", "26", "USDC", "40")],
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.49),
            _venue("bybit_flex", 0.51, [("1131", 1.0)]),  # +$40 new USD1, only $6 liquid
        ]
    )
    ok, msg = check_stable_earn_funding(d, s)
    assert not ok
    assert "freed-by-redeem $0.00" in (msg or "")


def test_check_stable_earn_funding_holds_pass() -> None:
    """Keeping a stable position (target ≈ held) is no new spend → pass,
    even with a tiny liquid pool."""
    s = _snapshot(
        flex_products=[_product("1131", "FlexibleSaving", coin="USD1", effective_apr="0.0085")],
        total_equity_usd="78",
        liquid_usdc_usd="6",
        liquid_usdt_usd="0",
        earn_positions=[_held("FlexibleSaving", "1131", "USD1", "15.6")],
    )
    # bybit_flex 0.2 × $78 = $15.6 ≈ held → no new spend
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.80),
            _venue("bybit_flex", 0.20, [("1131", 1.0)]),
        ]
    )
    assert check_stable_earn_funding(d, s) == (True, None)


def test_check_combined_stable_liquidity_rejects_joint_overrun() -> None:
    """validator-5: a NEW non-stable hedged pick ($4 → $8.20 USDT draw) and a
    NEW stable subscribe ($4) each fit the $10 pool alone, but together demand
    $12.20 > $10. The two own-side gates pass; the combined gate rejects."""
    s = _snapshot(
        flex_products=[_product("9001", "FlexibleSaving", coin="ID", apr_source="estimate_apr")],
        perp_market={"ID": _perp("ID")},
        total_equity_usd="100",
        liquid_usdc_usd="10",
        liquid_usdt_usd="0",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.92),
            _venue("bybit_flex", 0.04, [("9001", 1.0)]),     # $4 non-stable → $8.20
            _venue("bybit_onchain", 0.04, [("26", 1.0)]),    # $4 new USDC stable
        ]
    )
    assert check_stable_spend_cap(d, s) == (True, None)
    assert check_stable_earn_funding(d, s) == (True, None)
    ok, msg = check_combined_stable_liquidity(d, s)
    assert ok is False
    assert "combined stable-pool demand" in (msg or "")


def test_check_combined_stable_liquidity_passes_within_pool() -> None:
    """Same demands ($8.20 + $4 = $12.20) fit a larger $15 pool → pass."""
    s = _snapshot(
        flex_products=[_product("9001", "FlexibleSaving", coin="ID", apr_source="estimate_apr")],
        perp_market={"ID": _perp("ID")},
        total_equity_usd="100",
        liquid_usdc_usd="15",
        liquid_usdt_usd="0",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.92),
            _venue("bybit_flex", 0.04, [("9001", 1.0)]),
            _venue("bybit_onchain", 0.04, [("26", 1.0)]),
        ]
    )
    assert check_combined_stable_liquidity(d, s) == (True, None)


def test_check_combined_stable_liquidity_noop_when_no_spend() -> None:
    """No new spend on either side → no-op pass (the default clean decision)."""
    assert check_combined_stable_liquidity(_decision(), _snapshot()) == (True, None)


# ─── check_min_stake (2026-06-04, `.51`) ──────────────────────────────────


def test_check_min_stake_passes_default_clean() -> None:
    """Default decision (USD1 flex at $50 on $100 book) sits far above
    any realistic min_subscribe_usd — fixture doesn't even set one."""
    s = _snapshot()
    d = _decision()
    assert check_min_stake(d, s) == (True, None)


def test_check_min_stake_no_op_when_book_zero() -> None:
    s = _snapshot(total_equity_usd="0")
    d = _decision()
    assert check_min_stake(d, s) == (True, None)


def test_check_min_stake_no_op_when_min_unset() -> None:
    """A product without `min_subscribe_usd` must not trigger the rule —
    Bybit's missing minStakeAmount field is common on legacy products."""
    s = _snapshot(
        flex_products=[
            _product(
                "1131", "FlexibleSaving",
                coin="USD1", effective_apr="0.075",
                min_subscribe_usd=None,
            )
        ],
        total_equity_usd="100",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    assert check_min_stake(d, s) == (True, None)


def test_check_min_stake_fails_below_floor() -> None:
    """Tiny FlexibleSaving pick (e.g. ID at $1.79 floor) gets a sub-min
    allocation when Claude over-diversifies on a small vault. Validator
    must reject so the next cycle's prior-decision summary surfaces the
    violation."""
    s = _snapshot(
        flex_products=[
            _product(
                "id-1", "FlexibleSaving",
                coin="ID", effective_apr="0.30",
                min_subscribe_usd="1.79",
            )
        ],
        total_equity_usd="50",
        # ID coin needs a perp entry to bypass other hedge rules in
        # the test (not what we're exercising here).
        perp_market={"ID": _perp("ID", mark="0.5", min_notional="0.1")},
    )
    # ID pick at 2% of $50 book = $1.00 — below the $1.79 floor.
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.98),
            _venue("bybit_flex", 0.02, [("id-1", 1.0)]),
        ]
    )
    ok, msg = check_min_stake(d, s)
    assert not ok
    assert "bybit_flex/id-1" in (msg or "")
    assert "180012" in (msg or "")


def test_check_min_stake_passes_at_floor() -> None:
    """Pick sized exactly at min_subscribe_usd passes (the `+1e-9`
    tolerance handles fp comparison without spurious rejection)."""
    s = _snapshot(
        flex_products=[
            _product(
                "id-1", "FlexibleSaving",
                coin="ID", effective_apr="0.30",
                min_subscribe_usd="2.00",
            )
        ],
        total_equity_usd="100",
        perp_market={"ID": _perp("ID", mark="0.5", min_notional="0.1")},
    )
    # 2% of $100 = $2.00 — exactly at floor.
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.98),
            _venue("bybit_flex", 0.02, [("id-1", 1.0)]),
        ]
    )
    assert check_min_stake(d, s) == (True, None)


def test_check_min_stake_fails_lm_below_floor() -> None:
    """Mirror for LM picks — the validator applies to every venue where
    the snapshot category populates `min_subscribe_usd`. LM has the
    deepest floors ($50 for BTC/USDC, ETH/USDC) — easy to trip on a
    small vault."""
    s = _snapshot(
        lm_products=[
            _product(
                "24", "LiquidityMining",
                coin="ETH/USDC", effective_apr="0.025",
                apr_source="apy_e8",
                notes=["max_leverage=1"],
                min_subscribe_usd="50",
            )
        ],
        total_equity_usd="100",
    )
    # 20% of $100 = $20 — below $50 LM floor.
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.80),
            _venue("bybit_lm", 0.20, [("24", 1.0)]),
        ]
    )
    ok, msg = check_min_stake(d, s)
    assert not ok
    assert "bybit_lm/24" in (msg or "")


def test_check_min_stake_holding_below_floor_does_not_fire() -> None:
    """Regression (`bybit-sandbox.65`): a held Earn position kept at its
    current size places NO subscribe (the diff's delta ≈ 0), so it can't
    trip Bybit's `retCode=180012` even if the position now sits below the
    product's current `min_subscribe_usd`. Pre-fix the gross compare
    falsely rejected the hold."""
    s = _snapshot(
        onchain_products=[
            _product(
                "8", "OnChain", coin="TON", effective_apr="0.18",
                min_subscribe_usd="100",  # held $40 sits under today's floor
            )
        ],
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        total_equity_usd="100",
        earn_positions=[_held_ton("20")],  # 20 TON × $2.0 = $40 held
    )
    d = _hedged_decision(onchain_venue_weight=0.40)  # $40 target == held → no subscribe
    assert check_min_stake(d, s) == (True, None)


def test_check_min_stake_held_sub_min_sliver_does_not_fire() -> None:
    """Regression: a held position the LLM KEEPS drifts target-vs-held by
    weight quantization — e.g. $0.56 on a $40 held stake — producing a
    sub-min net-new the executor SKIPS (holds the position, no 180012).
    Pre-fix this $0.50–$10 band falsely rejected the hold and stranded the
    cycle skipped:invalid (the held-Processing-USDC loop hang)."""
    s = _snapshot(
        onchain_products=[
            _product("26", "OnChain", coin="USDC", effective_apr="0.04", min_subscribe_usd="10")
        ],
        total_equity_usd="100",
        earn_positions=[_held("OnChain", "26", "USDC", "40", status="Processing")],
    )
    # target $41 vs held $40 → $1 sub-min sliver on a held position.
    d = _decision(venues=[_venue("cash_usdc", 0.59), _venue("bybit_onchain", 0.41, [("26", 1.0)])])
    assert check_min_stake(d, s) == (True, None)


def test_check_min_stake_fresh_sub_min_pick_still_fires() -> None:
    """Intent preserved: a FRESH sub-min pick (no held position) is dropped
    wholesale by the executor and must still surface — only held-position
    slivers are exempted."""
    s = _snapshot(
        onchain_products=[
            _product("26", "OnChain", coin="USDC", effective_apr="0.04", min_subscribe_usd="10")
        ],
        total_equity_usd="100",
    )
    d = _decision(venues=[_venue("cash_usdc", 0.95), _venue("bybit_onchain", 0.05, [("26", 1.0)])])
    ok, msg = check_min_stake(d, s)  # $5 fresh < $10 min
    assert not ok
    assert "bybit_onchain/26" in (msg or "")


def test_check_min_stake_collects_multiple_violations() -> None:
    """All violations surface in a single error message — operator sees
    every problem in one pass."""
    s = _snapshot(
        flex_products=[
            _product(
                "id-1", "FlexibleSaving",
                coin="ID", effective_apr="0.30",
                min_subscribe_usd="1.79",
            ),
            _product(
                "io-1", "FlexibleSaving",
                coin="IO", effective_apr="0.25",
                min_subscribe_usd="1.28",
            ),
        ],
        total_equity_usd="50",
        perp_market={
            "ID": _perp("ID", mark="0.5", min_notional="0.1"),
            "IO": _perp("IO", mark="0.7", min_notional="0.1"),
        },
    )
    # Both at 1% of $50 = $0.50 each — below their respective floors.
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.98),
            _venue(
                "bybit_flex", 0.02,
                [("id-1", 0.5), ("io-1", 0.5)],
            ),
        ]
    )
    ok, msg = check_min_stake(d, s)
    assert not ok
    assert "id-1" in (msg or "")
    assert "io-1" in (msg or "")


# ─── check_capital_flow_simulation (2026-06-04, `.50`) ────────────────────


def test_check_capital_flow_passes_on_clean_default() -> None:
    """Default decision (cash 50% + USD1 flex 50%) is well under the
    capital-flow ceiling — stable picks commit at face value, no margin
    layer, comfortably within `book × (1 - cash_floor)`."""
    s = _snapshot(total_equity_usd="100")
    d = _decision()
    assert check_capital_flow_simulation(d, s) == (True, None)


def test_check_capital_flow_no_op_when_book_zero() -> None:
    """`total_equity_usd == 0` is the no-op guard (legacy fixture / pre-
    pivot snapshot) — same shape as `check_stable_spend_cap`."""
    s = _snapshot(total_equity_usd="0")
    d = _decision()
    assert check_capital_flow_simulation(d, s) == (True, None)


def test_check_capital_flow_fails_hedged_overflow() -> None:
    """OnChain TON 45% + cash 55% on $100, TON perp available → commit
    = 45 × 2.05 = $92.25, allowable = $100 × (1 - 0.10) = $90 → reject.
    Hedged non-stable picks layer stake (100% of pick_usd) + perp margin
    (~105% of pick_usd) so each non-stable book-dollar locks 2.05×."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        total_equity_usd="100",
    )
    d = _hedged_decision(onchain_venue_weight=0.45)
    ok, msg = check_capital_flow_simulation(d, s)
    assert not ok
    assert msg is not None
    assert "target capital commitment" in msg
    assert "exceeds" in msg


def test_check_capital_flow_passes_hedged_within_cap() -> None:
    """OnChain TON 40% + cash 60% on $100 → commit = 40 × 2.05 = $82,
    allowable = $90 → fits with headroom."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        total_equity_usd="100",
    )
    d = _hedged_decision(onchain_venue_weight=0.40)
    assert check_capital_flow_simulation(d, s) == (True, None)


def test_check_capital_flow_holding_nonstable_does_not_reject() -> None:
    """`bybit-sandbox` 2026-06-08: net-new scoping. A held non-stable Earn
    position the LLM re-states as a HOLD (target ≈ held) commits no NEW
    capital, so it must NOT trip the rule — even though its gross 2.05×
    would exceed the ceiling. Pre-fix this rejected the very hold the prompt
    mandates for an un-redeemable Processing position (live: TON)."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        total_equity_usd="100",
        # Held TON OnChain ≈ $45 (22.5 × mark 2.0) — same size as the pick.
        earn_positions=[_held("OnChain", "8", "TON", "22.5")],
    )
    # Re-state the held TON at 45% of book ($45) — a pure hold, net_new ≈ 0.
    d = _hedged_decision(onchain_venue_weight=0.45)
    assert check_capital_flow_simulation(d, s) == (True, None)


def test_check_capital_flow_growing_nonstable_still_caught() -> None:
    """Net-new scoping still catches GROWTH: held TON ≈ $10 grown to a $55
    target → net_new $45 × 2.05 = $92.25 > $90 ceiling → reject. The
    perp-OPEN risk the rule guards is a net-new event, so opening fresh
    exposure past the buffer must still fail."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        total_equity_usd="100",
        earn_positions=[_held("OnChain", "8", "TON", "5")],  # ≈ $10 held
    )
    d = _hedged_decision(onchain_venue_weight=0.55)  # $55 target → $45 new
    ok, msg = check_capital_flow_simulation(d, s)
    assert not ok
    assert msg is not None and "target capital commitment" in msg


def test_check_capital_flow_fails_mixed_venues_overflow() -> None:
    """OnChain TON 30% (hedged) + LM 30% + cash 40% on $100 → commit
    = 30×2.05 + 30 + 0 = $61.50 + $30 = $91.50, allowable $90 → reject.
    Hidden hedge margin pushes a venue-cap-compliant portfolio over the
    book ceiling once the face-value LM slice is added in."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        total_equity_usd="100",
    )
    d = Decision(
        thesis="hedged onchain + LM split blows past book cap",
        venues=[
            _venue("cash_usdc", 0.40),
            _venue("bybit_onchain", 0.30, [("8", 1.0)]),
            _venue("bybit_lm", 0.30, [("24", 1.0)]),
        ],
        hedges=[Hedge(coin="TON", notional_usd=-30.0)],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=12.0,
    )
    ok, msg = check_capital_flow_simulation(d, s)
    assert not ok
    assert msg is not None
    assert "bybit_onchain" in msg
    assert "bybit_lm" in msg


def test_check_capital_flow_stable_picks_no_buffer() -> None:
    """Stable Earn picks commit at face value — no margin layer. USD1
    flex 90% + cash 10% on $100 → commit = $90, allowable = $90 → pass
    exactly at the cap."""
    s = _snapshot(
        flex_products=[
            _product(
                "1131", "FlexibleSaving",
                coin="USD1", effective_apr="0.075",
            )
        ],
        total_equity_usd="100",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.10),
            _venue("bybit_flex", 0.90, [("1131", 1.0)]),
        ]
    )
    # Violates bybit_flex.max_weight=0.70 but check_capital_flow_simulation
    # is the unit under test here — it should pass on the math alone.
    assert check_capital_flow_simulation(d, s) == (True, None)


def test_check_capital_flow_skips_unhedgeable_non_stable() -> None:
    """Non-stable pick lacking a `perp_market` entry isn't hedged
    (`check_hedges_for_non_usd_picks` rejects upstream) — counting
    margin here would produce a duplicate error. Pick commits at face."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={},  # no TON perp → un-hedgeable
        total_equity_usd="100",
    )
    d = _hedged_decision(onchain_venue_weight=0.85)
    # 85 × 1.0 = 85 ≤ 90 (allowable) → passes; the hedge feasibility
    # rejection happens in a different check.
    assert check_capital_flow_simulation(d, s) == (True, None)


def test_check_capital_flow_carry_pick_includes_buffer() -> None:
    """Carry picks always commit stake + margin. carry 0.10 + cash 0.20
    + flex stable 0.70 on $100 → commit = 10×2.05 + 70 = $90.50 > $90 →
    reject. Without the buffer the same allocation would read $80 and
    pass — the rule exists exactly to catch this hidden overhead."""
    s = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.0001")},
        total_equity_usd="100",
    )
    d = Decision(
        thesis="carry + stable mix overflows once margin is added",
        venues=[
            _venue("cash_usdc", 0.20),
            _venue("bybit_flex", 0.70, [("1131", 1.0)]),
            _venue("bybit_funding_carry", 0.10, [("TONUSDT", 1.0)]),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=8.0,
    )
    ok, msg = check_capital_flow_simulation(d, s)
    assert not ok
    assert "bybit_funding_carry" in (msg or "")


def test_check_capital_flow_carry_within_cap() -> None:
    """Same carry shape but cash buffer covers the hidden margin: carry
    0.08 + cash 0.22 + flex 0.70 → commit = 8×2.05 + 70 = $86.40 ≤ $90
    → passes."""
    s = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.0001")},
        total_equity_usd="100",
    )
    d = Decision(
        thesis="carry + stable mix sized within book cap",
        venues=[
            _venue("cash_usdc", 0.22),
            _venue("bybit_flex", 0.70, [("1131", 1.0)]),
            _venue("bybit_funding_carry", 0.08, [("TONUSDT", 1.0)]),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=8.0,
    )
    assert check_capital_flow_simulation(d, s) == (True, None)


def test_cash_floor_matches_registry() -> None:
    """`CASH_FLOOR` must mirror `cash_usdc.min_weight` — single source
    of truth invariant."""
    from agent.reason.venues import VENUE_REGISTRY
    assert CASH_FLOOR == float(VENUE_REGISTRY["cash_usdc"].min_weight)


# ─── InvalidateAt schema (2026-06-03) ──────────────────────────────────────


def test_invalidate_at_accepts_all_nulls() -> None:
    """All-null InvalidateAt is valid — semantically "use category defaults"."""
    iv = InvalidateAt()
    assert iv.price_below is None
    assert iv.peg_dev_above_bps is None


def test_invalidate_at_rejects_price_below_above_inverted() -> None:
    """price_below must be < price_above when both set — otherwise the
    pick exits immediately on the open (no live range)."""
    with pytest.raises(ValueError, match="price_below"):
        InvalidateAt(price_below=2.0, price_above=1.5)


def test_invalidate_at_accepts_price_below_under_above() -> None:
    iv = InvalidateAt(price_below=1.5, price_above=2.5)
    assert iv.price_below == 1.5
    assert iv.price_above == 2.5


def test_invalidate_at_accepts_funding_below_negative() -> None:
    """funding_7d_below is a signed per-8h rate — negative values mean
    'fire if funding turns more negative than this'. Must accept neg."""
    iv = InvalidateAt(funding_7d_below=-0.00015)
    assert iv.funding_7d_below == -0.00015


def test_pick_accepts_invalidate_at_override() -> None:
    """Pick model accepts InvalidateAt as an optional field; serializes
    via model_dump and round-trips through validate."""
    p = Pick(
        product_id="8",
        weight=1.0,
        invalidate_at=InvalidateAt(price_below=1.40, funding_7d_below=-0.00015),
    )
    dumped = p.model_dump()
    assert dumped["invalidate_at"]["price_below"] == 1.40
    assert dumped["invalidate_at"]["funding_7d_below"] == -0.00015
    # Round-trip
    p2 = Pick.model_validate(dumped)
    assert p2.invalidate_at == p.invalidate_at


def test_pick_omitting_invalidate_at_defaults_to_none() -> None:
    p = Pick(product_id="8", weight=1.0)
    assert p.invalidate_at is None


# ─── Funding-carry rules (`bybit-strategy-expansion.4`) ─────────────────────


def _carry_snapshot(
    *,
    carry_products: list[ProductSummary] | None = None,
    perp_market: dict[str, PerpInfo] | None = None,
    flex_products: list[ProductSummary] | None = None,
    onchain_products: list[ProductSummary] | None = None,
    total_equity_usd: str = "1000",
    liquid_usdc_usd: str = "0",
    liquid_usdt_usd: str = "0",
) -> Snapshot:
    """Variant of `_snapshot()` that also populates the FundingCarry
    category. Default carry product surfaces TON with friction-adjusted
    APR for the happy-path tests."""
    snap = _snapshot(
        flex_products=flex_products,
        onchain_products=onchain_products,
        perp_market=perp_market,
        total_equity_usd=total_equity_usd,
        liquid_usdc_usd=liquid_usdc_usd,
        liquid_usdt_usd=liquid_usdt_usd,
    )
    if carry_products is not None:
        snap.products["FundingCarry"] = carry_products
    return snap


def _carry_product(coin: str = "TON", apr: str = "0.20") -> ProductSummary:
    return _product(
        product_id=f"{coin}USDT",
        category="FundingCarry",
        coin=coin,
        effective_apr=apr,
        apr_source="funding_carry",
    )


def test_check_slow_settle_cap_blocks_new_onchain_when_over_cap() -> None:
    """2026-06-08: with 54% of book already frozen in OnChain Processing,
    a NEW OnChain subscribe is rejected (cap 50%) — stop freezing more,
    keep liquidity for high-net picks. Held Processing is exempt."""
    from agent.validate.rules import check_slow_settle_cap
    s = _snapshot(
        onchain_products=[
            _product("26", "OnChain", coin="USDC", effective_apr="0.034",
                     apr_source="apr_history"),
            _product("25", "OnChain", coin="USDT", effective_apr="0.038",
                     apr_source="apr_history"),
        ],
        total_equity_usd="180",
        # $97 held OnChain (Processing) = 54% of book
        earn_positions=[
            _held("OnChain", "26", "USDC", "67", status="Processing"),
            _held("OnChain", "25", "USDT", "30", status="Processing"),
        ],
        liquid_usdc_usd="10", liquid_usdt_usd="10",
    )
    # LLM tries to add MORE USDT OnChain (new spend) on top of the 54% held.
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.30),
            _venue("bybit_onchain", 0.70, [("26", 0.55), ("25", 0.45)]),
        ]
    )
    ok, msg = check_slow_settle_cap(d, s)
    assert not ok
    assert msg is not None and "slow-settle" in msg


def test_check_slow_settle_cap_exempts_held_only_hold() -> None:
    """A pure HOLD of the frozen OnChain book (no new spend) passes — the
    cap is net-new, never rejects an un-redeemable Processing hold."""
    from agent.validate.rules import check_slow_settle_cap
    s = _snapshot(
        onchain_products=[
            _product("26", "OnChain", coin="USDC", effective_apr="0.034",
                     apr_source="apr_history"),
        ],
        total_equity_usd="180",
        earn_positions=[_held("OnChain", "26", "USDC", "97", status="Processing")],
    )
    # target ≈ held ($97 ≈ 0.5375 × 180 × 1.0) → net-new ≈ 0 → exempt
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.4625),
            _venue("bybit_onchain", 0.5375, [("26", 1.0)]),
        ]
    )
    assert check_slow_settle_cap(d, s) == (True, None)


def test_check_estimate_apr_probe_cap_blocks_oversized_unconfirmed() -> None:
    """An UNCONFIRMED estimate_apr non-stable pick above the 7% probe cap is
    rejected — probe it, don't bet the cap on a quoted rate (ME mirage risk)."""
    from agent.validate.rules import check_estimate_apr_probe_cap
    s = _snapshot(
        flex_products=[
            _product("498", "FlexibleSaving", coin="ME", effective_apr="0.60",
                     apr_source="estimate_apr"),
        ],
        perp_market={"ME": _perp("ME", mark="0.06", min_notional="0.06")},
        total_equity_usd="180",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.60),
            _venue("bybit_flex", 0.40, [("498", 1.0)]),  # 40% >> 7% probe cap
        ]
    )
    ok, msg = check_estimate_apr_probe_cap(d, s)
    assert not ok
    assert msg is not None and "probe cap" in msg


def test_check_estimate_apr_probe_cap_allows_probe_size() -> None:
    """Same pick at probe size (≤7%) passes; a CONFIRMED source at any size
    is exempt (probe→scale)."""
    from agent.validate.rules import check_estimate_apr_probe_cap
    s = _snapshot(
        flex_products=[
            _product("498", "FlexibleSaving", coin="ME", effective_apr="0.60",
                     apr_source="estimate_apr"),
        ],
        perp_market={"ME": _perp("ME", mark="0.06", min_notional="0.06")},
        total_equity_usd="180",
    )
    probe = _decision(
        venues=[
            _venue("cash_usdc", 0.95),
            _venue("bybit_flex", 0.05, [("498", 1.0)]),  # 5% ≤ 7% probe
        ]
    )
    assert check_estimate_apr_probe_cap(probe, s) == (True, None)


# ─── Probe ladder (.1) — measured_yield intermediate tier ──────────────────


def _ladder_snapshot(apr_source: str) -> Snapshot:
    """ME non-stable Flex pick at 60% APR from the given source, $180 book."""
    return _snapshot(
        flex_products=[
            _product("498", "FlexibleSaving", coin="ME", effective_apr="0.60",
                     apr_source=apr_source),
        ],
        perp_market={"ME": _perp("ME", mark="0.06", min_notional="0.06")},
        total_equity_usd="180",
    )


def _ladder_decision(weight: float) -> Decision:
    return _decision(
        venues=[
            _venue("cash_usdc", round(1.0 - weight, 4)),
            _venue("bybit_flex", weight, [("498", 1.0)]),
        ]
    )


def test_probe_ladder_estimate_apr_still_capped_at_7pct() -> None:
    """estimate_apr tier unchanged: 40% >> 7% probe cap is rejected."""
    from agent.validate.rules import check_estimate_apr_probe_cap
    ok, msg = check_estimate_apr_probe_cap(
        _ladder_decision(0.40), _ladder_snapshot("estimate_apr")
    )
    assert not ok
    assert msg is not None and "probe cap" in msg and "7%" in msg


def test_probe_ladder_estimate_apr_passes_at_5pct() -> None:
    """estimate_apr at 5% (≤ 7%) passes."""
    from agent.validate.rules import check_estimate_apr_probe_cap
    assert check_estimate_apr_probe_cap(
        _ladder_decision(0.05), _ladder_snapshot("estimate_apr")
    ) == (True, None)


def test_probe_ladder_measured_yield_passes_at_25pct() -> None:
    """measured_yield unlocks the intermediate 30% tier: 25% passes."""
    from agent.validate.rules import check_estimate_apr_probe_cap
    assert check_estimate_apr_probe_cap(
        _ladder_decision(0.25), _ladder_snapshot("measured_yield")
    ) == (True, None)


def test_probe_ladder_measured_yield_blocked_at_45pct() -> None:
    """measured_yield above the 30% tier is rejected (msg names the tier)."""
    from agent.validate.rules import check_estimate_apr_probe_cap
    ok, msg = check_estimate_apr_probe_cap(
        _ladder_decision(0.45), _ladder_snapshot("measured_yield")
    )
    assert not ok
    assert msg is not None
    assert "measured_yield" in msg and "30%" in msg


def test_probe_ladder_measured_yield_held_is_exempt() -> None:
    """Net-new scoped: a 45% measured_yield pick that is already HELD (no new
    spend) passes — growing past the tier is gated, holding isn't."""
    from agent.validate.rules import check_estimate_apr_probe_cap
    s = _snapshot(
        flex_products=[
            _product("498", "FlexibleSaving", coin="ME", effective_apr="0.60",
                     apr_source="measured_yield"),
        ],
        perp_market={"ME": _perp("ME", mark="0.06", min_notional="0.06")},
        total_equity_usd="180",
        # held USD = amount × mark = 1350 × 0.06 = $81 = 45% of book ≈ target
        earn_positions=[_held("FlexibleSaving", "498", "ME", "1350")],
    )
    assert check_estimate_apr_probe_cap(_ladder_decision(0.45), s) == (True, None)


def test_probe_ladder_apr_history_exempt_at_55pct() -> None:
    """apr_history is noise-immune: exempt from the ladder (the 0.60 product
    cap governs it instead), so 55% passes here."""
    from agent.validate.rules import check_estimate_apr_probe_cap
    assert check_estimate_apr_probe_cap(
        _ladder_decision(0.55), _ladder_snapshot("apr_history")
    ) == (True, None)


def test_probe_ladder_stable_unaffected() -> None:
    """The ladder only governs NON-stable picks; a large stable estimate_apr
    pick is not touched here (its own caps apply elsewhere)."""
    from agent.validate.rules import check_estimate_apr_probe_cap
    s = _snapshot(
        flex_products=[
            _product("1131", "FlexibleSaving", coin="USD1", effective_apr="0.40",
                     apr_source="estimate_apr"),
        ],
        total_equity_usd="180",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.60),
            _venue("bybit_flex", 0.40, [("1131", 1.0)]),
        ]
    )
    assert check_estimate_apr_probe_cap(d, s) == (True, None)


# ─── Slow-settle stable preference (.6) ────────────────────────────────────


def test_slow_settle_pref_blocks_thin_margin_onchain_relock() -> None:
    """NEW OnChain stable yield is rejected when a same-coin Flex twin yields
    nearly the same (< 1.5%/yr margin) — route to liquid Flex, don't freeze."""
    from agent.validate.rules import check_slow_settle_stable_preference
    s = _snapshot(
        flex_products=[
            _product("100", "FlexibleSaving", coin="USDC", effective_apr="0.030",
                     apr_source="apr_history"),
        ],
        onchain_products=[
            _product("26", "OnChain", coin="USDC", effective_apr="0.034",
                     apr_source="apr_history",
                     effective_apr_net_holding="0.034"),
        ],
        total_equity_usd="180",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.40),
            _venue("bybit_onchain", 0.60, [("26", 1.0)]),
        ]
    )
    ok, msg = check_slow_settle_stable_preference(d, s)
    assert not ok
    assert msg is not None and "USDC" in msg and "Flex" in msg


def test_slow_settle_pref_exempts_sub_min_sliver_on_held() -> None:
    """Regression: a sub-min net-new sliver on a HELD OnChain stake (weight
    quantization on a hold) can't be placed as a subscribe, so it's not a
    'route to Flex' decision — exempt, don't reject. Pairs with the
    check_min_stake fix that unstrands held-Processing holds."""
    from agent.validate.rules import check_slow_settle_stable_preference
    s = _snapshot(
        flex_products=[
            _product("100", "FlexibleSaving", coin="USDC", effective_apr="0.030",
                     apr_source="apr_history"),
        ],
        onchain_products=[
            _product("26", "OnChain", coin="USDC", effective_apr="0.034",
                     apr_source="apr_history", effective_apr_net_holding="0.034",
                     min_subscribe_usd="10"),
        ],
        total_equity_usd="100",
        earn_positions=[_held("OnChain", "26", "USDC", "40", status="Processing")],
    )
    # target $41 vs held $40 → $1 sub-min sliver (< $10 min) → hold, not a relock.
    d = _decision(venues=[_venue("cash_usdc", 0.59), _venue("bybit_onchain", 0.41, [("26", 1.0)])])
    assert check_slow_settle_stable_preference(d, s) == (True, None)


def test_slow_settle_pref_allows_fat_margin_onchain() -> None:
    """When OnChain's dead-time-discounted net clears the 1.5%/yr margin over
    the Flex twin, the relock is justified — passes."""
    from agent.validate.rules import check_slow_settle_stable_preference
    s = _snapshot(
        flex_products=[
            _product("100", "FlexibleSaving", coin="USDC", effective_apr="0.010",
                     apr_source="apr_history"),
        ],
        onchain_products=[
            _product("26", "OnChain", coin="USDC", effective_apr="0.045",
                     apr_source="apr_history",
                     effective_apr_net_holding="0.040"),
        ],
        total_equity_usd="180",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.40),
            _venue("bybit_onchain", 0.60, [("26", 1.0)]),
        ]
    )
    assert check_slow_settle_stable_preference(d, s) == (True, None)


def test_slow_settle_pref_inert_without_flex_twin() -> None:
    """No same-coin Flex product → nowhere to route → inert (passes) even on a
    thin OnChain rate."""
    from agent.validate.rules import check_slow_settle_stable_preference
    s = _snapshot(
        flex_products=[
            _product("100", "FlexibleSaving", coin="USDT", effective_apr="0.030",
                     apr_source="apr_history"),
        ],
        onchain_products=[
            _product("26", "OnChain", coin="USDC", effective_apr="0.034",
                     apr_source="apr_history"),
        ],
        total_equity_usd="180",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.40),
            _venue("bybit_onchain", 0.60, [("26", 1.0)]),
        ]
    )
    assert check_slow_settle_stable_preference(d, s) == (True, None)


def test_slow_settle_pref_compares_best_feasible_flex_twin() -> None:
    """The gate compares OnChain against the BEST same-coin Flex twin that's
    actually fundable at the pick size — not an arbitrary first match. Here a
    thin high-APR twin (3.3%) is INFEASIBLE (min_subscribe $500 > $108 target);
    only the feasible 0.8% twin remains, so OnChain 3.4% clears the 1.5% margin
    over it and is NOT rejected (routing to the infeasible twin would strand
    the yield)."""
    from agent.validate.rules import check_slow_settle_stable_preference
    s = _snapshot(
        flex_products=[
            _product("100", "FlexibleSaving", coin="USDC", effective_apr="0.033",
                     apr_source="apr_history", min_subscribe_usd="500"),
            _product("101", "FlexibleSaving", coin="USDC", effective_apr="0.008",
                     apr_source="apr_history"),
        ],
        onchain_products=[
            _product("26", "OnChain", coin="USDC", effective_apr="0.034",
                     apr_source="apr_history", effective_apr_net_holding="0.034"),
        ],
        total_equity_usd="180",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.40),
            _venue("bybit_onchain", 0.60, [("26", 1.0)]),
        ]
    )
    # Best FEASIBLE twin is the 0.8% one (the 3.3% twin needs $500 > $108);
    # 3.4% − 0.8% = 2.6% ≥ 1.5% margin → allowed.
    assert check_slow_settle_stable_preference(d, s) == (True, None)


def test_slow_settle_pref_exempts_held_hold() -> None:
    """A pure HOLD of the OnChain stable (net-new < floor) is never rejected,
    even with a same-coin Flex twin at a near-equal rate."""
    from agent.validate.rules import check_slow_settle_stable_preference
    s = _snapshot(
        flex_products=[
            _product("100", "FlexibleSaving", coin="USDC", effective_apr="0.030",
                     apr_source="apr_history"),
        ],
        onchain_products=[
            _product("26", "OnChain", coin="USDC", effective_apr="0.034",
                     apr_source="apr_history"),
        ],
        total_equity_usd="180",
        # ~$108 held = 60% of book ≈ target → net-new ≈ 0
        earn_positions=[_held("OnChain", "26", "USDC", "108")],
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.40),
            _venue("bybit_onchain", 0.60, [("26", 1.0)]),
        ]
    )
    assert check_slow_settle_stable_preference(d, s) == (True, None)


def test_slow_settle_pref_ignores_nonstable_onchain() -> None:
    """The preference governs STABLE OnChain only; a non-stable OnChain pick is
    not touched (the probe/net-hedge rules cover it)."""
    from agent.validate.rules import check_slow_settle_stable_preference
    s = _snapshot(
        flex_products=[
            _product("100", "FlexibleSaving", coin="TON", effective_apr="0.030",
                     apr_source="apr_history"),
        ],
        onchain_products=[
            _product("26", "OnChain", coin="TON", effective_apr="0.034",
                     apr_source="apr_history"),
        ],
        perp_market={"TON": _perp("TON")},
        total_equity_usd="180",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.60),
            _venue("bybit_onchain", 0.40, [("26", 1.0)]),
        ]
    )
    assert check_slow_settle_stable_preference(d, s) == (True, None)


def test_check_funding_carry_floor_passes_when_above_floor() -> None:
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        # 7d avg = 0.0001/8h ≈ +11% annualized, above +0.00005 floor.
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.0001")},
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.9),
            _venue("bybit_funding_carry", 0.1, [("TONUSDT", 1.0)]),
        ]
    )
    assert check_funding_carry_floor(d, snap) == (True, None)


def test_check_funding_carry_floor_fails_below_floor() -> None:
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        # 7d avg = 0.00001/8h, well below +0.00005 floor.
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.00001")},
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.9),
            _venue("bybit_funding_carry", 0.1, [("TONUSDT", 1.0)]),
        ]
    )
    ok, msg = check_funding_carry_floor(d, snap)
    assert ok is False
    assert msg is not None and "below carry floor" in msg


def test_check_funding_carry_floor_fails_when_perp_market_missing() -> None:
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        perp_market={},  # TON missing entirely
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.9),
            _venue("bybit_funding_carry", 0.1, [("TONUSDT", 1.0)]),
        ]
    )
    ok, msg = check_funding_carry_floor(d, snap)
    assert ok is False
    assert msg is not None and "funding_rate_7d_avg" in msg


def test_check_funding_carry_floor_noop_when_no_carry_venue() -> None:
    snap = _carry_snapshot()
    d = _decision()  # default = cash + flex stable, no carry
    assert check_funding_carry_floor(d, snap) == (True, None)


def test_check_funding_carry_floor_message_keeps_precision() -> None:
    """Regression: prior `.1f` formatting rounded annualized rate AND
    floor to the same displayed value when the rate was just below the
    floor, making the operator-facing rejection look self-contradictory
    (`+5.5% below ... +5.5%`). `.3f` keeps enough digits to read the
    margin."""
    # 7d avg 0.0000500137/8h → annualized ≈ +5.476%, marginally below
    # the +5.475% floor. Pre-fix print: "+5.5%" vs "+5.5%". Post-fix:
    # "+5.476%" vs "+5.475%".
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.00004999")},
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.9),
            _venue("bybit_funding_carry", 0.1, [("TONUSDT", 1.0)]),
        ]
    )
    ok, msg = check_funding_carry_floor(d, snap)
    assert ok is False
    assert msg is not None
    # `.3f` is wired — at minimum the message must NOT contain the old
    # `+5.5%` collision; specifically the rate is displayed with three
    # decimals (`+5.474%`).
    assert "+5.474%" in msg, msg
    assert "+5.475%" in msg, msg


def test_check_funding_rate_floor_rejects_on_invalid_interval() -> None:
    """Pre-fix the hedge funding-floor check silently passed any pick
    whose annualization returned None (`_annual_funding` does so for
    `interval <= 0`). That hid genuinely broken snapshot data — the
    operator saw the pick approved instead of a data-quality error. Now
    the validator surfaces it the same way `check_funding_carry_floor`
    already does. A negative interval is the only realistic trigger
    (the `or` fallback substitutes 0 with the 8h default), but the
    handling must be defensive regardless."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={
            "TON": _perp(
                "TON",
                funding_rate_7d_avg="0.0001",
                funding_interval_hours="-4",
            ),
        },
    )
    d = _hedged_decision(hedge_notional=-50.0)
    ok, msg = check_funding_rate_floor(d, s)
    assert ok is False
    assert msg is not None
    assert "TON" in msg
    assert "cannot annualize" in msg or "invalid" in msg


def test_check_hedges_for_non_usd_picks_uses_decimal_on_borderline_size() -> None:
    """`check_hedges_for_non_usd_picks` is now Decimal-based — a pick
    sized EXACTLY at the perp `min_notional_usd` must pass without a
    float-precision off-by-cents reject. Pre-fix the same case could
    flip pass/fail depending on `total_book * float(weight)` rounding
    at large notionals."""
    # total_equity 100 × venue 1.0 × pick 1.0 = 100 USD pick → exactly
    # equal to a 100 USD min_notional → passes (strict `<` comparison).
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", min_notional="100.0")},
    )
    d = _hedged_decision(hedge_notional=-100.0, onchain_venue_weight=1.0)
    assert check_hedges_for_non_usd_picks(d, s) == (True, None)


def test_check_no_double_carry_hedge_passes_when_disjoint_coins() -> None:
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        onchain_products=[_product("26", "OnChain", coin="USDC")],
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.0001")},
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_onchain", 0.4, [("26", 1.0)]),
            _venue("bybit_funding_carry", 0.1, [("TONUSDT", 1.0)]),
        ]
    )
    assert check_no_double_carry_hedge(d, snap) == (True, None)


def test_check_no_double_carry_hedge_fails_when_same_non_stable_coin() -> None:
    """TON in carry venue AND TON in non-stable OnChain pick → would
    open double perp short on TON."""
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        onchain_products=[_product("ton-prod", "OnChain", coin="TON")],
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.0001")},
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.4),
            _venue("bybit_onchain", 0.4, [("ton-prod", 1.0)]),
            _venue("bybit_funding_carry", 0.2, [("TONUSDT", 1.0)]),
        ]
    )
    ok, msg = check_no_double_carry_hedge(d, snap)
    assert ok is False
    assert msg is not None and "TON" in msg


def test_check_no_double_carry_hedge_ignores_stable_earn_overlap() -> None:
    """Carry on TON + stable USDC Earn pick → no conflict (stable Earn
    doesn't trigger an auto-hedge in the first place)."""
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        # Stable coin in flex venue — doesn't hedge.
        flex_products=[_product("usdc-flex", "FlexibleSaving", coin="USDC")],
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.0001")},
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.4),
            _venue("bybit_flex", 0.5, [("usdc-flex", 1.0)]),
            _venue("bybit_funding_carry", 0.1, [("TONUSDT", 1.0)]),
        ]
    )
    assert check_no_double_carry_hedge(d, snap) == (True, None)


def test_check_no_double_carry_hedge_noop_when_no_carry_venue() -> None:
    snap = _carry_snapshot()
    d = _decision()  # default = cash + flex stable
    assert check_no_double_carry_hedge(d, snap) == (True, None)


def test_check_no_double_carry_hedge_fails_when_lm_base_matches_carry() -> None:
    """TON carried in bybit_funding_carry AND a TON/USDT LM pick (base=TON) →
    the delta-hedged LM base leg would double the carry short (validator-1)."""
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.0001")},
    )
    snap.products["LiquidityMining"] = [
        _product("ton-lm", "LiquidityMining", coin="TON/USDT",
                 apr_source="apy_e8", notes=["max_leverage=1"]),
    ]
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.4),
            _venue("bybit_lm", 0.4, [("ton-lm", 1.0)]),
            _venue("bybit_funding_carry", 0.2, [("TONUSDT", 1.0)]),
        ]
    )
    ok, msg = check_no_double_carry_hedge(d, snap)
    assert ok is False
    assert msg is not None and "TON" in msg and "bybit_lm" in msg


def test_check_no_double_carry_hedge_passes_when_lm_base_disjoint() -> None:
    """Carry on TON + an LM pick whose base is ETH (the default ETH/USDC LM
    product) → no overlap, no double short."""
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.0001")},
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.4),
            _venue("bybit_lm", 0.4, [("24", 1.0)]),  # default LM = ETH/USDC
            _venue("bybit_funding_carry", 0.2, [("TONUSDT", 1.0)]),
        ]
    )
    assert check_no_double_carry_hedge(d, snap) == (True, None)


def test_check_stable_spend_cap_counts_carry_picks() -> None:
    """Carry pick alone overruns the liquid stable supply — `.4`
    extension to capital-flow accounting must catch it.

    Setup: $1000 book, $50 USDT supply. Carry pick at 10% of book =
    $100 → spot $100 + perp margin $105 = $205, exceeds $50. Reject.
    """
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.0001")},
        total_equity_usd="1000",
        liquid_usdc_usd="0",
        liquid_usdt_usd="50",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.9),
            _venue("bybit_funding_carry", 0.1, [("TONUSDT", 1.0)]),
        ]
    )
    ok, msg = check_stable_spend_cap(d, snap)
    assert ok is False
    assert msg is not None
    assert "bybit_funding_carry/TONUSDT" in msg
    assert "TON" in msg


def test_check_stable_spend_cap_passes_carry_within_supply() -> None:
    """Carry pick that fits in liquid stable supply passes. $1000 book,
    $300 USDT supply, carry pick at 10% = $100 → spot $100 + perp $105
    = $205 ≤ $300. Pass."""
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        perp_market={"TON": _perp("TON", funding_rate_7d_avg="0.0001")},
        total_equity_usd="1000",
        liquid_usdc_usd="0",
        liquid_usdt_usd="300",
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.9),
            _venue("bybit_funding_carry", 0.1, [("TONUSDT", 1.0)]),
        ]
    )
    assert check_stable_spend_cap(d, snap) == (True, None)


# ─── 4h funding annualization (`bybit-strategy-expansion.2/.4` fix) ─────────


def test_check_funding_rate_floor_passes_4h_coin_at_annualized_floor() -> None:
    """A 4h coin at -0.00005/period (annualized -10.95% = floor exactly)
    passes. The pre-fix code compared per-period -0.00005 to per-period
    floor -0.0001 → would pass too, but for the WRONG reason: it'd let
    -0.00009/4h (-19.7% annualized) slip through as "above floor"
    despite breaching the policy intent."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={
            "TON": _perp(
                "TON",
                funding_rate_7d_avg="-0.00005",
                funding_interval_hours="4",
            )
        },
    )
    d = _hedged_decision(hedge_notional=-50.0)
    assert check_funding_rate_floor(d, s) == (True, None)


def test_check_funding_rate_floor_rejects_4h_coin_below_annualized_floor() -> None:
    """4h-interval funding is annualized correctly in the NET calc. TON gross
    18% + (-0.00009/4h → annualized -19.7%) - 1.8% friction = -3.5% net ≤ 0
    → reject. The SAME -0.00009 at 8h annualizes to only -9.86% → net +6.3%
    → would pass; so the 4h interval is decisive (regression guard for the
    per-period vs annualized bug)."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={
            "TON": _perp(
                "TON",
                funding_rate_7d_avg="-0.00009",
                funding_interval_hours="4",
            )
        },
    )
    d = _hedged_decision(hedge_notional=-50.0)
    ok, msg = check_funding_rate_floor(d, s)
    assert ok is False
    assert msg is not None and "net-of-hedge" in msg


def test_check_funding_carry_floor_accepts_4h_coin_above_annualized_floor() -> None:
    """4h coin at +0.00003/period (annualized 6.57%/year) above carry
    floor 5.475%/year. Pre-fix code rejected this (per-period 0.00003 <
    per-period 0.00005 floor)."""
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        perp_market={
            "TON": _perp(
                "TON",
                funding_rate_7d_avg="0.00003",
                funding_interval_hours="4",
            )
        },
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.9),
            _venue("bybit_funding_carry", 0.1, [("TONUSDT", 1.0)]),
        ]
    )
    assert check_funding_carry_floor(d, snap) == (True, None)


def test_check_funding_carry_floor_rejects_4h_coin_below_annualized_floor() -> None:
    """4h coin at +0.00002/period (annualized 4.38%) below floor."""
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        perp_market={
            "TON": _perp(
                "TON",
                funding_rate_7d_avg="0.00002",
                funding_interval_hours="4",
            )
        },
    )
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.9),
            _venue("bybit_funding_carry", 0.1, [("TONUSDT", 1.0)]),
        ]
    )
    ok, msg = check_funding_carry_floor(d, snap)
    assert ok is False
    assert msg is not None and "below carry floor" in msg


# ─── check_lm_residual_naked_exposure (lm-residual.3) ────────────────────────


def test_lm_residual_rejects_new_coarse_eth_lm() -> None:
    """NEW ETH/USDC LM on a small vault: the $26.5 base leg floor-rounds the
    coarse 0.01-ETH perp lot to leave ~$9.6 naked (>3% of book) → reject."""
    s = _snapshot(
        total_equity_usd="100",
        lm_products=[
            _product("24", "LiquidityMining", coin="ETH/USDC",
                     effective_apr="0.05", apr_source="apy_e8",
                     notes=["max_leverage=1"]),
        ],
        # ETH lot = 0.01 × $1691.58 ≈ $16.92; min_order_qty 0.01 so one lot is
        # tradable → base leg $26.5 floor-rounds to 0.01 ETH, leaving ~$9.58.
        perp_market={
            "ETH": _perp("ETH", mark="1691.58", qty_step="0.01", min_order_qty="0.01")
        },
    )
    d = _decision(venues=[
        _venue("cash_usdc", 0.47),
        _venue("bybit_lm", 0.53, [("24", 1.0)]),  # pick_usd=$53, base leg $26.5
    ])
    ok, msg = check_lm_residual_naked_exposure(d, s)
    assert ok is False
    assert msg is not None and "bybit_lm" in msg and "ETH" in msg and "naked" in msg


def test_lm_residual_exempts_held_position() -> None:
    """A HELD ETH/USDC LM kept at its current size (net-new < min) is exempt —
    the de-risk redeem sweep owns held residual, not a cycle reject."""
    s = _snapshot(
        total_equity_usd="100",
        lm_products=[
            _product("24", "LiquidityMining", coin="ETH/USDC",
                     effective_apr="0.05", apr_source="apy_e8",
                     notes=["max_leverage=1"]),
        ],
        perp_market={"ETH": _perp("ETH", mark="1691.58", qty_step="0.01")},
        lm_positions=[{"productId": "24", "positionId": "p24",
                       "principalLiquidityValue": "53.0"}],
    )
    # Keeps the held $53 position at ~the same weight → net-new ≈ 0.
    d = _decision(venues=[
        _venue("cash_usdc", 0.47),
        _venue("bybit_lm", 0.53, [("24", 1.0)]),
    ])
    assert check_lm_residual_naked_exposure(d, s) == (True, None)


def test_lm_residual_passes_clean_fine_lot() -> None:
    """NEW LM whose base leg is a clean multiple of a FINE perp lot leaves
    ~zero residual → pass."""
    s = _snapshot(
        total_equity_usd="100",
        lm_products=[
            _product("70", "LiquidityMining", coin="FINE/USDC",
                     effective_apr="0.05", apr_source="apy_e8",
                     notes=["max_leverage=1"]),
        ],
        # lot = qty_step*mark = 0.001*100 = $0.10; base leg $25 / $0.10 = 250 lots exact
        perp_market={"FINE": _perp("FINE", mark="100", qty_step="0.001")},
    )
    d = _decision(venues=[
        _venue("cash_usdc", 0.5),
        _venue("bybit_lm", 0.5, [("70", 1.0)]),  # pick_usd=$50, base leg $25
    ])
    assert check_lm_residual_naked_exposure(d, s) == (True, None)
