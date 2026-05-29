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

import pytest

from agent.reason.schema import Decision, Hedge, Pick, VenueAllocation
from agent.sandbox.snapshot import (
    MarketSnapshot,
    PerpInfo,
    ProductSummary,
    Snapshot,
    UsdcPegSnapshot,
    WalletSnapshot,
)
from agent.validate.rules import (
    FUNDING_FLOOR_8H,
    PEG_STRESS_BPS,
    PEG_STRESS_STABLES_FLOOR,
    check_confidence,
    check_disabled_venues,
    check_effective_pick_cap,
    check_funding_rate_floor,
    check_hedges_for_non_usd_picks,
    check_lm_leverage_size_cap,
    check_no_missing_apr_source,
    check_peg_stress,
    check_picks_required,
    check_product_ids_in_snapshot,
    check_risk_flags,
    check_venue_caps,
    check_venue_floors,
    validate,
)


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
) -> ProductSummary:
    return ProductSummary(
        category=category,
        product_id=product_id,
        coin=coin,
        effective_apr=Decimal(effective_apr),
        apr_source=apr_source,
        base_apr_string=None,
        redeem_lockup_minutes=None,
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
        wallet=WalletSnapshot(total_equity_usd=Decimal(total_equity_usd)),
        earn_positions=[],
        lm_positions=[],
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
) -> PerpInfo:
    return PerpInfo(
        symbol=f"{coin.upper()}USDT",
        funding_rate_8h=Decimal("0.0001"),
        funding_rate_7d_avg=(
            Decimal(funding_rate_7d_avg)
            if funding_rate_7d_avg is not None
            else None
        ),
        mark_price=Decimal(mark),
        orderbook_depth_50bps_usd=Decimal("100000"),
        min_order_qty=Decimal("0.1"),
        min_notional_usd=Decimal(min_notional),
        max_leverage=Decimal("50"),
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
    """Default decision is validator-clean: cash 50%, flex 50% USD1@1.0."""
    venues = venues or [
        _venue("cash_usdc", 0.5),
        _venue("bybit_flex", 0.5, [("1131", 1.0)]),
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


def test_check_effective_pick_cap_fails_when_single_pick_oversizes() -> None:
    # bybit_flex=0.60 × pick.weight=1.0 = 0.60 > 0.50 cap.
    d = _decision(
        venues=[
            _venue("cash_usdc", 0.40),
            _venue("bybit_flex", 0.60, [("1131", 1.0)]),
        ]
    )
    ok, msg = check_effective_pick_cap(d)
    assert ok is False
    assert "bybit_flex/1131" in (msg or "")


def test_check_effective_pick_cap_passes_when_split() -> None:
    # bybit_flex=0.60 split 50/50 → effective 0.30 each, under cap.
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
    # Need both picks present in snapshot
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


# ─── Aggregate happy + sad paths ────────────────────────────────────────────


def test_validate_aggregates_multiple_failures_in_one_pass() -> None:
    """The aggregator does NOT short-circuit — when a single decision
    violates several rules, all errors come back in one pass so the
    operator can debug them together."""
    s = _snapshot(deviation_bps=-150.0)
    d = _decision(
        confidence=0.3,
        risk_flags=["flag-a"],
        venues=[
            _venue("cash_usdc", 0.05),  # below 0.10 floor
            _venue("bybit_onchain", 0.60, [("26", 1.0)]),  # above 0.40 cap
            _venue("bybit_flex", 0.35, [("1131", 1.0)]),
        ],
    )
    ok, errors = validate(d, s)
    assert ok is False
    # At least four distinct violations expected.
    assert len(errors) >= 4
    joined = " | ".join(errors)
    assert "confidence" in joined
    assert "risk_flags" in joined
    assert "cash_usdc" in joined  # floor
    assert "bybit_onchain" in joined  # cap


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
    """Exactly at the floor passes (strict-less-than comparison)."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={
            "TON": _perp("TON", funding_rate_7d_avg=str(FUNDING_FLOOR_8H))
        },
    )
    d = _hedged_decision(hedge_notional=-50.0)
    assert check_funding_rate_floor(d, s) == (True, None)


def test_aggregate_validate_passes_hedged_non_usd_pick() -> None:
    """End-to-end: validator accepts a TON OnChain pick when the hedge
    is sized correctly and the perp market entry clears min-notional.
    `bybit_onchain.max_weight=0.40` (registry), so the pick goes at 40%
    of book → $40 hedge target ±20% tolerance."""
    s = _snapshot(
        onchain_products=_onchain_ton(),
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
    )
    d = _hedged_decision(
        hedge_notional=-40.0,
        onchain_venue_weight=0.40,
    )
    ok, errors = validate(d, s)
    assert ok, errors
