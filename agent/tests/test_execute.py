"""Sandbox executor tests (`.11`).

Covers `diff_to_actions` planning logic + `execute_actions` dispatch
(dry-run path + live-with-mock-client path). Real Bybit calls are
mocked via `AsyncMock` — the unit test trusts `BybitClient.place_earn_order`
to do its job and only checks the executor's contract with it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent.bybit_oracle.bybit_client import (
    BybitAPIError,
    EarnOrderResult,
    PerpPosition,
    SpotOrderResult,
)
from agent.reason.schema import Decision, Hedge, Pick, VenueAllocation
from agent.sandbox.execute import (
    MIN_ACTION_USDC,
    Action,
    ActionKind,
    diff_to_actions,
    execute_actions,
    _enforce_usdt_budget,
    _order_link_id,
)
from agent.sandbox.snapshot import (
    MarketSnapshot,
    PerpInfo,
    ProductSummary,
    Snapshot,
    UsdcPegSnapshot,
    WalletSnapshot,
)


# ─── Fixture factories ──────────────────────────────────────────────────────


def _snapshot(
    *,
    total_equity_usd: str = "100",
    usdt_available_usd: str = "0",
    earn_positions: list[dict] | None = None,
    perp_market: dict[str, PerpInfo] | None = None,
    perp_positions: list[PerpPosition] | None = None,
    advance_earn_quotes: dict[str, dict] | None = None,
    lm_products: list[ProductSummary] | None = None,
    lm_positions: list[dict] | None = None,
    advance_products: dict[str, list[ProductSummary]] | None = None,
    onchain_products: list[ProductSummary] | None = None,
    flex_products: list[ProductSummary] | None = None,
    alpha_products: list[ProductSummary] | None = None,
    alpha_positions: list[dict] | None = None,
) -> Snapshot:
    products: dict[str, list[ProductSummary]] = {
        "FlexibleSaving": flex_products or [],
        "OnChain": onchain_products or [],
        "LiquidityMining": lm_products or [],
    }
    if advance_products:
        for cat, items in advance_products.items():
            products[cat] = items
    if alpha_products:
        products["AlphaFarm"] = alpha_products
    return Snapshot(
        captured_at=datetime.now(UTC),
        wallet=WalletSnapshot(
            total_equity_usd=Decimal(total_equity_usd),
            usdt_available_usd=Decimal(usdt_available_usd),
        ),
        earn_positions=earn_positions or [],
        lm_positions=lm_positions or [],
        alpha_positions=alpha_positions or [],
        products=products,
        market=MarketSnapshot(),
        perp_market=perp_market or {},
        perp_positions=perp_positions or [],
        advance_earn_quotes=advance_earn_quotes or {},
        usdc_peg=UsdcPegSnapshot(
            price_usd=Decimal("1.0"),
            deviation_bps=Decimal("0"),
            fetched_at=datetime.now(UTC),
        ),
        errors=[],
    )


def _advance_product(
    category: str, product_id: str, coin: str
) -> ProductSummary:
    """Minimal ProductSummary for an advance-Earn product. `coin` carries
    the stake currency — for DualAssets that's `"BASE/QUOTE"`, for
    DiscountBuy / SmartLeverage / DoubleWin it's the single stake coin
    (typically USDT). The diff layer looks the pair up here when the
    quote endpoint doesn't echo it."""
    return ProductSummary(
        category=category,
        product_id=product_id,
        coin=coin,
        effective_apr=Decimal("0"),
        apr_source="missing",
        notes=[],
    )


def _lm_product(
    product_id: str,
    *,
    base: str = "ETH",
    quote: str = "USDC",
    apr: str = "0.02",
    max_leverage: int = 1,
    min_subscribe_usd: str | None = None,
) -> ProductSummary:
    """Build an LM ProductSummary the way snapshot.py would (`_lm_summary`).
    Defaults to ETH/USDC at 2% APR, leverage=1 — the canonical pickable
    unleveraged pair surfaced in real snapshots via `lm_unleveraged`.
    Set `min_subscribe_usd` to exercise the diff's min-floor check."""
    return ProductSummary(
        category="LiquidityMining",
        product_id=product_id,
        coin=f"{base}/{quote}",
        effective_apr=Decimal(apr),
        apr_source="apy_e8",
        min_subscribe_usd=(
            Decimal(min_subscribe_usd) if min_subscribe_usd is not None else None
        ),
        notes=[f"max_leverage={max_leverage}"],
    )


def _lm_position(
    *,
    product_id: str,
    position_id: str,
    principal_usd: str = "20",
) -> dict:
    """Build an LM position row the way Bybit echoes it. Tests use
    `principalLiquidityValue` as the headline number — the executor
    prefers it over reconstructing from quote/base/price."""
    return {
        "positionId": position_id,
        "productId": product_id,
        "principalLiquidityValue": principal_usd,
        "status": "Active",
    }


def _dual_quote(
    *,
    base: str = "BTC",
    quote_coin: str = "USDT",
    expired_in_ms: int = 600_000,  # 10 min ahead, per offer
    offers: list[dict] | None = None,
) -> dict:
    """Bybit DualAssets quote payload — matches the live shape verified
    2026-05-28. `expiredAt` lives on each offer (not parent); base/quote
    coins are NOT echoed in the quote (they're in the product list)."""
    from datetime import timedelta

    expired = int(
        (datetime.now(UTC) + timedelta(milliseconds=expired_in_ms)).timestamp() * 1000
    )
    return {
        "category": "DualAssets",
        "list": [
            {
                "productId": "da-1",
                "currentPrice": "65000",
                "buyLowPrice": offers
                or [
                    {"selectPrice": "60000", "apyE8": "50000000", "expiredAt": str(expired)},
                    {"selectPrice": "62000", "apyE8": "80000000", "expiredAt": str(expired)},
                ],
                "sellHighPrice": [],
            }
        ],
    }


def _discount_quote(
    *,
    coin: str = "USDT",
    inst_uid: str | None = "instUid-123",
    expired_in_ms: int = 600_000,
    purchase_price: str = "63000",
    knockout_price: str = "55000",
) -> dict:
    """Bybit DiscountBuy quote payload — top-level key is `offers`
    (verified live 2026-05-28), NOT `list` like DualAssets uses. Offer
    rows don't carry `coin` in production payloads; the diff looks the
    stake currency up from the snapshot's product list. We still include
    `coin` here so existing tests that exercise the offer-side fallback
    keep working."""
    from datetime import timedelta

    expired = int(
        (datetime.now(UTC) + timedelta(milliseconds=expired_in_ms)).timestamp() * 1000
    )
    return {
        "offers": [
            {
                "coin": coin,
                "instUid": inst_uid,
                "currentPrice": "65000",
                "purchasePrice": purchase_price,
                "knockoutPrice": knockout_price,
                "knockoutCouponE8": "100000000",
                "expiredAt": str(expired),
                "category": "DiscountBuy",
            }
        ],
    }


def _short_pos(
    coin: str,
    *,
    size: str,
    position_value: str | None = None,
    mark_price: str | None = None,
) -> PerpPosition:
    """Short PerpPosition fixture. `position_value` is what Bybit's
    `/v5/position/list` echoes for an open USDT-settled position; tests
    that want to exercise the mark-price fallback path can omit it."""
    return PerpPosition(
        symbol=f"{coin.upper()}USDT",
        side="Sell",
        size=size,
        positionValue=position_value,
        markPrice=mark_price,
    )


def _perp(coin: str, *, mark: str = "2.0", min_notional: str = "0.5") -> PerpInfo:
    return PerpInfo(
        symbol=f"{coin.upper()}USDT",
        funding_rate_8h=Decimal("0.0001"),
        mark_price=Decimal(mark),
        orderbook_depth_50bps_usd=Decimal("100000"),
        min_order_qty=Decimal("0.1"),
        min_notional_usd=Decimal(min_notional),
        max_leverage=Decimal("50"),
    )


def _pos(category: str, product_id: str, amount: str, coin: str = "USDC") -> dict:
    """A raw EarnPosition dict — mirrors `EarnPosition.model_dump()`."""
    return {
        "category": category,
        "productId": product_id,
        "coin": coin,
        "amount": amount,
    }


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


def _decision(venues: list[VenueAllocation]) -> Decision:
    return Decision(
        thesis="placeholder thesis describing the planned move.",
        venues=venues,
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=3.5,
    )


# ─── Diff: fresh subscribes ─────────────────────────────────────────────────


def test_diff_fresh_subscribe_into_flex() -> None:
    snap = _snapshot(total_equity_usd="100")
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    assert len(subs) == 1
    assert subs[0].category == "FlexibleSaving"
    assert subs[0].product_id == "1131"
    assert subs[0].amount == Decimal("50.0")


def test_diff_split_picks_yield_two_subscribes() -> None:
    snap = _snapshot(total_equity_usd="200")
    d = _decision(
        [
            _venue("cash_usdc", 0.4),
            _venue("bybit_flex", 0.6, [("1131", 0.7), ("1", 0.3)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    # 0.6 * 200 = 120, split 70/30 → 84 + 36
    by_pid = {a.product_id: a.amount for a in subs}
    assert by_pid["1131"] == Decimal("84.0")
    assert by_pid["1"] == Decimal("36.0")


# ─── Diff: redeems ──────────────────────────────────────────────────────────


def test_diff_redeems_when_currently_held_position_drops_out() -> None:
    snap = _snapshot(
        total_equity_usd="100",
        earn_positions=[_pos("FlexibleSaving", "9999", "30")],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    kinds = [a.kind for a in actions]
    # Redeems must precede subscribes.
    assert kinds[0] == ActionKind.REDEEM_EARN
    assert kinds[1] == ActionKind.SUBSCRIBE_EARN
    redeem = [a for a in actions if a.kind == ActionKind.REDEEM_EARN][0]
    assert redeem.product_id == "9999"
    assert redeem.amount == Decimal("30")


def test_diff_partial_redeem_when_target_below_current() -> None:
    snap = _snapshot(
        total_equity_usd="100",
        earn_positions=[_pos("FlexibleSaving", "1131", "60")],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.8),
            _venue("bybit_flex", 0.2, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert len(actions) == 1
    assert actions[0].kind == ActionKind.REDEEM_EARN
    assert actions[0].amount == Decimal("40")  # 60 current - 20 target


# ─── Diff: non-stable current sizing (.34) ──────────────────────────────────


def test_diff_non_stable_current_priced_via_mark_no_op_when_matching() -> None:
    """25 TON @ mark $2.0 = $50 current. Target also $50 → no action."""
    snap = _snapshot(
        total_equity_usd="100",
        earn_positions=[_pos("OnChain", "8", "25", coin="TON")],
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_onchain", 0.5, [("8", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    # No Earn action — current $50 matches target $50 within threshold.
    earn_kinds = {ActionKind.SUBSCRIBE_EARN, ActionKind.REDEEM_EARN}
    assert not [a for a in actions if a.kind in earn_kinds]


def test_diff_non_stable_current_under_target_emits_subscribe_delta() -> None:
    """25 TON @ $2.0 = $50 current; target $80 → subscribe $30 worth."""
    snap = _snapshot(
        total_equity_usd="160",
        earn_positions=[_pos("OnChain", "8", "25", coin="TON")],
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_onchain", 0.5, [("8", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    assert len(subs) == 1
    # target = 160 * 0.5 * 1.0 = 80; delta = 80 - 50 = 30.
    assert subs[0].product_id == "8"
    assert subs[0].amount == Decimal("30.0")


def test_diff_non_stable_current_over_target_emits_redeem_delta() -> None:
    """25 TON @ $2.0 = $50 current; target $20 → redeem $30 worth."""
    snap = _snapshot(
        total_equity_usd="40",
        earn_positions=[_pos("OnChain", "8", "25", coin="TON")],
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_onchain", 0.5, [("8", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    redeems = [a for a in actions if a.kind == ActionKind.REDEEM_EARN]
    assert len(redeems) == 1
    # target = 40 * 0.5 * 1.0 = 20; current $50 - $20 = $30 to redeem.
    assert redeems[0].amount == Decimal("30.0")


def test_diff_non_stable_current_without_perp_market_treated_as_zero() -> None:
    """25 TON but no perp_market entry → current=$0 → full target subscribe.
    Safer than mis-sizing by treating coin units as dollars."""
    snap = _snapshot(
        total_equity_usd="100",
        earn_positions=[_pos("OnChain", "8", "25", coin="TON")],
        perp_market={},  # mark price disappeared this cycle
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_onchain", 0.5, [("8", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    # current treated as $0 → full target $50 subscribe.
    assert len(subs) == 1
    assert subs[0].amount == Decimal("50.0")


def test_diff_stable_current_still_treated_as_1to1_regression() -> None:
    """Stable USDC position must NOT route through perp_market — that
    table doesn't carry USDC, and treating amount as USD is correct."""
    snap = _snapshot(
        total_equity_usd="100",
        earn_positions=[_pos("FlexibleSaving", "1131", "30")],
        perp_market={},
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    # current $30 stable, target $50 → subscribe $20.
    assert len(subs) == 1
    assert subs[0].amount == Decimal("20.0")


# ─── Diff: no action paths ──────────────────────────────────────────────────


def test_diff_skips_when_delta_below_threshold() -> None:
    snap = _snapshot(
        total_equity_usd="100",
        earn_positions=[_pos("FlexibleSaving", "1131", "50.10")],  # close to 50
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    # delta is 0.10, below MIN_ACTION_USDC = 0.50 → no action.
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert actions == []
    assert MIN_ACTION_USDC == Decimal("0.50")


def test_diff_empty_when_book_zero() -> None:
    snap = _snapshot(total_equity_usd="0")
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    assert diff_to_actions(snap, d, snapshot_ts="20260527T120000Z") == []


def test_diff_cash_only_produces_no_actions() -> None:
    snap = _snapshot(total_equity_usd="100")
    d = _decision([_venue("cash_usdc", 1.0)])
    assert diff_to_actions(snap, d, snapshot_ts="20260527T120000Z") == []


# ─── Out-of-scope skip (LM + advance-Earn) ──────────────────────────────────


def test_diff_lm_pick_emits_subscribe_lm() -> None:
    """`.47`: an LM pick on a max_leverage=1 ETH/USDC pair turns into a
    single-sided USDC subscribe at the venue × pick USD amount. No SKIP."""
    snap = _snapshot(
        total_equity_usd="100",
        lm_products=[_lm_product("24")],  # ETH/USDC max_leverage=1
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("24", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_LM]
    assert len(subs) == 1
    assert subs[0].category == "LiquidityMining"
    assert subs[0].product_id == "24"
    assert subs[0].coin == "USDC"
    assert subs[0].amount == Decimal("30.0")  # 100 × 0.3 × 1.0
    assert subs[0].position_id is None


def test_diff_lm_subscribe_below_min_emits_skip() -> None:
    """`.47` follow-up: Bybit enforces `minInvestmentQuote` per LM
    product (50 USDC for ETH/USDC, BTC/USDC). A target below that floor
    must SKIP, not produce an order Bybit will reject at execute time."""
    snap = _snapshot(
        total_equity_usd="100",
        lm_products=[_lm_product("24", min_subscribe_usd="50")],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.9),
            _venue("bybit_lm", 0.1, [("24", 1.0)]),  # target = $10, below $50 min
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_LM]
    skips = [a for a in actions if a.kind == ActionKind.SKIP_OUT_OF_SCOPE]
    assert subs == []
    assert len(skips) == 1
    assert "below Bybit min" in skips[0].reason
    assert "$50" in skips[0].reason


def test_diff_lm_subscribe_above_min_emits_subscribe() -> None:
    """Sanity check the other side of the floor — a target ≥ min must
    still emit SUBSCRIBE_LM as the no-min case does."""
    snap = _snapshot(
        total_equity_usd="200",
        lm_products=[_lm_product("24", min_subscribe_usd="50")],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.70),
            _venue("bybit_lm", 0.30, [("24", 1.0)]),  # target = $60, above $50
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_LM]
    assert len(subs) == 1
    assert subs[0].amount == Decimal("60.0")


def test_diff_lm_subscribe_no_min_surfaced_subscribes_anyway() -> None:
    """When the snapshot lacks `min_subscribe_usd` (older snapshots
    before .47 follow-up), the floor check must be a no-op — we don't
    block valid subscribes just because we don't know the min."""
    snap = _snapshot(
        total_equity_usd="100",
        lm_products=[_lm_product("24")],  # min_subscribe_usd=None
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("24", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_LM]
    assert len(subs) == 1


def test_diff_lm_pick_missing_from_snapshot_emits_skip() -> None:
    """LLM hallucinated a product_id not in snapshot.products → SKIP,
    not a runtime KeyError. Mirrors the advance-Earn missing-quote case."""
    snap = _snapshot(total_equity_usd="100", lm_products=[])
    d = _decision(
        [
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("99", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    skips = [a for a in actions if a.kind == ActionKind.SKIP_OUT_OF_SCOPE]
    assert len(skips) == 1
    assert "not in snapshot" in skips[0].reason


def test_diff_lm_pick_usdt_quote_emits_subscribe_with_swap_leg() -> None:
    """Operator hard rule (2026-05-27 + 2026-05-29): never restrict LM
    picks to USDC-quote. A USDT-quote pair must produce SUBSCRIBE_LM +
    a USDC→USDT swap leg sized to cover it. Mirrors the auto-swap
    pattern Earn already uses for non-USDC stable picks."""
    snap = _snapshot(
        total_equity_usd="100",
        lm_products=[_lm_product("16", base="NEAR", quote="USDT", max_leverage=1)],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("16", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_LM]
    swaps = [a for a in actions if a.kind == ActionKind.SWAP_SPOT]
    assert len(subs) == 1
    assert subs[0].coin == "USDT"
    assert subs[0].amount == Decimal("30.0")  # 100 * 0.3 * 1.0
    assert len(swaps) == 1
    assert swaps[0].product_id == "USDCUSDT"
    assert swaps[0].coin == "USDT"
    # Swap must come before the LM subscribe in the action sequence.
    kinds = [a.kind for a in actions]
    assert kinds.index(ActionKind.SWAP_SPOT) < kinds.index(ActionKind.SUBSCRIBE_LM)


def test_diff_lm_pick_non_stable_quote_emits_skip() -> None:
    """Hypothetical edge case — non-stable quote coin (e.g. BTC/ETH LP)
    can't be sized in USD without a quote-side mark. SKIP with explicit
    reason rather than silently mis-size. Real Bybit LM is always
    stable-quoted; this guards against future product shape changes."""
    snap = _snapshot(
        total_equity_usd="100",
        lm_products=[_lm_product("99", base="BTC", quote="ETH", max_leverage=1)],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("99", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    skips = [a for a in actions if a.kind == ActionKind.SKIP_OUT_OF_SCOPE]
    assert len(skips) == 1
    assert "not a recognized stable" in skips[0].reason


def test_diff_lm_full_exit_when_target_zero_and_position_exists() -> None:
    """Position open + LLM drops LM from the plan → REDEEM_LM full exit
    with `position_id` populated for the executor's remove-liquidity call."""
    snap = _snapshot(
        total_equity_usd="100",
        lm_products=[_lm_product("24")],
        lm_positions=[
            _lm_position(product_id="24", position_id="9001", principal_usd="20")
        ],
    )
    # cash_usdc=1.0 — LM dropped entirely.
    d = _decision([_venue("cash_usdc", 1.0)])
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    redeems = [a for a in actions if a.kind == ActionKind.REDEEM_LM]
    assert len(redeems) == 1
    assert redeems[0].position_id == "9001"
    assert redeems[0].product_id == "24"
    assert redeems[0].amount == Decimal("20")


def test_diff_lm_existing_position_at_target_emits_nothing() -> None:
    """Position size matches the LLM's target within MIN_ACTION_USDC →
    no-op. Avoids churn from re-subscribing the same position."""
    snap = _snapshot(
        total_equity_usd="100",
        lm_products=[_lm_product("24")],
        lm_positions=[
            _lm_position(product_id="24", position_id="9001", principal_usd="30")
        ],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("24", 1.0)]),  # target = $30
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    lm_actions = [
        a
        for a in actions
        if a.kind in (ActionKind.SUBSCRIBE_LM, ActionKind.REDEEM_LM)
    ]
    assert lm_actions == []


def test_diff_lm_partial_decrease_emits_redeem_with_remove_rate() -> None:
    """Partial drawdown (target > 0 but smaller than current) now wires
    REDEEM_LM with `removeRate=N%` instead of SKIP. Operator change
    2026-05-29 to support leveraged LM de-risk path."""
    snap = _snapshot(
        total_equity_usd="100",
        lm_products=[_lm_product("24")],
        lm_positions=[
            _lm_position(product_id="24", position_id="9001", principal_usd="30")
        ],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.9),
            _venue("bybit_lm", 0.1, [("24", 1.0)]),  # target = $10, current $30
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    redeems = [a for a in actions if a.kind == ActionKind.REDEEM_LM]
    assert len(redeems) == 1
    r = redeems[0]
    assert r.position_id == "9001"
    # (30 - 10) / 30 = 66.67% → rounds to 67
    assert r.extra["remove_rate"] == 67
    assert r.amount == Decimal("20")  # USD amount being redeemed


def test_diff_lm_partial_increase_emits_skip() -> None:
    """Partial INCREASE (target > current) stays as SKIP — Bybit's
    add-liquidity opens a second position rather than topping up the
    existing one, which would complicate redeem tracking. Operator can
    full-exit + resubscribe next cycle."""
    snap = _snapshot(
        total_equity_usd="100",
        lm_products=[_lm_product("24")],
        lm_positions=[
            _lm_position(product_id="24", position_id="9001", principal_usd="10")
        ],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("24", 1.0)]),  # target = $30, current $10
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    skips = [a for a in actions if a.kind == ActionKind.SKIP_OUT_OF_SCOPE]
    assert len(skips) == 1
    assert "partial increase not wired" in skips[0].reason


def test_diff_advance_pick_emits_skip() -> None:
    snap = _snapshot(total_equity_usd="100")
    d = _decision(
        [
            _venue("cash_usdc", 0.8),
            _venue("bybit_dual_asset", 0.2, [("134878", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert any(a.kind == ActionKind.SKIP_OUT_OF_SCOPE for a in actions)


# ─── Alpha Farm diff + dispatch (`.54`) ────────────────────────────────────


def _alpha_summary_row(token_code: str = "DEX_123", symbol: str = "PEPE") -> ProductSummary:
    return ProductSummary(
        category="AlphaFarm",
        product_id=token_code,
        coin=symbol,
        effective_apr=Decimal("0.10"),
        apr_source="momentum",
    )


def _alpha_position(
    token_code: str = "DEX_123", symbol: str = "PEPE",
    amount_usd: str = "10", amount_native: str = "1000000",
) -> dict:
    return {
        "tokenCode": token_code,
        "tokenSymbol": symbol,
        "tokenAmount": amount_native,
        "tokenAmountUsd": amount_usd,
        "chainCode": "ETH",
    }


def test_diff_alpha_pick_emits_skip_when_gate_off(monkeypatch) -> None:
    """Default state: VAULT_ALPHA_EXEC_ENABLED unset → gate is False →
    Alpha picks should NOT fire live API calls during the `.14` smoke."""
    monkeypatch.setattr("agent.sandbox.execute.ALPHA_EXEC_ENABLED", False)
    snap = _snapshot(
        total_equity_usd="100",
        alpha_products=[_alpha_summary_row()],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.95),
            _venue("bybit_alpha", 0.05, [("DEX_123", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    skips = [a for a in actions if a.kind == ActionKind.SKIP_OUT_OF_SCOPE]
    assert any(
        a.category == "AlphaFarm" and "VAULT_ALPHA_EXEC_ENABLED" in a.reason
        for a in skips
    ), f"expected gate SKIP, got: {[a.reason for a in skips]}"
    assert not any(
        a.kind in (ActionKind.ALPHA_PURCHASE, ActionKind.ALPHA_REDEEM)
        for a in actions
    ), "no alpha live actions should be emitted when gate is off"


def test_diff_alpha_purchase_when_gate_on(monkeypatch) -> None:
    monkeypatch.setattr("agent.sandbox.execute.ALPHA_EXEC_ENABLED", True)
    snap = _snapshot(
        total_equity_usd="100",
        alpha_products=[_alpha_summary_row()],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.95),
            _venue("bybit_alpha", 0.05, [("DEX_123", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    purchases = [a for a in actions if a.kind == ActionKind.ALPHA_PURCHASE]
    assert len(purchases) == 1
    a = purchases[0]
    assert a.category == "AlphaFarm"
    assert a.product_id == "DEX_123"
    assert a.coin == "PEPE"
    assert a.amount == Decimal("5.00")
    assert "alpha_purchase DEX_123" in a.reason
    assert "CEX_1" in a.reason  # pay token surfaced for log readability


def test_diff_alpha_full_exit_when_position_dropped(monkeypatch) -> None:
    """Holding PEPE, LLM no longer picks Alpha — full exit via REDEEM
    with native token amount carried in extra."""
    monkeypatch.setattr("agent.sandbox.execute.ALPHA_EXEC_ENABLED", True)
    snap = _snapshot(
        total_equity_usd="100",
        alpha_positions=[_alpha_position(amount_usd="10", amount_native="1000000")],
    )
    d = _decision([_venue("cash_usdc", 1.0)])  # no Alpha pick this cycle
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    redeems = [a for a in actions if a.kind == ActionKind.ALPHA_REDEEM]
    assert len(redeems) == 1
    r = redeems[0]
    assert r.product_id == "DEX_123"
    assert r.coin == "PEPE"
    assert r.amount == Decimal("10")
    assert r.extra.get("token_amount_native") == "1000000"


def test_diff_alpha_partial_reduction_emits_skip(monkeypatch) -> None:
    """MVP: only full exits — partial scale-down SKIPs with reason."""
    monkeypatch.setattr("agent.sandbox.execute.ALPHA_EXEC_ENABLED", True)
    snap = _snapshot(
        total_equity_usd="100",
        alpha_products=[_alpha_summary_row()],
        alpha_positions=[_alpha_position(amount_usd="10")],
    )
    # Currently $10, target $3 (drop of $7 > MIN_ACTION_USDC)
    d = _decision(
        [
            _venue("cash_usdc", 0.97),
            _venue("bybit_alpha", 0.03, [("DEX_123", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    skips = [
        a for a in actions
        if a.kind == ActionKind.SKIP_OUT_OF_SCOPE and a.category == "AlphaFarm"
    ]
    assert any("partial reduction not wired" in a.reason for a in skips)


def test_diff_alpha_no_op_when_target_matches_current(monkeypatch) -> None:
    """Current ≈ target → no action (within MIN_ACTION_USDC)."""
    monkeypatch.setattr("agent.sandbox.execute.ALPHA_EXEC_ENABLED", True)
    snap = _snapshot(
        total_equity_usd="100",
        alpha_products=[_alpha_summary_row()],
        alpha_positions=[_alpha_position(amount_usd="5")],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.95),
            _venue("bybit_alpha", 0.05, [("DEX_123", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert not any(
        a.category == "AlphaFarm" for a in actions
    ), f"expected no alpha action when target≈current, got: {actions}"


def test_diff_alpha_redeem_skips_when_no_native_amount(monkeypatch) -> None:
    """Position present but tokenAmount missing (degraded fetch) →
    SKIP rather than redeem with garbage."""
    monkeypatch.setattr("agent.sandbox.execute.ALPHA_EXEC_ENABLED", True)
    snap = _snapshot(
        total_equity_usd="100",
        alpha_positions=[
            {"tokenCode": "DEX_999", "tokenSymbol": "X", "tokenAmountUsd": "5"}
        ],
    )
    d = _decision([_venue("cash_usdc", 1.0)])
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    skips = [
        a for a in actions
        if a.kind == ActionKind.SKIP_OUT_OF_SCOPE and a.category == "AlphaFarm"
    ]
    assert any("no tokenAmount" in a.reason for a in skips)


# ─── Idempotency keys ───────────────────────────────────────────────────────


def test_order_link_id_is_deterministic() -> None:
    assert _order_link_id("20260527T120000Z", 0) == "sandbox-20260527T120000Z-000"
    assert _order_link_id("20260527T120000Z", 42) == "sandbox-20260527T120000Z-042"


def test_idempotency_keys_unique_within_plan() -> None:
    snap = _snapshot(
        total_equity_usd="200",
        earn_positions=[_pos("FlexibleSaving", "9999", "20")],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.3),
            _venue("bybit_flex", 0.5, [("1131", 0.6), ("1", 0.4)]),
            _venue("bybit_onchain", 0.2, [("26", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    keys = [a.order_link_id for a in actions]
    assert len(keys) == len(set(keys))


# ─── Execute: dry-run vs live ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_dry_run_writes_log_without_calling_client(tmp_path: Path) -> None:
    client = AsyncMock()
    action = Action(
        kind=ActionKind.SUBSCRIBE_EARN,
        category="FlexibleSaving",
        product_id="1131",
        coin="USD1",
        amount=Decimal("50"),
        order_link_id="sandbox-test-000",
        reason="test",
    )
    results = await execute_actions(
        client,
        [action],
        snapshot_ts="20260527T120000Z",
        dry_run=True,
        executions_dir=tmp_path,
    )
    assert results[0].status == "dry-run"
    assert results[0].response["would_call"] == "place_earn_order"
    client.place_earn_order.assert_not_called()
    log_path = tmp_path / "20260527T120000Z.jsonl"
    assert log_path.is_file()
    line = log_path.read_text().strip()
    assert json.loads(line)["status"] == "dry-run"


@pytest.mark.asyncio
async def test_execute_live_calls_place_earn_order(tmp_path: Path) -> None:
    client = AsyncMock()
    client.place_earn_order.return_value = EarnOrderResult(orderId="abc-123")
    action = Action(
        kind=ActionKind.SUBSCRIBE_EARN,
        category="FlexibleSaving",
        product_id="1131",
        coin="USD1",
        amount=Decimal("50"),
        order_link_id="sandbox-test-001",
        reason="test",
    )
    results = await execute_actions(
        client,
        [action],
        snapshot_ts="20260527T120000Z",
        dry_run=False,
        executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    assert results[0].response == {"orderId": "abc-123"}
    client.place_earn_order.assert_awaited_once()
    kwargs = client.place_earn_order.await_args.kwargs
    assert kwargs["category"] == "FlexibleSaving"
    assert kwargs["product_id"] == "1131"
    assert kwargs["side"] == "Stake"
    assert kwargs["coin"] == "USD1"
    assert kwargs["account_type"] == "UNIFIED"
    assert kwargs["order_link_id"] == "sandbox-test-001"


@pytest.mark.asyncio
async def test_execute_live_redeem_uses_redeem_side(tmp_path: Path) -> None:
    client = AsyncMock()
    client.place_earn_order.return_value = EarnOrderResult(orderId="r-1")
    action = Action(
        kind=ActionKind.REDEEM_EARN,
        category="OnChain",
        product_id="26",
        coin="USDC",
        amount=Decimal("10"),
        order_link_id="sandbox-test-002",
        reason="redeem",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260527T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    kwargs = client.place_earn_order.await_args.kwargs
    assert kwargs["side"] == "Redeem"
    # OnChain Earn requires FUND account type per Bybit V5 spec.
    assert kwargs["account_type"] == "FUND"


@pytest.mark.asyncio
async def test_execute_live_catches_bybit_api_error(tmp_path: Path) -> None:
    client = AsyncMock()
    client.place_earn_order.side_effect = BybitAPIError(
        180005, "product is suspended", "/v5/earn/place-order"
    )
    action = Action(
        kind=ActionKind.SUBSCRIBE_EARN,
        category="FlexibleSaving",
        product_id="1131",
        coin="USD1",
        amount=Decimal("50"),
        order_link_id="sandbox-test-003",
        reason="test",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260527T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "error"
    assert "180005" in (results[0].error or "")
    # One error doesn't abort the loop — but with one action, just verify
    # the result is captured cleanly.


@pytest.mark.asyncio
async def test_execute_skip_does_not_call_client(tmp_path: Path) -> None:
    client = AsyncMock()
    action = Action(
        kind=ActionKind.SKIP_OUT_OF_SCOPE,
        category="LiquidityMining",
        product_id="24",
        coin="ETH/USDC",
        amount=Decimal("30"),
        order_link_id="sandbox-test-004",
        reason="LM not wired",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260527T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "skipped"
    client.place_earn_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_appends_one_line_per_action(tmp_path: Path) -> None:
    client = AsyncMock()
    client.place_earn_order.return_value = EarnOrderResult(orderId="x")
    actions = [
        Action(
            kind=ActionKind.SUBSCRIBE_EARN,
            category="FlexibleSaving",
            product_id="1131",
            coin="USD1",
            amount=Decimal("10"),
            order_link_id="sandbox-test-000",
            reason="r1",
        ),
        Action(
            kind=ActionKind.SUBSCRIBE_EARN,
            category="OnChain",
            product_id="26",
            coin="USDC",
            amount=Decimal("5"),
            order_link_id="sandbox-test-001",
            reason="r2",
        ),
    ]
    await execute_actions(
        client, actions, snapshot_ts="20260527T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    log = (tmp_path / "20260527T120000Z.jsonl").read_text().splitlines()
    assert len(log) == 2
    for line in log:
        assert json.loads(line)["status"] == "ok"


# ─── Hedge actions (.31) ────────────────────────────────────────────────────


# OnChain TON product fixture for hedge-flow tests. Auto-hedge derives
# the coin from `snapshot.products["OnChain"]`, so any hedge test that
# wants TON to be picked must surface product 8 with coin=TON here.
_TON_PRODUCT = ProductSummary(
    category="OnChain",
    product_id="8",
    coin="TON",
    effective_apr=Decimal("0.18"),
    apr_source="estimate_apr",
)

# FlexibleSaving non-stable product for the FlexibleSaving auto-hedge
# path (`.47` follow-up 2026-05-29). Non-stable Flex picks like ID, IO,
# AGIX get the same auto-hedge as OnChain non-stables.
_ID_FLEX_PRODUCT = ProductSummary(
    category="FlexibleSaving",
    product_id="315",
    coin="ID",
    effective_apr=Decimal("0.12"),
    apr_source="estimate_apr",
)


def test_diff_flex_non_stable_pick_emits_auto_hedge() -> None:
    """`.47` follow-up: a non-stable FlexibleSaving pick (e.g. ID, IO)
    must produce an OPEN_PERP_SHORT same as OnChain non-stable picks.
    Auto-hedge derives notional from pick USD value; no Hedge entry
    required on the Decision."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"ID": _perp("ID", mark="0.5")},
        flex_products=[_ID_FLEX_PRODUCT],
    )
    d = Decision(
        thesis="Flex ID non-stable pick — auto-hedge derived from pick USD",
        venues=[
            _venue("cash_usdc", 0.6),
            _venue("bybit_flex", 0.4, [("315", 1.0)]),
        ],
        hedges=[],  # auto-derived, intentionally empty
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=4.8,
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260529T120000Z")
    opens = [a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]
    assert len(opens) == 1
    h = opens[0]
    assert h.product_id == "IDUSDT"
    assert h.coin == "ID"
    # pick_usd = 100 * 0.4 * 1.0 = $40; qty = 40 / 0.5 = 80 ID
    assert h.amount == Decimal("80.000")


def _decision_with_hedge(hedge_notional: float = -50.0) -> Decision:
    """Decision with TON OnChain pick + (informational) TON short hedge.
    `hedge_notional` is informational only after 2026-05-29 — auto-hedge
    derives size from the pick — but we keep the parameter to document
    test intent in the venue rebalance tests."""
    return Decision(
        thesis="TON OnChain at 18% APR with paired short hedge for delta-neutral.",
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_onchain", 0.5, [("8", 1.0)]),
        ],
        hedges=[Hedge(coin="TON", notional_usd=hedge_notional)],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=15.0,
    )


def test_diff_hedge_emits_open_perp_short() -> None:
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    hedges = [a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]
    assert len(hedges) == 1
    h = hedges[0]
    assert h.product_id == "TONUSDT"
    assert h.coin == "TON"
    # $50 notional at mark $2.0 → 25 TON.
    assert h.amount == Decimal("25.000")


def test_diff_hedge_sequence_is_open_before_subscribe() -> None:
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON")},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge()
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    kinds = [a.kind for a in actions]
    # Open hedge BEFORE subscribe so the unhedged window is minimal.
    open_idx = kinds.index(ActionKind.OPEN_PERP_SHORT)
    sub_idx = kinds.index(ActionKind.SUBSCRIBE_EARN)
    assert open_idx < sub_idx


def test_diff_hedge_without_perp_market_falls_to_skip() -> None:
    # No perp_market entry for TON → can't price hedge qty → skip.
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge()
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    skips = [a for a in actions if a.kind == ActionKind.SKIP_OUT_OF_SCOPE]
    assert any("TON" in a.coin for a in skips)


# ─── Hedge close / diff (.32) ───────────────────────────────────────────────


def _no_hedge_decision() -> Decision:
    """Same shape as `_decision_with_hedge` but with `hedges=[]` — used
    to assert that an open position with no matching hedge is closed."""
    return Decision(
        thesis="TON OnChain pick fully unwound; close existing hedge.",
        venues=[_venue("cash_usdc", 1.0)],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=3.0,
    )


def test_diff_hedge_close_when_position_dropped_from_decision() -> None:
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
        perp_positions=[
            _short_pos("TON", size="25", position_value="50"),
        ],
    )
    d = _no_hedge_decision()
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    closes = [a for a in actions if a.kind == ActionKind.CLOSE_PERP]
    opens = [a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]
    assert len(closes) == 1
    assert not opens
    c = closes[0]
    assert c.product_id == "TONUSDT"
    assert c.coin == "TON"
    assert c.amount == Decimal("25")


def test_diff_hedge_open_only_when_no_current_position() -> None:
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
        perp_positions=[],
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert not [a for a in actions if a.kind == ActionKind.CLOSE_PERP]
    opens = [a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]
    assert len(opens) == 1
    assert opens[0].amount == Decimal("25.000")


def test_diff_hedge_no_op_when_notional_matches() -> None:
    """Position size matches decision target → no close, no reopen."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
        perp_positions=[
            _short_pos("TON", size="25", position_value="50.00"),
        ],
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    perp_kinds = {
        ActionKind.CLOSE_PERP,
        ActionKind.OPEN_PERP_SHORT,
    }
    assert not [a for a in actions if a.kind in perp_kinds]


def test_diff_hedge_resize_emits_close_then_open() -> None:
    """Decision asks for $80 but $50 is open → close+reopen, close first."""
    # Auto-hedge sizes from pick USD: bybit_onchain weight 0.80 × pick weight 1.0
    # × book $100 = $80 (override default 0.50 weight in factory by re-decoding).
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
        perp_positions=[
            _short_pos("TON", size="25", position_value="50.00"),
        ],
        onchain_products=[_TON_PRODUCT],
    )
    # Build a decision that targets $80 in TON (was: $50 cash + $50 onchain;
    # now $20 cash + $80 onchain so auto-hedge derives $80 notional).
    d = Decision(
        thesis="resize TON hedge: $50 -> $80",
        venues=[
            _venue("cash_usdc", 0.2),
            _venue("bybit_onchain", 0.8, [("8", 1.0)]),
        ],
        hedges=[Hedge(coin="TON", notional_usd=-80.0)],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=15.0,
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    kinds = [a.kind for a in actions]
    close_idx = kinds.index(ActionKind.CLOSE_PERP)
    open_idx = kinds.index(ActionKind.OPEN_PERP_SHORT)
    assert close_idx < open_idx
    close_action = actions[close_idx]
    open_action = actions[open_idx]
    assert close_action.amount == Decimal("25")  # close the whole short
    assert open_action.amount == Decimal("40.000")  # $80 / $2 mark


def test_diff_hedge_close_uses_unique_order_link_ids() -> None:
    """Close + open in the same cycle must produce distinct
    orderLinkIds — Bybit dedupes by it for ~30min."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
        perp_positions=[
            _short_pos("TON", size="25", position_value="50.00"),
        ],
    )
    d = _decision_with_hedge(hedge_notional=-80.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    link_ids = [a.order_link_id for a in actions]
    assert len(link_ids) == len(set(link_ids))


def test_diff_hedge_close_only_when_perp_market_missing() -> None:
    """Position exists but `perp_market` lost the entry between cycles —
    we can still safely close (size comes from the position, not mark),
    but we cannot reopen at any meaningful target size → skip the open.
    Pick weight set to 80% of book so auto-hedge target ($80) drifts
    from current ($50) and forces a close+reopen attempt."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={},  # no TON entry
        perp_positions=[
            _short_pos("TON", size="25", position_value="50.00"),
        ],
        onchain_products=[_TON_PRODUCT],
    )
    d = Decision(
        thesis="resize TON: $50 → $80, perp market missing forces close only",
        venues=[
            _venue("cash_usdc", 0.2),
            _venue("bybit_onchain", 0.8, [("8", 1.0)]),
        ],
        hedges=[Hedge(coin="TON", notional_usd=-80.0)],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=15.0,
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    closes = [a for a in actions if a.kind == ActionKind.CLOSE_PERP]
    opens = [a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]
    skips = [
        a
        for a in actions
        if a.kind == ActionKind.SKIP_OUT_OF_SCOPE and a.coin == "TON"
    ]
    assert len(closes) == 1
    assert not opens
    assert len(skips) == 1


def test_diff_hedge_sequence_close_before_open_before_subscribe() -> None:
    """Full chain: redeems → closes → opens → subscribes. Closes before
    opens frees margin for the new short in-cycle."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
        perp_positions=[
            _short_pos("TON", size="40", position_value="80.00"),
        ],
        onchain_products=[_TON_PRODUCT],
    )
    # Subscribe to a Flex product AND resize the hedge.
    d = Decision(
        thesis="resize TON hedge while subscribing to flex.",
        venues=[
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
            _venue("bybit_onchain", 0.5, [("8", 1.0)]),
        ],
        hedges=[Hedge(coin="TON", notional_usd=-50.0)],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=15.0,
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    kinds = [a.kind for a in actions]
    close_idx = kinds.index(ActionKind.CLOSE_PERP)
    open_idx = kinds.index(ActionKind.OPEN_PERP_SHORT)
    sub_idx = kinds.index(ActionKind.SUBSCRIBE_EARN)
    assert close_idx < open_idx < sub_idx


@pytest.mark.asyncio
async def test_execute_close_perp_dry_run(tmp_path: Path) -> None:
    client = AsyncMock()
    action = Action(
        kind=ActionKind.CLOSE_PERP,
        category="Perp",
        product_id="TONUSDT",
        coin="TON",
        amount=Decimal("25"),
        order_link_id="sandbox-test-close-000",
        reason="close TON short",
    )
    results = await execute_actions(
        client,
        [action],
        snapshot_ts="20260527T120000Z",
        dry_run=True,
        executions_dir=tmp_path,
    )
    assert results[0].status == "dry-run"
    payload = results[0].response
    assert payload["would_call"] == "place_perp_order"
    assert payload["side"] == "Buy"
    assert payload["reduce_only"] is True
    client.place_perp_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_close_perp_live_uses_buy_reduce_only(tmp_path: Path) -> None:
    client = AsyncMock()
    client.place_perp_order.return_value = SpotOrderResult(orderId="close-1")
    action = Action(
        kind=ActionKind.CLOSE_PERP,
        category="Perp",
        product_id="TONUSDT",
        coin="TON",
        amount=Decimal("25"),
        order_link_id="sandbox-test-close-001",
        reason="close TON short",
    )
    results = await execute_actions(
        client,
        [action],
        snapshot_ts="20260527T120000Z",
        dry_run=False,
        executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    assert results[0].response == {"orderId": "close-1"}
    # Close must NOT touch leverage — only opens force 1x.
    client.set_leverage.assert_not_called()
    client.place_perp_order.assert_awaited_once()
    kwargs = client.place_perp_order.await_args.kwargs
    assert kwargs["symbol"] == "TONUSDT"
    assert kwargs["side"] == "Buy"
    assert kwargs["qty"] == "25"
    assert kwargs["reduce_only"] is True
    assert kwargs["order_link_id"] == "sandbox-test-close-001"


@pytest.mark.asyncio
async def test_execute_open_perp_short_dry_run(tmp_path: Path) -> None:
    client = AsyncMock()
    action = Action(
        kind=ActionKind.OPEN_PERP_SHORT,
        category="Perp",
        product_id="TONUSDT",
        coin="TON",
        amount=Decimal("25.0"),
        order_link_id="sandbox-test-hedge-000",
        reason="short TON for OnChain hedge",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260527T120000Z",
        dry_run=True, executions_dir=tmp_path,
    )
    assert results[0].status == "dry-run"
    assert results[0].response["would_call"] == "place_perp_order"
    assert results[0].response["side"] == "Sell"
    assert results[0].response["leverage"] == 1
    client.place_perp_order.assert_not_called()
    client.set_leverage.assert_not_called()


# ─── USDT margin swap (.33) ─────────────────────────────────────────────────


def test_diff_swap_emitted_when_usdt_short_for_open_hedge() -> None:
    """Open hedge of $50, no USDT in UNIFIED → USDC→USDT swap $52.50
    (5% buffer). A non-stable Earn pick (TON) also triggers a parallel
    {coin}USDT Buy swap now (non-stable swap path landed 2026-06-03);
    here we just locate the hedge-side USDCUSDT swap."""
    snap = _snapshot(
        total_equity_usd="100",
        usdt_available_usd="0",
        perp_market={"TON": _perp("TON", mark="2.0")},
        perp_positions=[],
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    hedge_swaps = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT and a.product_id == "USDCUSDT"
    ]
    assert len(hedge_swaps) == 1
    sw = hedge_swaps[0]
    assert sw.side == "Sell"
    assert sw.coin == "USDT"
    assert sw.amount == Decimal("52.50")  # 50 * 1.05 buffer


def test_diff_no_swap_when_usdt_already_sufficient() -> None:
    """Existing $60 USDT covers the buffered $52.50 requirement."""
    snap = _snapshot(
        total_equity_usd="100",
        usdt_available_usd="60",
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert not [a for a in actions if a.kind == ActionKind.SWAP_SPOT]


def test_diff_swap_credits_margin_released_by_closes() -> None:
    """A close releases USDT margin → reduces required swap."""
    snap = _snapshot(
        total_equity_usd="200",
        usdt_available_usd="0",
        perp_market={
            "TON": _perp("TON", mark="2.0"),
            "DOGE": _perp("DOGE", mark="0.10"),
        },
        # Current $40 TON short → will be closed (no TON in target).
        perp_positions=[
            _short_pos("TON", size="20", position_value="40.00"),
        ],
    )
    # Decision drops TON, adds DOGE $30 hedge.
    d = Decision(
        thesis="switch TON hedge to DOGE.",
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_onchain", 0.5, [("8", 1.0)]),
        ],
        hedges=[Hedge(coin="DOGE", notional_usd=-30.0)],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=10.0,
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    swaps = [a for a in actions if a.kind == ActionKind.SWAP_SPOT]
    # required = 30 * 1.05 = 31.5; available = 0 + 40 (closed) = 40 → no swap.
    assert swaps == []


def test_diff_no_swap_when_no_hedges_planned() -> None:
    snap = _snapshot(total_equity_usd="100", usdt_available_usd="0")
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert not [a for a in actions if a.kind == ActionKind.SWAP_SPOT]


def test_diff_no_swap_when_open_skipped_due_to_missing_perp_market() -> None:
    """Hedge requested but `perp_market` absent → OPEN downgrades to SKIP;
    SKIP doesn't book margin → no swap needed."""
    snap = _snapshot(
        total_equity_usd="100",
        usdt_available_usd="0",
        perp_market={},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert not [a for a in actions if a.kind == ActionKind.SWAP_SPOT]


def test_diff_swap_suppressed_below_min_threshold() -> None:
    """Sub-dollar hedge-side shortfall doesn't emit a USDCUSDT SWAP —
    Bybit margin call won't fire on pennies, and a $0.30 swap is more
    noise than signal. The TON Buy swap (non-stable Earn coverage) is
    a separate concern and emits independently when above
    MIN_SWAP_USDC."""
    snap = _snapshot(
        total_equity_usd="100",
        usdt_available_usd="52.30",  # buffered req = 52.50 → 0.20 short
        perp_market={"TON": _perp("TON", mark="2.0")},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    hedge_swaps = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT and a.product_id == "USDCUSDT"
    ]
    assert not hedge_swaps


def test_diff_sequence_redeems_closes_swaps_opens_subscribes() -> None:
    """Full pipeline order — swap must sit between closes and opens so
    freed margin from closes is counted before the swap, and fresh USDT
    is available before opens fire."""
    snap = _snapshot(
        total_equity_usd="100",
        usdt_available_usd="0",
        perp_market={"TON": _perp("TON", mark="2.0")},
        earn_positions=[_pos("FlexibleSaving", "9999", "20")],  # drop pick
        onchain_products=[_TON_PRODUCT],
    )
    d = Decision(
        thesis="redeem old flex, open TON hedge with margin swap, sub flex.",
        venues=[
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
            _venue("bybit_onchain", 0.5, [("8", 1.0)]),
        ],
        hedges=[Hedge(coin="TON", notional_usd=-50.0)],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=12.0,
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    kinds = [a.kind for a in actions]
    redeem_idx = kinds.index(ActionKind.REDEEM_EARN)
    swap_idx = kinds.index(ActionKind.SWAP_SPOT)
    open_idx = kinds.index(ActionKind.OPEN_PERP_SHORT)
    sub_idx = kinds.index(ActionKind.SUBSCRIBE_EARN)
    assert redeem_idx < swap_idx < open_idx < sub_idx


@pytest.mark.asyncio
async def test_execute_swap_spot_dry_run(tmp_path: Path) -> None:
    client = AsyncMock()
    action = Action(
        kind=ActionKind.SWAP_SPOT,
        category="Spot",
        product_id="USDCUSDT",
        coin="USDT",
        amount=Decimal("52.50"),
        order_link_id="sandbox-test-swap-000",
        reason="swap for hedge margin",
    )
    results = await execute_actions(
        client,
        [action],
        snapshot_ts="20260527T120000Z",
        dry_run=True,
        executions_dir=tmp_path,
    )
    assert results[0].status == "dry-run"
    payload = results[0].response
    assert payload["would_call"] == "place_spot_order"
    assert payload["side"] == "Sell"
    assert payload["symbol"] == "USDCUSDT"
    assert payload["qty"] == "52.50"
    client.place_spot_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_swap_spot_live_sells_usdc(tmp_path: Path) -> None:
    client = AsyncMock()
    client.place_spot_order.return_value = SpotOrderResult(orderId="swap-1")
    action = Action(
        kind=ActionKind.SWAP_SPOT,
        category="Spot",
        product_id="USDCUSDT",
        coin="USDT",
        amount=Decimal("52.50"),
        order_link_id="sandbox-test-swap-001",
        reason="margin swap",
    )
    results = await execute_actions(
        client,
        [action],
        snapshot_ts="20260527T120000Z",
        dry_run=False,
        executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    assert results[0].response == {"orderId": "swap-1"}
    client.place_spot_order.assert_awaited_once()
    kwargs = client.place_spot_order.await_args.kwargs
    assert kwargs["symbol"] == "USDCUSDT"
    assert kwargs["side"] == "Sell"
    assert kwargs["qty"] == "52.50"
    assert kwargs["order_link_id"] == "sandbox-test-swap-001"


# ─── Advance-Earn execution (.35) ──────────────────────────────────────────


def _advance_decision(venue_id: str, product_id: str, weight: float = 0.5) -> Decision:
    return Decision(
        thesis="advance-Earn pick for sandbox test.",
        venues=[
            _venue("cash_usdc", 1.0 - weight),
            _venue(venue_id, weight, [(product_id, 1.0)]),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=8.0,
    )


def test_diff_dual_assets_emits_subscribe_advance_earn() -> None:
    quote = _dual_quote(base="BTC", quote_coin="USDT")
    snap = _snapshot(
        total_equity_usd="200",
        advance_earn_quotes={"DualAssets/da-1": quote},
        advance_products={
            "DualAssets": [_advance_product("DualAssets", "da-1", "BTC/USDT")]
        },
    )
    d = _advance_decision("bybit_dual_asset", "da-1", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN]
    assert len(subs) == 1
    a = subs[0]
    assert a.category == "DualAssets"
    assert a.product_id == "da-1"
    assert a.coin == "USDT"
    # weight 0.2 × 200 = 40
    assert a.amount == Decimal("40.0")
    # Reason carries the chosen strike + APR for audit.
    assert "DualAssets BTC/USDT buyLowPrice" in a.reason
    # And the encoded offer for executor rebuild.
    assert " offer=" in a.reason


def test_diff_discount_buy_emits_subscribe_advance_earn() -> None:
    quote = _discount_quote(coin="USDT", inst_uid="inst-xyz")
    snap = _snapshot(
        total_equity_usd="100",
        advance_earn_quotes={"DiscountBuy/db-7": quote},
        advance_products={
            "DiscountBuy": [_advance_product("DiscountBuy", "db-7", "USDT")]
        },
    )
    d = _advance_decision("bybit_discount_buy", "db-7", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN]
    assert len(subs) == 1
    a = subs[0]
    assert a.category == "DiscountBuy"
    assert a.product_id == "db-7"
    assert a.coin == "USDT"
    assert "inst-xyz" in a.reason


def test_diff_advance_earn_missing_quote_emits_skip() -> None:
    snap = _snapshot(
        total_equity_usd="200",
        advance_earn_quotes={},  # quote window didn't include this product
    )
    d = _advance_decision("bybit_dual_asset", "da-1", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert not [a for a in actions if a.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN]
    skips = [a for a in actions if a.kind == ActionKind.SKIP_OUT_OF_SCOPE]
    assert any("no cached quote" in s.reason for s in skips)


def test_diff_dual_assets_picks_only_non_expired_offer() -> None:
    """`.35` follow-up: when DualAssets returns a multi-offer payload
    with some rows already past their per-offer `expiredAt`, the diff
    must skip the stale ones and pick from the remaining set rather
    than emitting SKIP because the headline row happened to be old."""
    from datetime import timedelta

    now = datetime.now(UTC)
    fresh_ms = int((now + timedelta(seconds=600)).timestamp() * 1000)
    stale_ms = int((now - timedelta(seconds=60)).timestamp() * 1000)
    quote = _dual_quote(
        offers=[
            # Higher APR but expired → must be skipped
            {"selectPrice": "62000", "apyE8": "100000000", "expiredAt": str(stale_ms)},
            # Lower APR but fresh → this is the one we pick
            {"selectPrice": "60000", "apyE8": "50000000", "expiredAt": str(fresh_ms)},
        ]
    )
    snap = _snapshot(
        total_equity_usd="200",
        advance_earn_quotes={"DualAssets/da-1": quote},
        advance_products={
            "DualAssets": [_advance_product("DualAssets", "da-1", "BTC/USDT")]
        },
    )
    d = _advance_decision("bybit_dual_asset", "da-1", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN]
    assert len(subs) == 1
    # selectPrice=60000 is the fresh one despite its lower apyE8
    assert "60000" in subs[0].reason
    assert "62000" not in subs[0].reason


def test_diff_dual_assets_missing_from_snapshot_emits_skip() -> None:
    """When the product isn't in `snapshot.products["DualAssets"]`, we
    can't determine the stake coin → SKIP rather than guessing."""
    quote = _dual_quote()
    snap = _snapshot(
        total_equity_usd="200",
        advance_earn_quotes={"DualAssets/da-1": quote},
        advance_products={"DualAssets": []},  # product missing
    )
    d = _advance_decision("bybit_dual_asset", "da-1", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    skips = [a for a in actions if a.kind == ActionKind.SKIP_OUT_OF_SCOPE]
    assert any("product missing from snapshot" in s.reason for s in skips)


def test_diff_discount_buy_uses_offers_key_not_list() -> None:
    """Regression guard for the `.35` shape bug: live DiscountBuy quote
    payload has `offers` at top-level, not `list`. The diff must read
    from `offers` so picks subscribe instead of silently SKIP-ing with
    'empty quote list'."""
    quote = _discount_quote(coin="USDT", inst_uid="real-inst-1")
    # Sanity check on fixture itself — make sure we're testing the right shape.
    assert "offers" in quote and "list" not in quote
    snap = _snapshot(
        total_equity_usd="100",
        advance_earn_quotes={"DiscountBuy/db-7": quote},
        advance_products={
            "DiscountBuy": [_advance_product("DiscountBuy", "db-7", "USDT")]
        },
    )
    d = _advance_decision("bybit_discount_buy", "db-7", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN]
    assert len(subs) == 1
    assert "real-inst-1" in subs[0].reason


def test_diff_advance_earn_expired_offer_still_emits_subscribe() -> None:
    """2026-05-29 follow-up: stale-at-diff is NOT a SKIP. Bybit advance-
    Earn offers rotate every 30-60s; the diff-time quote may already be
    expired by the time the executor reaches dispatch. Diff emits
    SUBSCRIBE_ADVANCE_EARN with the stale offer encoded as fallback;
    execute refreshes the quote on the wire."""
    expired_quote = _dual_quote(expired_in_ms=-60_000)  # 1 min ago, per offer
    snap = _snapshot(
        total_equity_usd="200",
        advance_earn_quotes={"DualAssets/da-1": expired_quote},
        advance_products={
            "DualAssets": [_advance_product("DualAssets", "da-1", "BTC/USDT")]
        },
    )
    d = _advance_decision("bybit_dual_asset", "da-1", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN]
    assert len(subs) == 1
    assert "stale-at-diff" in subs[0].reason
    assert "execute will refresh" in subs[0].reason


def test_diff_discount_buy_stale_offer_still_emits_subscribe() -> None:
    """Same pattern for DiscountBuy: missing instUid at diff time
    doesn't kill the pick — execute time refresh may surface a usable
    offer. Diff emits SUBSCRIBE with empty fallback offer; execute
    handles the refresh."""
    quote = _discount_quote(inst_uid=None)
    snap = _snapshot(
        total_equity_usd="100",
        advance_earn_quotes={"DiscountBuy/db-1": quote},
        advance_products={
            "DiscountBuy": [_advance_product("DiscountBuy", "db-1", "USDT")]
        },
    )
    d = _advance_decision("bybit_discount_buy", "db-1", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN]
    assert len(subs) == 1
    assert "stale-at-diff" in subs[0].reason


def test_diff_smart_leverage_still_skips_with_explanatory_reason() -> None:
    """SmartLeverage / DoubleWin remain out-of-scope — `.36` will model
    their payoff later. The skip reason must NOT mention .11 (that line
    was tightened in `.35`)."""
    d = _advance_decision("bybit_smart_leverage", "sl-1", weight=0.1)
    snap = _snapshot(total_equity_usd="100")
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    skips = [
        a
        for a in actions
        if a.kind == ActionKind.SKIP_OUT_OF_SCOPE and a.category == "SmartLeverage"
    ]
    assert len(skips) == 1
    assert "SmartLeverage" in skips[0].reason


@pytest.mark.asyncio
async def test_execute_subscribe_advance_earn_dry_run(tmp_path: Path) -> None:
    client = AsyncMock()
    offer = {
        "selectPrice": "62000",
        "side": "Buy",
        "expiredTime": "9999999999999",
        "apyE8": "80000000",
    }
    action = Action(
        kind=ActionKind.SUBSCRIBE_ADVANCE_EARN,
        category="DualAssets",
        product_id="da-1",
        coin="USDT",
        amount=Decimal("40"),
        order_link_id="sandbox-test-adv-000",
        reason=f"subscribe DualAssets/da-1 (USDT) $40.00: x offer={json.dumps(offer)}",
    )
    results = await execute_actions(
        client,
        [action],
        snapshot_ts="20260527T120000Z",
        dry_run=True,
        executions_dir=tmp_path,
    )
    assert results[0].status == "dry-run"
    payload = results[0].response
    assert payload["would_call"] == "place_advance_earn_order"
    assert payload["category"] == "DualAssets"
    assert payload["coin"] == "USDT"
    assert payload["extra"]["dualAssetsExtra"]["selectPrice"] == "62000"
    client.place_advance_earn_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_subscribe_advance_earn_live_dual_assets(tmp_path: Path) -> None:
    client = AsyncMock()
    client.place_advance_earn_order.return_value = {"orderId": "adv-1"}
    # Refresh-at-execute returns an empty quote (no fresh offer) so the
    # dispatch falls back to the diff-time offer encoded in `reason`.
    client.get_advance_product_quote.return_value = {}
    offer = {
        "selectPrice": "62000",
        "side": "Buy",
        "expiredTime": "9999999999999",
        "apyE8": "80000000",
    }
    action = Action(
        kind=ActionKind.SUBSCRIBE_ADVANCE_EARN,
        category="DualAssets",
        product_id="da-1",
        coin="USDT",
        amount=Decimal("40"),
        order_link_id="sandbox-test-adv-001",
        reason=f"subscribe DualAssets x offer={json.dumps(offer)}",
    )
    results = await execute_actions(
        client,
        [action],
        snapshot_ts="20260527T120000Z",
        dry_run=False,
        executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    assert results[0].response == {"orderId": "adv-1"}
    client.place_advance_earn_order.assert_awaited_once()
    kwargs = client.place_advance_earn_order.await_args.kwargs
    assert kwargs["category"] == "DualAssets"
    assert kwargs["product_id"] == "da-1"
    assert kwargs["side"] == "Stake"
    assert kwargs["coin"] == "USDT"
    assert kwargs["amount"] == "40"
    assert kwargs["account_type"] == "UNIFIED"
    assert kwargs["extra"]["dualAssetsExtra"]["selectPrice"] == "62000"
    assert kwargs["extra"]["dualAssetsExtra"]["apyE8"] == "80000000"


@pytest.mark.asyncio
async def test_execute_subscribe_advance_earn_uses_fresh_quote_over_stale(
    tmp_path: Path,
) -> None:
    """`.35` 2026-05-29 follow-up: when the execute-time quote refresh
    returns a fresh offer, dispatch uses THAT (with the up-to-date
    selectPrice/apyE8), NOT the stale diff-time fallback encoded in
    action.reason."""
    client = AsyncMock()
    client.place_advance_earn_order.return_value = {"orderId": "adv-fresh"}
    fresh_quote = {
        "category": "DualAssets",
        "list": [
            {
                "productId": "da-1",
                "buyLowPrice": [
                    {
                        "selectPrice": "60500",  # different from stale 62000
                        "side": "Buy",
                        "expiredAt": "9999999999999",
                        "apyE8": "90000000",  # different from stale 80000000
                    }
                ],
                "sellHighPrice": [],
            }
        ],
    }
    client.get_advance_product_quote.return_value = fresh_quote
    stale_offer = {
        "selectPrice": "62000",
        "side": "Buy",
        "expiredTime": "9999999999999",
        "apyE8": "80000000",
    }
    action = Action(
        kind=ActionKind.SUBSCRIBE_ADVANCE_EARN,
        category="DualAssets",
        product_id="da-1",
        coin="USDT",
        amount=Decimal("40"),
        order_link_id="sandbox-test-adv-fresh",
        reason=f"subscribe DualAssets x offer={json.dumps(stale_offer)}",
    )
    results = await execute_actions(
        client,
        [action],
        snapshot_ts="20260527T120000Z",
        dry_run=False,
        executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    kwargs = client.place_advance_earn_order.await_args.kwargs
    # Dispatch must use the FRESH offer, not the stale one from reason.
    assert kwargs["extra"]["dualAssetsExtra"]["selectPrice"] == "60500"
    assert kwargs["extra"]["dualAssetsExtra"]["apyE8"] == "90000000"
    client.get_advance_product_quote.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_subscribe_advance_earn_live_discount_buy(tmp_path: Path) -> None:
    client = AsyncMock()
    client.place_advance_earn_order.return_value = {"orderId": "adv-2"}
    client.get_advance_product_quote.return_value = {}  # see DualAssets test
    offer = {
        "instUid": "inst-xyz",
        "currentPrice": "65000",
        "purchasePrice": "63000",
        "knockoutPrice": "55000",
        "knockoutCouponE8": "100000000",
        "expiredAt": "9999999999999",
    }
    action = Action(
        kind=ActionKind.SUBSCRIBE_ADVANCE_EARN,
        category="DiscountBuy",
        product_id="db-7",
        coin="USDT",
        amount=Decimal("20"),
        order_link_id="sandbox-test-adv-002",
        reason=f"subscribe DiscountBuy x offer={json.dumps(offer)}",
    )
    results = await execute_actions(
        client,
        [action],
        snapshot_ts="20260527T120000Z",
        dry_run=False,
        executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    kwargs = client.place_advance_earn_order.await_args.kwargs
    assert kwargs["category"] == "DiscountBuy"
    assert kwargs["extra"]["discountBuyExtra"]["instUid"] == "inst-xyz"
    assert kwargs["extra"]["discountBuyExtra"]["purchasePrice"] == "63000"


# ─── Approval gate (.12) ───────────────────────────────────────────────────


class _FakeStdin:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _approval_decision(confidence: float = 0.7) -> Decision:
    return Decision(
        thesis="placeholder thesis for approval test cycle.",
        venues=[
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ],
        hedges=[],
        confidence=confidence,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=4.0,
    )


def _approval_actions() -> list[Action]:
    return [
        Action(
            kind=ActionKind.SUBSCRIBE_EARN,
            category="FlexibleSaving",
            product_id="1131",
            coin="USD1",
            amount=Decimal("50"),
            order_link_id="sandbox-test-0",
            reason="t",
        )
    ]


def test_approval_yes_with_high_confidence_auto_approves() -> None:
    from agent.sandbox.execute import request_approval

    d = _approval_decision(confidence=0.8)
    ok = request_approval(
        d, _approval_actions(),
        yes=True, min_confidence=0.6,
        stdin=_FakeStdin(tty=False),
        input_fn=lambda _: pytest.fail("should not prompt"),
    )
    assert ok is True


def test_approval_yes_below_min_falls_back_to_prompt() -> None:
    from agent.sandbox.execute import request_approval

    d = _approval_decision(confidence=0.5)
    calls = []
    def fake_input(prompt: str) -> str:
        calls.append(prompt)
        return "y"
    ok = request_approval(
        d, _approval_actions(),
        yes=True, min_confidence=0.6,
        stdin=_FakeStdin(tty=True),
        input_fn=fake_input,
    )
    assert ok is True
    assert len(calls) == 1  # prompted exactly once


def test_approval_non_tty_no_yes_refuses() -> None:
    from agent.sandbox.execute import request_approval

    d = _approval_decision(confidence=0.8)
    ok = request_approval(
        d, _approval_actions(),
        yes=False, min_confidence=0.6,
        stdin=_FakeStdin(tty=False),
        input_fn=lambda _: pytest.fail("should not prompt"),
    )
    assert ok is False


def test_approval_interactive_y_proceeds() -> None:
    from agent.sandbox.execute import request_approval

    d = _approval_decision()
    ok = request_approval(
        d, _approval_actions(),
        yes=False, min_confidence=0.6,
        stdin=_FakeStdin(tty=True),
        input_fn=lambda _: "y",
    )
    assert ok is True


def test_approval_interactive_n_aborts() -> None:
    from agent.sandbox.execute import request_approval

    d = _approval_decision()
    ok = request_approval(
        d, _approval_actions(),
        yes=False, min_confidence=0.6,
        stdin=_FakeStdin(tty=True),
        input_fn=lambda _: "n",
    )
    assert ok is False


def test_approval_interactive_empty_defaults_to_no() -> None:
    from agent.sandbox.execute import request_approval

    d = _approval_decision()
    ok = request_approval(
        d, _approval_actions(),
        yes=False, min_confidence=0.6,
        stdin=_FakeStdin(tty=True),
        input_fn=lambda _: "",
    )
    assert ok is False


def test_approval_interactive_eof_aborts() -> None:
    from agent.sandbox.execute import request_approval

    d = _approval_decision()
    def raise_eof(_: str) -> str:
        raise EOFError
    ok = request_approval(
        d, _approval_actions(),
        yes=False, min_confidence=0.6,
        stdin=_FakeStdin(tty=True),
        input_fn=raise_eof,
    )
    assert ok is False


def test_approval_accepts_russian_yes() -> None:
    from agent.sandbox.execute import request_approval

    d = _approval_decision()
    ok = request_approval(
        d, _approval_actions(),
        yes=False, min_confidence=0.6,
        stdin=_FakeStdin(tty=True),
        input_fn=lambda _: "да",
    )
    assert ok is True


@pytest.mark.asyncio
async def test_execute_open_perp_short_live_sets_leverage_then_places(tmp_path: Path) -> None:
    client = AsyncMock()
    client.set_leverage.return_value = None
    client.place_perp_order.return_value = SpotOrderResult(orderId="perp-1")
    action = Action(
        kind=ActionKind.OPEN_PERP_SHORT,
        category="Perp",
        product_id="TONUSDT",
        coin="TON",
        amount=Decimal("25.0"),
        order_link_id="sandbox-test-hedge-001",
        reason="hedge",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260527T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    assert results[0].response == {"orderId": "perp-1"}
    client.set_leverage.assert_awaited_once_with("TONUSDT", 1)
    client.place_perp_order.assert_awaited_once()
    kwargs = client.place_perp_order.await_args.kwargs
    assert kwargs["symbol"] == "TONUSDT"
    assert kwargs["side"] == "Sell"
    assert kwargs["qty"] == "25.0"
    assert kwargs["order_link_id"] == "sandbox-test-hedge-001"


# ─── LM execute dispatch (.47) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_subscribe_lm_dry_run(tmp_path: Path) -> None:
    """Dry-run shows the planned add_liquidity call shape — single-sided
    USDC quote_amount, leverage=1, no live client invocation."""
    client = AsyncMock()
    action = Action(
        kind=ActionKind.SUBSCRIBE_LM,
        category="LiquidityMining",
        product_id="24",
        coin="USDC",
        amount=Decimal("5"),
        order_link_id="sandbox-lm-000",
        reason="subscribe LM/24",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260527T120000Z",
        dry_run=True, executions_dir=tmp_path,
    )
    assert results[0].status == "dry-run"
    payload = results[0].response
    assert payload["would_call"] == "add_liquidity"
    assert payload["quote_amount"] == "5"
    assert payload["quote_account_type"] == "UNIFIED"
    assert payload["leverage"] == "1"
    client.add_liquidity.assert_not_called()


@pytest.mark.asyncio
async def test_execute_subscribe_lm_live_calls_add_liquidity(tmp_path: Path) -> None:
    """Live SUBSCRIBE_LM dispatches to BybitClient.add_liquidity with the
    single-sided USDC body the diff layer encoded."""
    from agent.bybit_oracle.bybit_client import LMOrderResult

    client = AsyncMock()
    client.add_liquidity.return_value = LMOrderResult(
        orderId="lm-abc", orderLinkId="sandbox-lm-001"
    )
    action = Action(
        kind=ActionKind.SUBSCRIBE_LM,
        category="LiquidityMining",
        product_id="24",
        coin="USDC",
        amount=Decimal("5"),
        order_link_id="sandbox-lm-001",
        reason="subscribe",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260527T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    assert results[0].response == {"orderId": "lm-abc"}
    client.add_liquidity.assert_awaited_once()
    kwargs = client.add_liquidity.await_args.kwargs
    assert kwargs["product_id"] == "24"
    assert kwargs["quote_amount"] == "5"
    assert kwargs["quote_account_type"] == "UNIFIED"
    assert kwargs["leverage"] == "1"
    assert kwargs["order_link_id"] == "sandbox-lm-001"


@pytest.mark.asyncio
async def test_execute_redeem_lm_live_calls_remove_liquidity(tmp_path: Path) -> None:
    """REDEEM_LM full exit uses position_id from the action, removeRate=100,
    removeType=Normal (returns both coins pro-rata)."""
    from agent.bybit_oracle.bybit_client import LMOrderResult

    client = AsyncMock()
    client.remove_liquidity.return_value = LMOrderResult(orderId="rm-1")
    action = Action(
        kind=ActionKind.REDEEM_LM,
        category="LiquidityMining",
        product_id="24",
        coin="USDC",
        amount=Decimal("20"),
        order_link_id="sandbox-lm-002",
        reason="full exit",
        position_id="9001",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260527T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    client.remove_liquidity.assert_awaited_once()
    kwargs = client.remove_liquidity.await_args.kwargs
    assert kwargs["product_id"] == "24"
    assert kwargs["position_id"] == "9001"
    assert kwargs["remove_rate"] == 100
    assert kwargs["remove_type"] == "Normal"


@pytest.mark.asyncio
async def test_execute_redeem_lm_missing_position_id_raises(tmp_path: Path) -> None:
    """A REDEEM_LM action without position_id is a programming error in
    the diff layer — surface loudly as RuntimeError rather than silently
    sending a malformed body to Bybit."""
    client = AsyncMock()
    action = Action(
        kind=ActionKind.REDEEM_LM,
        category="LiquidityMining",
        product_id="24",
        coin="USDC",
        amount=Decimal("20"),
        order_link_id="sandbox-lm-003",
        reason="missing pid",
        position_id=None,
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260527T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "error"
    assert "position_id" in (results[0].error or "")
    client.remove_liquidity.assert_not_called()


@pytest.mark.asyncio
async def test_execute_claim_lm_live_calls_claim_interest(tmp_path: Path) -> None:
    """CLAIM_LM dispatches to claim_lm_interest with the product_id from
    the action — `"-1"` claims every active position in one round-trip."""
    client = AsyncMock()
    client.claim_lm_interest.return_value = None
    action = Action(
        kind=ActionKind.CLAIM_LM,
        category="LiquidityMining",
        product_id="-1",
        coin="USDC",
        amount=Decimal("0"),
        order_link_id="sandbox-lm-004",
        reason="claim all",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260527T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    assert results[0].response == {"claimed": True}
    client.claim_lm_interest.assert_awaited_once_with(product_id="-1")


# ─── USDT budget enforcement (2026-06-03) ───────────────────────────────────


def _spot_action(
    *,
    product_id: str,
    coin: str,
    amount: str,
    side: str,
    idx: int = 0,
) -> Action:
    return Action(
        kind=ActionKind.SWAP_SPOT,
        category="Spot",
        product_id=product_id,
        coin=coin,
        amount=Decimal(amount),
        side=side,
        order_link_id=f"sandbox-test-{idx:03d}",
        reason="test",
    )


def _open_short_action(coin: str, qty: str, idx: int = 0) -> Action:
    return Action(
        kind=ActionKind.OPEN_PERP_SHORT,
        category="linear",
        product_id=f"{coin}USDT",
        coin=coin,
        amount=Decimal(qty),
        order_link_id=f"sandbox-hedge-{idx:03d}",
        reason="test",
    )


def test_enforce_usdt_budget_noop_when_liquid_zero() -> None:
    """liquid_usdt=0 → no-op (preserves pre-budget runtime behavior, mirrors
    `_enforce_usdc_budget` early-out). Tests that don't populate the field
    keep their original expectations."""
    snap = _snapshot(total_equity_usd="100", perp_market={"ID": _perp("ID", mark="0.5")})
    buy_swap = _spot_action(product_id="IDUSDT", coin="ID", amount="40", side="Buy")
    kept, dropped = _enforce_usdt_budget(
        Decimal("0"),
        hedge_swaps=[],
        hedge_opens=[_open_short_action("ID", "80")],
        hedge_closes=[],
        earn_swaps=[buy_swap],
        snapshot=snap,
    )
    assert kept == [buy_swap]
    assert dropped == set()


def test_enforce_usdt_budget_drops_buy_when_perp_consumes_all() -> None:
    """liquid_usdt + hedge_swap_inflow + close_release fully covers
    perp_demand → no headroom for non-stable Buy swap → drop it."""
    snap = _snapshot(total_equity_usd="100", perp_market={"ID": _perp("ID", mark="0.5")})
    # perp ID short 80 qty × $0.5 mark × 1.05 buffer = $42 margin
    # liquid_usdt $10 + hedge swap $32 inflow = $42 supply → exact match
    hedge_swap = _spot_action(product_id="USDCUSDT", coin="USDT", amount="32", side="Sell")
    buy_swap = _spot_action(product_id="IDUSDT", coin="ID", amount="40", side="Buy", idx=1)
    kept, dropped = _enforce_usdt_budget(
        Decimal("10"),
        hedge_swaps=[hedge_swap],
        hedge_opens=[_open_short_action("ID", "80")],
        hedge_closes=[],
        earn_swaps=[buy_swap],
        snapshot=snap,
    )
    assert dropped == {"ID"}
    assert kept == []


def test_enforce_usdt_budget_keeps_buy_with_headroom() -> None:
    """liquid_usdt covers both perp margin + Buy swap → Buy stays."""
    snap = _snapshot(total_equity_usd="200", perp_market={"ID": _perp("ID", mark="0.5")})
    # perp demand = 80 × 0.5 × 1.05 = $42; Buy = $40 → total $82
    # liquid_usdt $100 supply → fits
    buy_swap = _spot_action(product_id="IDUSDT", coin="ID", amount="40", side="Buy")
    kept, dropped = _enforce_usdt_budget(
        Decimal("100"),
        hedge_swaps=[],
        hedge_opens=[_open_short_action("ID", "80")],
        hedge_closes=[],
        earn_swaps=[buy_swap],
        snapshot=snap,
    )
    assert dropped == set()
    assert kept == [buy_swap]


def test_enforce_usdt_budget_drops_tail_buys_sequentially() -> None:
    """Two Buy swaps, only first fits in remaining budget → drop second."""
    snap = _snapshot(
        total_equity_usd="200",
        perp_market={
            "ID": _perp("ID", mark="0.5"),
            "IO": _perp("IO", mark="1.0"),
        },
    )
    # No perp margin; liquid_usdt = $50; first Buy $30 fits, second $30 overflows
    buy_a = _spot_action(product_id="IDUSDT", coin="ID", amount="30", side="Buy", idx=0)
    buy_b = _spot_action(product_id="IOUSDT", coin="IO", amount="30", side="Buy", idx=1)
    kept, dropped = _enforce_usdt_budget(
        Decimal("50"),
        hedge_swaps=[],
        hedge_opens=[],
        hedge_closes=[],
        earn_swaps=[buy_a, buy_b],
        snapshot=snap,
    )
    assert dropped == {"IO"}
    assert kept == [buy_a]


def test_enforce_usdt_budget_credits_close_release() -> None:
    """CLOSE_PERP releases its IM back as USDT → counts as supply,
    lets a Buy swap fit that would otherwise overflow."""
    snap = _snapshot(
        total_equity_usd="200",
        perp_market={
            "TON": _perp("TON", mark="5.0"),
            "ID": _perp("ID", mark="0.5"),
        },
    )
    # No live perp; closing TON 10 qty × $5 mark = $50 release
    # liquid_usdt = $0; Buy $40 → supply $50 ≥ demand $40 → keep
    close = Action(
        kind=ActionKind.CLOSE_PERP,
        category="linear",
        product_id="TONUSDT",
        coin="TON",
        amount=Decimal("10"),
        order_link_id="sandbox-close-000",
        reason="test",
    )
    buy = _spot_action(product_id="IDUSDT", coin="ID", amount="40", side="Buy")
    kept, dropped = _enforce_usdt_budget(
        # Tiny non-zero liquid to bypass early-out; supply still ~ close
        Decimal("0.01"),
        hedge_swaps=[],
        hedge_opens=[],
        hedge_closes=[close],
        earn_swaps=[buy],
        snapshot=snap,
    )
    assert dropped == set()
    assert kept == [buy]


def test_diff_subscribe_quantizes_amount_to_stake_precision() -> None:
    """`.precision` quantization (2026-06-03 fix for live retCode=180001
    on USDT Flex product 1). When the product surfaces `stake_precision`,
    the emitted SUBSCRIBE_EARN amount must be rounded DOWN to that many
    decimal places — never out-precision the product on the wire."""
    usdt_flex = ProductSummary(
        category="FlexibleSaving",
        product_id="1",
        coin="USDT",
        effective_apr=Decimal("0.0224"),
        apr_source="estimate_apr",
        min_subscribe_usd=Decimal("1.5"),
        stake_precision=4,
    )
    snap = _snapshot(
        total_equity_usd="100",
        # FUND has USDT for the subscribe to be sourceable
        usdt_available_usd="100",
        flex_products=[usdt_flex],
    )
    # weight 0.107 of $100 = $10.70; pick.weight=1.0 → raw $10.70.
    # Should serialize to amount="10.7000" (4 decimals), never 5+.
    d = Decision(
        thesis="USDT Flex pick to validate precision quantization.",
        venues=[
            _venue("cash_usdc", 0.893),
            _venue("bybit_flex", 0.107, [("1", 1.0)]),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=2.0,
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260603T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    assert len(subs) == 1
    sent = subs[0].amount
    # Quantized to ≤4 decimals (no 5+ decimal artifact like 10.69056).
    exp = sent.as_tuple().exponent
    assert exp >= -4, f"amount={sent} has {-exp} decimals, want ≤4"
    # And rounded DOWN (never above the requested delta).
    assert sent <= Decimal("10.70")


def test_diff_subscribe_skips_when_precision_rounds_below_min_action() -> None:
    """If precision rounds the delta below MIN_ACTION_USDC, skip the
    subscribe rather than emit a sub-min action that the Earn endpoint
    would reject downstream."""
    tiny_precision = ProductSummary(
        category="FlexibleSaving",
        product_id="99",
        coin="USDC",
        effective_apr=Decimal("0.05"),
        apr_source="estimate_apr",
        min_subscribe_usd=Decimal("0.01"),
        stake_precision=0,  # integer-only — rounds $0.99 down to $0
    )
    snap = _snapshot(
        total_equity_usd="100",
        flex_products=[tiny_precision],
    )
    d = Decision(
        thesis="Edge case: precision=0 + tiny weight rounds to zero.",
        venues=[
            _venue("cash_usdc", 0.9901),
            _venue("bybit_flex", 0.0099, [("99", 1.0)]),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=0.5,
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260603T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    assert subs == []


def test_enforce_usdt_budget_leaves_sell_swaps_untouched() -> None:
    """USDC→stable Sell swaps don't spend USDT (capped by USDC budget
    instead). They pass through this filter regardless."""
    snap = _snapshot(total_equity_usd="100", perp_market={"ID": _perp("ID", mark="0.5")})
    sell = _spot_action(product_id="USDCUSDT", coin="USDT", amount="20", side="Sell")
    buy = _spot_action(product_id="IDUSDT", coin="ID", amount="30", side="Buy", idx=1)
    kept, dropped = _enforce_usdt_budget(
        Decimal("50"),
        hedge_swaps=[],
        hedge_opens=[],
        hedge_closes=[],
        earn_swaps=[sell, buy],
        snapshot=snap,
    )
    # Sell goes first (preserved order in `other_swaps + kept_buy`); Buy fits.
    assert dropped == set()
    assert kept == [sell, buy]
