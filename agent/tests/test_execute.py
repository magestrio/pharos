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
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.bybit_oracle.bybit_client import (
    BybitAPIError,
    EarnOrderResult,
    OrderHistoryEntry,
    PerpPosition,
    SpotOrderResult,
)
from agent.reason.schema import Decision, Hedge, Pick, VenueAllocation
from agent.sandbox.execute import (
    MIN_ACTION_USDC,
    MIN_SWAP_USDC,
    Action,
    ActionKind,
    diff_to_actions,
    execute_actions,
    reconcile_executions,
    verify_executions_against_bybit,
    _enforce_usdt_budget,
    _execute_one,
    _hedged_pick_underfunded_coins,
    _order_link_id,
    _stable_consolidate_actions,
    _swap_actions_for_hedges,
    _unfunded_nonstable_subscribe_coins,
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
    liquid_usdc_usd: str | None = None,
    liquid_usdt_usd: str | None = None,
    earn_positions: list[dict] | None = None,
    perp_market: dict[str, PerpInfo] | None = None,
    perp_positions: list[PerpPosition] | None = None,
    advance_earn_quotes: dict[str, dict] | None = None,
    advance_earn_positions: dict[str, list[dict]] | None = None,
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
    # Default `liquid_usdt_usd` to `usdt_available_usd` so legacy tests
    # that only set the UNIFIED-side balance still pass — production
    # snapshots populate both, but tests pre-dating the 2026-06-03 fix
    # never knew the field existed.
    liquid_usdt_default = (
        liquid_usdt_usd if liquid_usdt_usd is not None else usdt_available_usd
    )
    return Snapshot(
        captured_at=datetime.now(UTC),
        wallet=WalletSnapshot(
            total_equity_usd=Decimal(total_equity_usd),
            usdt_available_usd=Decimal(usdt_available_usd),
            liquid_usdc_usd=Decimal(liquid_usdc_usd or "0"),
            liquid_usdt_usd=Decimal(liquid_usdt_default),
        ),
        earn_positions=earn_positions or [],
        lm_positions=lm_positions or [],
        alpha_positions=alpha_positions or [],
        products=products,
        market=MarketSnapshot(),
        perp_market=perp_market or {},
        perp_positions=perp_positions or [],
        advance_earn_quotes=advance_earn_quotes or {},
        advance_earn_positions=advance_earn_positions or {},
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


def _perp(
    coin: str,
    *,
    mark: str = "2.0",
    min_notional: str = "0.5",
    min_order_qty: str = "0.1",
) -> PerpInfo:
    return PerpInfo(
        symbol=f"{coin.upper()}USDT",
        funding_rate_8h=Decimal("0.0001"),
        mark_price=Decimal(mark),
        orderbook_depth_50bps_usd=Decimal("100000"),
        min_order_qty=Decimal(min_order_qty),
        min_notional_usd=Decimal(min_notional),
        max_leverage=Decimal("50"),
    )


def _pos(
    category: str,
    product_id: str,
    amount: str,
    coin: str = "USDC",
    status: str = "",
) -> dict:
    """A raw EarnPosition dict — mirrors `EarnPosition.model_dump()`.
    `status="Processing"` marks an un-redeemable (still-settling) chunk."""
    return {
        "category": category,
        "productId": product_id,
        "coin": coin,
        "amount": amount,
        "status": status,
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


def test_diff_skips_redeem_when_position_fully_processing() -> None:
    """Regression (`bybit-sandbox.65`, prod 2026-06-07): a held OnChain
    position whose entire balance is still `Processing` cannot be redeemed
    (place-order Redeem reverts retCode=180020). When the LLM drops it,
    the diff must SKIP rather than emit a doomed REDEEM — the position is
    held until Bybit settles it; the hedge stays via the atomic-pair
    guard."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="1.5", min_notional="1.0")},
        earn_positions=[
            _pos("OnChain", "8", "4.154", coin="TON", status="Processing"),
            _pos("OnChain", "8", "3.382", coin="TON", status="Processing"),
            _pos("OnChain", "8", "2.987", coin="TON", status="Processing"),
        ],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260607T120000Z")
    ton_redeems = [
        a for a in actions
        if a.kind == ActionKind.REDEEM_EARN and a.product_id == "8"
    ]
    assert ton_redeems == []  # no doomed redeem on a Processing position
    ton_skips = [
        a for a in actions
        if a.kind == ActionKind.SKIP_OUT_OF_SCOPE and a.product_id == "8"
    ]
    assert len(ton_skips) == 1
    assert "Processing" in ton_skips[0].reason


def test_diff_redeems_partially_settled_position() -> None:
    """A position with a settled (redeemable) chunk + a Processing chunk is
    NOT fully processing → the redeem still fires (the Processing guard
    only suppresses the entirely-unsettled case)."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="1.5", min_notional="1.0")},
        earn_positions=[
            _pos("OnChain", "8", "4.0", coin="TON", status="Active"),
            _pos("OnChain", "8", "3.0", coin="TON", status="Processing"),
        ],
    )
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260607T120000Z")
    ton_redeems = [
        a for a in actions
        if a.kind == ActionKind.REDEEM_EARN and a.product_id == "8"
    ]
    assert len(ton_redeems) == 1


def test_diff_orphan_spot_sold_to_usdt_when_not_subscribed() -> None:
    """Regression for 2026-06-03: after redeeming LIT Earn or after a
    failed subscribe leaves Buy proceeds in UNIFIED, the orphan spot
    sits naked-long unless we Sell it back to USDT. Auto-orphan-cleanup
    must emit a SWAP_SPOT Sell on {coin}USDT for each orphan above
    MIN_SWAP_USDC."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"LIT": _perp("LIT", mark="1.65", min_notional="1.0")},
    )
    # Inject the orphan via wallet directly — fixture default doesn't
    # populate unified_coin_balances.
    snap.wallet.unified_coin_balances = {"LIT": Decimal("4.9")}
    # LLM picks nothing for LIT (cash + USD1 only).
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260603T180000Z")
    sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "LITUSDT"
        and a.side == "Sell"
    ]
    assert len(sells) == 1, [a.kind for a in actions]
    s = sells[0]
    # Rounded DOWN to qty_step (default 0.001 when not surfaced).
    assert s.amount > Decimal("4.8")
    assert s.amount <= Decimal("4.9")
    assert "orphan" in s.reason.lower()


def test_diff_orphan_spot_sold_from_fund_account() -> None:
    """`bybit-sandbox` 2026-06-08: principal freed by an LM/LP redeem
    settles in the FUND account, not UNIFIED (live: ~17 TIA after an LM
    exit). The orphan-seller must scan FUND too — the SWAP_SPOT Sell
    dispatch transfers it FUND→UNIFIED first — else the non-stable sits as
    naked directional spot forever, violating the controlled-risk thesis."""
    snap = _snapshot(
        total_equity_usd="180",
        perp_market={"TIA": _perp("TIA", mark="0.32", min_notional="1.0")},
    )
    snap.wallet.unified_coin_balances = {}  # nothing sellable in UNIFIED
    snap.wallet.fund_coin_balances = {"TIA": Decimal("17.08")}  # freed LM principal
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260608T120000Z")
    sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "TIAUSDT"
        and a.side == "Sell"
    ]
    assert len(sells) == 1, [a.kind for a in actions]
    assert sells[0].amount > Decimal("17")
    assert "orphan" in sells[0].reason.lower()


def test_diff_orphan_spot_merges_unified_and_fund() -> None:
    """A non-stable split across UNIFIED and FUND is summed for the
    sell-down (total long = wallet UNIFIED+FUND), not scanned per-account."""
    snap = _snapshot(
        total_equity_usd="180",
        perp_market={"TIA": _perp("TIA", mark="0.32", min_notional="1.0")},
    )
    snap.wallet.unified_coin_balances = {"TIA": Decimal("5")}
    snap.wallet.fund_coin_balances = {"TIA": Decimal("12")}
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260608T120000Z")
    sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "TIAUSDT"
        and a.side == "Sell"
    ]
    assert len(sells) == 1
    assert sells[0].amount > Decimal("16.9")  # ~17 combined, rounded down


def test_diff_orphan_spot_btc_routes_to_usdc_directly() -> None:
    """`.49`: BTC settling out of a 1d DiscountBuy lands in UNIFIED with
    no Earn position and no perp short backing it. Orphan cleanup must
    route to `BTCUSDC` (single hop) instead of `BTCUSDT` + a subsequent
    USDT→USDC sweep. Vault is USDC-denominated; the indirect path
    pays double fees and orphans USDT."""
    snap = _snapshot(
        total_equity_usd="100",
        # Live Bybit BTC perp min_order_qty is 0.001 BTC — the default
        # 0.1 used elsewhere in this fixture is a placeholder that would
        # SKIP a realistic $100-of-BTC orphan as below-min.
        perp_market={
            "BTC": _perp(
                "BTC", mark="65000", min_notional="5.0", min_order_qty="0.001"
            )
        },
    )
    snap.wallet.unified_coin_balances = {"BTC": Decimal("0.0015")}
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260604T120000Z")
    sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT and a.product_id == "BTCUSDC"
    ]
    assert len(sells) == 1, [a.product_id for a in actions if a.kind == ActionKind.SWAP_SPOT]
    s = sells[0]
    assert s.side == "Sell"
    assert s.coin == "USDC"
    # No leftover BTCUSDT order — only BTCUSDC.
    assert not [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT and a.product_id == "BTCUSDT"
    ]
    assert "→ USDC" in s.reason


def test_diff_orphan_spot_eth_routes_to_usdc() -> None:
    """Same as BTC: ETH is the other common DiscountBuy underlying and
    has a deep ETHUSDC spot pair on Bybit (verified in `.4`)."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={
            "ETH": _perp(
                "ETH", mark="3500", min_notional="1.0", min_order_qty="0.01"
            )
        },
    )
    snap.wallet.unified_coin_balances = {"ETH": Decimal("0.03")}
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260604T120000Z")
    sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT and a.product_id == "ETHUSDC"
    ]
    assert len(sells) == 1
    assert sells[0].coin == "USDC"


def test_diff_orphan_spot_other_coin_keeps_usdt_route() -> None:
    """Coins outside the `_USDC_PAIR_COINS` whitelist (TON, LIT, AGIX,
    long-tail memecoins) lack liquid USDC-quote spot pairs on Bybit —
    they MUST keep the universal `{coin}USDT` route. This is the
    regression guard for the conservative whitelist design."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"LIT": _perp("LIT", mark="1.65", min_notional="1.0")},
    )
    snap.wallet.unified_coin_balances = {"LIT": Decimal("4.9")}
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260604T120000Z")
    sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT and a.product_id == "LITUSDT"
    ]
    assert len(sells) == 1
    assert sells[0].coin == "USDT"


def test_orphan_sell_quote_dispatch_table() -> None:
    """Unit-level: helper must dispatch `BTC`/`ETH`/`SOL` to USDC and
    everything else to USDT. Guards against accidental whitelist
    typo / case drift."""
    from agent.sandbox.execute import _orphan_sell_quote
    assert _orphan_sell_quote("BTC") == ("BTCUSDC", "USDC")
    assert _orphan_sell_quote("ETH") == ("ETHUSDC", "USDC")
    assert _orphan_sell_quote("SOL") == ("SOLUSDC", "USDC")
    assert _orphan_sell_quote("TON") == ("TONUSDT", "USDT")
    assert _orphan_sell_quote("LIT") == ("LITUSDT", "USDT")


def test_diff_orphan_spot_skipped_when_subscribe_also_planned() -> None:
    """If the LLM is subscribing more of the same coin this cycle,
    auto-sell must not fire — the subscribe path will consume the
    wallet balance via _ensure_fund_balance / Buy-credit logic."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        onchain_products=[_TON_PRODUCT],
    )
    snap.wallet.unified_coin_balances = {"TON": Decimal("5.0")}
    d = _decision_with_hedge(hedge_notional=-40.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260603T180000Z")
    ton_sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "TONUSDT"
        and a.side == "Sell"
    ]
    assert ton_sells == []


def test_diff_orphan_sell_respects_open_perp_short_coverage() -> None:
    """Regression for 2026-06-03: TON-style naked-short bug. When the
    wallet holds non-stable spot AND there's an open perp short on the
    same coin, only the EXCESS over the hedge should be sold — not the
    spot that's currently backing the short."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        # Held TON Earn 4.154 + open TON short 4.1 = balanced hedge.
        earn_positions=[_pos("OnChain", "8", "4.154", coin="TON")],
        perp_positions=[_short_pos("TON", size="4.1", position_value="8.2")],
    )
    # 3.34 TON sitting in UNIFIED on top of the hedged pair → excess.
    snap.wallet.unified_coin_balances = {"TON": Decimal("3.34")}
    # LLM keeps TON as-is (matching the existing position).
    d = _decision_with_hedge(hedge_notional=-8.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260603T180000Z")
    sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "TONUSDT"
        and a.side == "Sell"
    ]
    assert len(sells) == 1
    sold_qty = sells[0].amount
    # Excess long = (3.34 + 4.154) - 4.1 = 3.394; sellable capped at
    # wallet (3.34), then quantized down to qty_step. Must be <= 3.34
    # and > 3.0 (a quantization that kills most of it would indicate
    # the cap didn't trigger correctly).
    assert sold_qty <= Decimal("3.34")
    assert sold_qty > Decimal("3.0")


def test_diff_orphan_sell_skipped_when_perp_fully_covers_wallet() -> None:
    """When the perp short matches the long exposure exactly (no excess),
    orphan-sell must not fire — selling would reduce long below short
    and create a naked short."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        earn_positions=[_pos("OnChain", "8", "4.15", coin="TON")],
        perp_positions=[_short_pos("TON", size="4.1", position_value="8.2")],
    )
    # No spot in UNIFIED; everything else balanced.
    snap.wallet.unified_coin_balances = {"TON": Decimal("0")}
    d = _decision_with_hedge(hedge_notional=-8.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260603T180000Z")
    ton_sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "TONUSDT"
        and a.side == "Sell"
    ]
    assert ton_sells == []


def test_diff_closes_naked_perp_short_when_long_gone() -> None:
    """Recovery: TON perp short 4.1 with zero TON long → some CLOSE_PERP
    must fire. In this scenario `_hedge_diff_actions` already catches
    it (LLM dropped TON → target hedge = 0); the new
    `_close_naked_perp_actions` is defense-in-depth for cases where
    hedge_diff doesn't (e.g. mid-cycle long-side loss after subscribes
    + redeems net out below the perp). Asserting on the outcome (TON
    perp closes by ANY path) makes the test future-proof against
    refactors that move responsibility between the two functions."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
        earn_positions=[],
        perp_positions=[_short_pos("TON", size="4.1", position_value="8.2")],
    )
    snap.wallet.unified_coin_balances = {"TON": Decimal("0")}
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260603T180000Z")
    closes = [
        a for a in actions
        if a.kind == ActionKind.CLOSE_PERP and a.coin == "TON"
    ]
    assert len(closes) >= 1
    total_closed = sum((a.amount for a in closes), Decimal(0))
    # The whole 4.1 short should be closed (combined across any sources).
    assert total_closed >= Decimal("4.0")


def test_close_naked_perp_actions_unit_trim_unhedged_portion() -> None:
    """Unit test the safety-net function directly: when long_now <
    perp_short post-cycle, only the naked portion (short - long) gets
    a CLOSE_PERP, never the whole short. Bypasses diff_to_actions
    because the conditions where this fires (hedge_diff's rebalance
    threshold doesn't catch a small long-side gap) are tricky to set
    up at the integration level."""
    from agent.sandbox.execute import _close_naked_perp_actions
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="0.1")},
        # Earn 3 TON; perp short 5 TON (already on the book).
        earn_positions=[_pos("OnChain", "8", "3.0", coin="TON")],
        perp_positions=[_short_pos("TON", size="5.0", position_value="10.0")],
    )
    snap.wallet.unified_coin_balances = {"TON": Decimal("0")}
    # Empty plan — no subscribes / redeems / hedge actions planned this
    # cycle. Long = 3 (Earn), short = 5 (perp). Naked = 2.
    closes = _close_naked_perp_actions(
        snap,
        hedge_closes=[],
        hedge_opens=[],
        redeems=[],
        subscribes=[],
        snapshot_ts="20260603T180000Z",
        idx_offset=0,
    )
    assert len(closes) == 1
    c = closes[0]
    assert c.coin == "TON"
    assert c.product_id == "TONUSDT"
    assert c.amount == Decimal("2.000")
    assert "auto-close naked" in c.reason


def test_close_naked_perp_actions_unit_skip_when_balanced() -> None:
    """When long == short post-cycle, no auto-close fires."""
    from agent.sandbox.execute import _close_naked_perp_actions
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="0.1")},
        earn_positions=[_pos("OnChain", "8", "5.0", coin="TON")],
        perp_positions=[_short_pos("TON", size="5.0", position_value="10.0")],
    )
    snap.wallet.unified_coin_balances = {"TON": Decimal("0")}
    closes = _close_naked_perp_actions(
        snap, [], [], [], [], "20260603T180000Z", idx_offset=0
    )
    assert closes == []


def test_close_naked_perp_actions_unit_credits_planned_subscribe() -> None:
    """A planned SUBSCRIBE_EARN this cycle adds to long_now — its
    amount_native counts as backing for the perp short. Without this
    credit, auto-close would trigger pre-emptively and trim a perp
    that's about to be fully hedged a moment later."""
    from agent.sandbox.execute import _close_naked_perp_actions
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="0.1")},
        # Earn empty pre-cycle; will be subscribed via the planned action.
        earn_positions=[],
        perp_positions=[_short_pos("TON", size="4.0", position_value="8.0")],
    )
    snap.wallet.unified_coin_balances = {"TON": Decimal("0")}
    planned_subscribe = Action(
        kind=ActionKind.SUBSCRIBE_EARN,
        category="OnChain",
        product_id="8",
        coin="TON",
        amount=Decimal("8.0"),
        amount_native=Decimal("4.0"),
        order_link_id="x",
        reason="planned",
    )
    closes = _close_naked_perp_actions(
        snap, [], [], [], [planned_subscribe],
        "20260603T180000Z", idx_offset=0,
    )
    # Long post-cycle = 0 + 4.0 (subscribe) = 4.0; short = 4.0. Balanced.
    assert closes == []


def test_diff_orphan_spot_skipped_below_min_swap() -> None:
    """Dust-sized orphan (< MIN_SWAP_USDC) isn't worth a swap — Bybit
    spot fees would exceed the recovery."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"LIT": _perp("LIT", mark="0.50", min_notional="1.0")},
    )
    snap.wallet.unified_coin_balances = {"LIT": Decimal("2.0")}  # $1 USD
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260603T180000Z")
    sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT and a.side == "Sell"
        and a.product_id == "LITUSDT"
    ]
    assert sells == []


def test_diff_redeems_dropped_non_stable_even_when_perp_mark_missing() -> None:
    """Regression for 2026-06-03 live bug. A non-stable Earn position
    (LIT) held from a prior cycle; this cycle's snapshot has no
    perp_market[LIT] entry (Bybit fan-out budget exhausted, or coin
    fell out of ranked picks). Pre-fix: _amount_to_usd→0 → delta→0 →
    SKIP — naked spot exposure persists when the LLM drops the pick.
    Post-fix: defensive REDEEM uses native qty regardless of USD
    measurement; both legs close cleanly."""
    snap = _snapshot(
        total_equity_usd="100",
        # No perp_market entry for LIT — mark price unavailable.
        earn_positions=[_pos("FlexibleSaving", "1114", "4.9005", coin="LIT")],
    )
    # LLM dropped the LIT pick entirely (USD1 only).
    d = _decision(
        [
            _venue("cash_usdc", 0.5),
            _venue("bybit_flex", 0.5, [("1131", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260603T170000Z")
    redeems = [a for a in actions if a.kind == ActionKind.REDEEM_EARN]
    assert len(redeems) == 1
    r = redeems[0]
    assert r.product_id == "1114"
    assert r.coin == "LIT"
    # Native qty must be preserved so the executor can pass it to Bybit
    # — the USD path would have sent 0 and silently no-op'd.
    assert r.amount_native == Decimal("4.9005")


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


def test_diff_partial_redeem_non_stable_sets_native_qty() -> None:
    """Regression: reducing (not fully dropping) a non-stable OnChain
    stake must size the REDEEM in native coin units. Pre-fix the regular
    redeem branch left amount_native=None, so the executor sent the USD
    figure as the native qty and Bybit redeemed the wrong amount /
    rejected — the "exit TON didn't redeem from staking" desync."""
    snap = _snapshot(
        total_equity_usd="100",
        earn_positions=[_pos("OnChain", "8", "25", coin="TON")],
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    # 25 TON @ $2 = $50 current; venue 0.2 → $20 target → redeem $30 (60%).
    d = _decision(
        [
            _venue("cash_usdc", 0.8),
            _venue("bybit_onchain", 0.2, [("8", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    redeems = [a for a in actions if a.kind == ActionKind.REDEEM_EARN]
    assert len(redeems) == 1
    assert redeems[0].amount_native is not None
    assert redeems[0].amount_native == Decimal("15")  # 60% of 25 TON


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


def test_diff_lm_held_position_on_dropped_product_still_redeems() -> None:
    """(.66) A legacy LM position whose product was dropped from the snapshot
    choice set (the leveraged-product filter) must still REDEEM when the LLM
    omits it — NOT SKIP. Otherwise the held capital is stuck un-redeemable
    because the executor's product lookup returns None. Redemption only needs
    the positionId from lm_positions, so the catalog row is unnecessary."""
    snap = _snapshot(
        total_equity_usd="100",
        lm_products=[_lm_product("24")],  # product 29 NOT in the catalog
        lm_positions=[
            _lm_position(product_id="29", position_id="211940", principal_usd="10.78")
        ],
    )
    d = _decision([_venue("cash_usdc", 1.0)])  # LM omitted entirely
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    redeems = [a for a in actions if a.kind == ActionKind.REDEEM_LM]
    assert len(redeems) == 1
    assert redeems[0].position_id == "211940"
    assert redeems[0].product_id == "29"
    assert "dropped from choice set" in redeems[0].reason
    assert not [
        a for a in actions
        if a.kind == ActionKind.SKIP_OUT_OF_SCOPE and a.product_id == "29"
    ]


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
    """Default state: VAULT8004_ALPHA_EXEC_ENABLED unset → gate is False →
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
        a.category == "AlphaFarm" and "VAULT8004_ALPHA_EXEC_ENABLED" in a.reason
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
    from agent.bybit_oracle.bybit_client import EarnPosition

    client = AsyncMock()
    client.place_earn_order.return_value = EarnOrderResult(orderId="r-1")
    # OnChain redeem is per-position: the executor reads live positions
    # and redeems each by its redeemPositionId.
    client.get_earn_positions.return_value = [
        EarnPosition(
            productId="26", coin="USDC", amount="10", id="900",
            category="OnChain", status="Active",
        ),
    ]
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
    # ...and must carry the per-stake position id, else 180020.
    assert kwargs["redeem_position_id"] == "900"


@pytest.mark.asyncio
async def test_execute_redeem_polls_settlement_before_continue(
    tmp_path: Path,
) -> None:
    """A successful live REDEEM_EARN must poll the wallet for the freed
    coin before later actions consume it (redeem settlement barrier)."""
    client = AsyncMock()
    client.place_earn_order.return_value = EarnOrderResult(orderId="r-1")
    action = Action(
        kind=ActionKind.REDEEM_EARN,
        category="FlexibleSaving",
        product_id="1131",
        coin="USD1",
        amount=Decimal("50"),
        order_link_id="sandbox-test-redeem-poll",
        reason="redeem",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260607T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    client.poll_redemption_credited.assert_awaited_once()
    kwargs = client.poll_redemption_credited.await_args.kwargs
    assert kwargs["coin"] == "USD1"
    assert kwargs["min_credit"] == Decimal("50")


@pytest.mark.asyncio
async def test_execute_redeem_skips_poll_on_dry_run(tmp_path: Path) -> None:
    """Dry-run places no orders, so there's nothing to wait for — the
    settlement poll must not fire."""
    client = AsyncMock()
    client.place_earn_order.return_value = EarnOrderResult(orderId="r-1")
    action = Action(
        kind=ActionKind.REDEEM_EARN,
        category="FlexibleSaving",
        product_id="1131",
        coin="USD1",
        amount=Decimal("50"),
        order_link_id="sandbox-test-redeem-dry",
        reason="redeem",
    )
    await execute_actions(
        client, [action], snapshot_ts="20260607T120000Z",
        dry_run=True, executions_dir=tmp_path,
    )
    client.poll_redemption_credited.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_redeem_poll_timeout_continues(tmp_path: Path) -> None:
    """If the freed coin doesn't credit in time, the cycle proceeds
    (degrades to the old behavior) rather than raising."""
    client = AsyncMock()
    client.place_earn_order.return_value = EarnOrderResult(orderId="r-1")
    client.poll_redemption_credited.side_effect = TimeoutError("not credited")
    action = Action(
        kind=ActionKind.REDEEM_EARN,
        category="FlexibleSaving",
        product_id="1131",
        coin="USD1",
        amount=Decimal("50"),
        order_link_id="sandbox-test-redeem-timeout",
        reason="redeem",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260607T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    client.poll_redemption_credited.assert_awaited_once()


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
    # Two TON skips: the reopen (no perp_market entry to price qty) and the
    # `.3` guard on the OnChain subscribe — without a mark we can't size the
    # native stake units, so there's no funded spot path (would 180016).
    assert len(skips) == 2
    assert any("cannot price qty" in s.reason for s in skips)
    assert any("no funded spot path" in s.reason for s in skips)


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
    """Open hedge of $50 + auto-Buy swap for non-stable TON pick → the
    consolidated USDCUSDT swap (.06.03) covers BOTH perp margin and the
    {coin}USDT Buy demand in a single conversion. Pre-consolidation the
    hedge swap sized only $52.50 (margin only); Buy ran on starved
    UNIFIED USDT and 170131'd."""
    snap = _snapshot(
        total_equity_usd="100",
        usdt_available_usd="0",
        # USDC headroom so the `.2` pre-flight (which bounds the funding-swap
        # inflow by available USDC) keeps the pick — this test isolates the
        # swap SIZING, not the USDC-starvation skip path.
        liquid_usdc_usd="150",
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
    # perp margin 50 × 1.05 = 52.50; Buy demand for $50 TON pick =
    # 50.00 × 1.01 = 50.50 → shortfall 103.00. Sized to cover shortfall + the
    # `.2` spend reserve (max(1%·103, $0.20) = 1.03), grossed up for the 0.1%
    # fee: (103.00 + 1.03) / 0.999 = 104.134 → ROUND_UP 104.14.
    assert sw.amount == Decimal("104.14")


def test_hedge_swap_headroom_covers_dual_leg_after_fee() -> None:
    """Regression for the live 2026-06-08 BERA drop: a hedged non-stable pick
    needs USDT for BOTH legs (perp margin + the {coin}USDT Buy). The USDC→USDT
    funding swap must over-convert so the USDT NETTED after the ~0.1% spot fee
    + quantize still covers both — bare-shortfall sizing under-delivered and
    the tail Buy dropped on a sub-cent gap, stranding the pick in cash."""
    snap = _snapshot(
        total_equity_usd="178", usdt_available_usd="0",
        perp_market={"BERA": _perp("BERA", mark="2.0")},
    )
    opens = [Action(
        kind=ActionKind.OPEN_PERP_SHORT, category="Perp", product_id="BERAUSDT",
        coin="BERA", amount=Decimal("6.0"), order_link_id="lid", reason="",
        side="Sell",
    )]
    # perp notional 6×2=12, margin ×1.05=12.60; Buy demand 12.58 →
    # shortfall 25.18. Sized to cover shortfall + the `.2` spend reserve
    # (max(1%·25.18, $0.20) = 0.2518), grossed up for the 0.1% fee:
    # (25.18 + 0.2518) / 0.999 = 25.4573 → ROUND_UP 25.46.
    swaps = _swap_actions_for_hedges(
        snap, opens, [], "20260608T160000Z",
        idx_offset=0, extra_usdt_demand=Decimal("12.58"),
    )
    assert len(swaps) == 1
    qty = swaps[0].amount
    assert qty == Decimal("25.46")
    # USDT netted after the 0.1% taker fee covers the $25.18 dual-leg need
    # PLUS the spend reserve, so the `.2` pre-flight keeps the pick.
    assert qty * Decimal("0.999") >= Decimal("25.18") + Decimal("0.2518")


def test_diff_no_swap_when_usdt_already_sufficient() -> None:
    """Existing $60 USDT covers the buffered $52.50 requirement.
    Narrowed assertion (`.60`): only checks no `Sell USDCUSDT` shortfall
    swap; a `Buy USDCUSDT` excess sweep may emit on the residue."""
    snap = _snapshot(
        total_equity_usd="100",
        usdt_available_usd="60",
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Sell"
    ]
    assert sells == []


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
    sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Sell"
    ]
    # required = 30 * 1.05 = 31.5; available = 0 + 40 (closed) = 40 → no
    # hedge_swap Sell shortfall. `.60` excess sweep (Buy USDCUSDT) on the
    # ~$8.50 residue is allowed — that's the new sweep path; this test
    # only guards the close-credit logic in `_swap_actions_for_hedges`.
    assert sells == []


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
    """Combined USDT demand (perp margin + Buy) below the available
    liquid USDT by < MIN_SWAP_USDC → suppress the USDCUSDT swap. Bybit
    fees on a sub-dollar swap exceed the value of the operation."""
    snap = _snapshot(
        total_equity_usd="100",
        # perp $52.50 + Buy $50.50 = $103.00 demand; liquid USDT $102.80
        # → shortfall $0.20, below MIN_SWAP_USDC.
        usdt_available_usd="102.80",
        liquid_usdt_usd="102.80",
        perp_market={"TON": _perp("TON", mark="2.0")},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    hedge_swaps = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Sell"
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


# ─── USDT-excess sweep (.60) ───────────────────────────────────────────────


def _usdt_flex_product(product_id: str = "888", apr: str = "0.05") -> ProductSummary:
    """Flex USDT stable product — drives a USDT-denominated SUBSCRIBE_EARN
    so the sweep test can exercise the `usdt_subscribe_demand` term."""
    return ProductSummary(
        category="FlexibleSaving",
        product_id=product_id,
        coin="USDT",
        effective_apr=Decimal(apr),
        apr_source="apy_e8",
    )


def test_diff_usdt_excess_swept_to_usdc_when_no_demand() -> None:
    """Idle USDT with no consumers (perp margin, Buy demand, USDT
    subscribes all zero) → emit one `Buy USDCUSDT` sweep sized to
    `liquid_usdt_usd`. Canonical case: USDT FlexibleSaving payout sits
    on the sub-account between cycles."""
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdt_usd="50",
    )
    d = _decision([_venue("cash_usdc", 1.0)])
    actions = diff_to_actions(snap, d, snapshot_ts="20260604T120000Z")
    sweeps = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Buy"
    ]
    assert len(sweeps) == 1
    s = sweeps[0]
    assert s.coin == "USDC"
    # excess $50 − reserve max(1%, $0.20)=$0.50 = $49.50 (under-size so the
    # cleanup Buy can't over-reach dispatch-time UNIFIED USDT).
    assert s.amount == Decimal("49.50")
    # Sweep must sit after subscribes so USDT consumers fire first.
    kinds = [a.kind for a in actions]
    assert kinds[-1] == ActionKind.SWAP_SPOT


def test_diff_usdt_excess_no_sweep_when_hedge_consumes_supply() -> None:
    """Hedged non-stable pick funded almost entirely by the USDC→USDT swap.

    Post-`.2`: with $50 liquid USDT and no USDC the TON pick ($52.50 margin
    + $50.50 Buy = $103 demand) relies on the funding swap, whose 0.5%
    headroom can't clear the 1% spend reserve — so the pre-flight
    BINARY-skips the whole hedged pick (the exact partial-exec it exists to
    prevent): no perp open, no Buy, no subscribe survives, and the funding
    Sell is re-sized to 0. The now-idle $50 USDT has no consumer left, so
    the `.60` excess sweep correctly reclaims it back to USDC (one Buy)."""
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdt_usd="50",
        perp_market={"TON": _perp("TON", mark="2.0")},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260604T120000Z")
    # `.2` skipped the whole hedged TON pick: no perp open, no Buy demand,
    # no subscribe survives; the funding Sell was re-sized to 0.
    assert not [a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]
    assert not [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    sells = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Sell"
    ]
    assert sells == []
    assert any(
        a.kind == ActionKind.SKIP_OUT_OF_SCOPE
        and a.coin == "TON"
        and "170131 pre-flight" in a.reason
        for a in actions
    )
    # No Buy demand remains, so `.60` reclaims the idle USDT to USDC.
    buys = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Buy"
    ]
    assert len(buys) == 1
    assert buys[0].coin == "USDC"


def test_diff_usdt_excess_reduced_by_usdt_subscribe_demand() -> None:
    """Liquid USDT $70 - USDT-stable Flex subscribe $50 = $20 excess →
    sweep $20 − reserve $0.20 floor = $19.80. Verifies the
    `usdt_subscribe_demand` subtraction (and the small under-size reserve)."""
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdt_usd="70",
        flex_products=[_usdt_flex_product("888")],
    )
    # Wallet already has USDT for the subscribe → earn_swaps emits no
    # USDC→USDT Sell leg (would otherwise mask the sweep math).
    snap.wallet.unified_coin_balances = {"USDT": Decimal("70")}
    d = _decision([
        _venue("cash_usdc", 0.5),
        _venue("bybit_flex", 0.5, [("888", 1.0)]),
    ])
    actions = diff_to_actions(snap, d, snapshot_ts="20260604T120000Z")
    sweeps = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Buy"
    ]
    assert len(sweeps) == 1
    assert sweeps[0].amount == Decimal("19.80")


def test_diff_usdt_excess_below_threshold_suppressed() -> None:
    """Excess < MIN_SWAP_USDC ($5) → suppress sweep. Mirrors the Sell-side
    sub-threshold guard in `_swap_actions_for_hedges`; Bybit per-pair
    min-notional + fees make a sub-$5 round-trip uneconomic."""
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdt_usd="4.50",
    )
    d = _decision([_venue("cash_usdc", 1.0)])
    actions = diff_to_actions(snap, d, snapshot_ts="20260604T120000Z")
    sweeps = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Buy"
    ]
    assert sweeps == []


def test_diff_usdt_excess_credits_close_released_margin() -> None:
    """CLOSE_PERP releases USDT margin into the available pool → excess
    sweep includes it. Symmetric to `_swap_actions_for_hedges` which
    credits closes against required."""
    snap = _snapshot(
        total_equity_usd="200",
        liquid_usdt_usd="0",
        perp_market={"TON": _perp("TON", mark="2.0")},
        # Existing TON short to close; no new opens (decision is all cash).
        perp_positions=[_short_pos("TON", size="20", position_value="40.00")],
    )
    d = _decision([_venue("cash_usdc", 1.0)])
    actions = diff_to_actions(snap, d, snapshot_ts="20260604T120000Z")
    sweeps = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Buy"
    ]
    assert len(sweeps) == 1
    # close_notional = 20 × $2 = $40; no demand → excess $40 − reserve
    # max(1%, $0.20)=$0.40 = $39.60.
    assert sweeps[0].amount == Decimal("39.60")


def test_diff_usdt_excess_under_sizes_below_liquid() -> None:
    """Regression for the live 2026-06-08 executed_partial: the cleanup sweep
    sized to the snapshot's liquid_usdt ($13.11) over-reached dispatch-time
    UNIFIED ($13.01) and the Buy rejected. The swept qty must be strictly
    below liquid_usdt so settlement drift / FUND dust can't strand it."""
    snap = _snapshot(total_equity_usd="178", liquid_usdt_usd="13.11")
    d = _decision([_venue("cash_usdc", 1.0)])
    actions = diff_to_actions(snap, d, snapshot_ts="20260608T163359Z")
    sweeps = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Buy"
    ]
    assert len(sweeps) == 1
    # excess $13.11 − reserve max(1%=$0.13, $0.20)=$0.20 = $12.91 < $13.01.
    assert sweeps[0].amount == Decimal("12.91")
    assert sweeps[0].amount < Decimal("13.11")


def test_diff_usdc_subscribe_funded_from_idle_usdt() -> None:
    """`bybit-sandbox.68`: a USDC Earn subscribe on a USDT-funded wallet
    gets a `USDCUSDT` Buy emitted BEFORE the subscribe so the stake has
    USDC on hand. Pre-fix the only USDT→USDC conversion was the
    post-subscribe excess sweep — a cycle too late, so the subscribe
    180016'd (executed_partial). Mirrors the 2026-06-07 prod cycle:
    $6 liquid USDC, $100 idle USDT, ~$70 USDC OnChain subscribe."""
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdc_usd="6",
        liquid_usdt_usd="100",
    )
    snap.wallet.unified_coin_balances = {
        "USDC": Decimal("6"),
        "USDT": Decimal("100"),
    }
    d = _decision([
        _venue("cash_usdc", 0.3),
        _venue("bybit_flex", 0.7, [("1131", 1.0)]),  # USDC stable pick → $70
    ])
    actions = diff_to_actions(snap, d, snapshot_ts="20260607T120000Z")

    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    assert len(subs) == 1 and subs[0].coin == "USDC"
    sub_idx = actions.index(subs[0])

    # Funding Buy: shortfall $70 - $6 = $64, × 1.01 = $64.64.
    funding = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT" and a.side == "Buy"
    ]
    assert funding, "expected a USDT→USDC funding Buy"
    first_buy = funding[0]
    assert first_buy.amount == Decimal("64.64")
    # Ordered BEFORE the subscribe so the stake has USDC on hand.
    assert actions.index(first_buy) < sub_idx
    # USDC available at subscribe time covers the stake.
    pre_sub_in = sum(
        (a.amount for i, a in enumerate(actions)
         if i < sub_idx and a.kind == ActionKind.SWAP_SPOT
         and a.product_id == "USDCUSDT" and a.side == "Buy"),
        Decimal(0),
    )
    assert Decimal("6") + pre_sub_in >= subs[0].amount


def test_diff_usdc_subscribe_no_funding_when_usdc_on_hand() -> None:
    """USDC subscribe fully covered by liquid USDC → no funding Buy
    (regression guard: the `.68` path must not fire when USDC suffices)."""
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdc_usd="100",
        liquid_usdt_usd="0",
    )
    snap.wallet.unified_coin_balances = {"USDC": Decimal("100")}
    d = _decision([
        _venue("cash_usdc", 0.3),
        _venue("bybit_flex", 0.7, [("1131", 1.0)]),
    ])
    actions = diff_to_actions(snap, d, snapshot_ts="20260607T120000Z")
    buys = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT" and a.side == "Buy"
    ]
    assert buys == []


def test_diff_usdc_funding_and_sweep_have_unique_order_link_ids() -> None:
    """`bybit-sandbox.68`: a USDC-funding Buy (in `earn_swaps`) and the
    USDT-excess sweep coexist with NO hedge swap. The block-offset scheme
    reserves a hedge-swap slot before `earn_swaps`, but downstream offsets
    count the actual `len(hedge_swaps)==0` — so pre-fix both Buys landed on
    the same orderLinkId (retCode 170141 Duplicate clientOrderId). The
    final renumber must keep every id unique."""
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdc_usd="0.2",
        liquid_usdt_usd="44",
    )
    snap.wallet.unified_coin_balances = {
        "USDC": Decimal("0.2"),
        "USDT": Decimal("44"),
    }
    d = _decision([
        _venue("cash_usdc", 0.77),
        _venue("bybit_flex", 0.23, [("1131", 1.0)]),  # ~$23 USDC subscribe
    ])
    actions = diff_to_actions(snap, d, snapshot_ts="20260607T202447Z")

    usdcusdt_buys = [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT" and a.side == "Buy"
    ]
    assert len(usdcusdt_buys) >= 2, "expected a funding Buy AND a sweep Buy"
    olids = [a.order_link_id for a in actions]
    assert len(olids) == len(set(olids)), f"duplicate orderLinkId: {olids}"


def _redeem(category: str, coin: str, amount: str, pid: str = "26") -> Action:
    return Action(
        kind=ActionKind.REDEEM_EARN, category=category, product_id=pid,
        coin=coin, amount=Decimal(amount), order_link_id="r", reason="redeem",
    )


def _subscribe(category: str, coin: str, amount: str, pid: str = "1") -> Action:
    return Action(
        kind=ActionKind.SUBSCRIBE_EARN, category=category, product_id=pid,
        coin=coin, amount=Decimal(amount), order_link_id="s", reason="subscribe",
    )


def test_defer_subscribe_funded_by_slow_onchain_redeem() -> None:
    """`.63`: a USDC subscribe fundable only by a SLOW OnChain USDC redeem
    (won't credit this cycle) is deferred to SKIP — not emitted to 180016."""
    from agent.sandbox.execute import _defer_subscribes_awaiting_slow_redeem
    snap = _snapshot(total_equity_usd="100", liquid_usdc_usd="5")
    out, deferred = _defer_subscribes_awaiting_slow_redeem(
        snap,
        [_subscribe("FlexibleSaving", "USDC", "40")],
        [_redeem("OnChain", "USDC", "40")],
        [],
    )
    assert deferred == {"USDC"}
    assert out[0].kind == ActionKind.SKIP_OUT_OF_SCOPE
    assert "deferred" in out[0].reason


def test_no_defer_when_redeem_settles_in_cycle() -> None:
    """`.63`: a FlexibleSaving (fast, <1min) redeem funds the subscribe in
    cycle — no defer even with low liquid."""
    from agent.sandbox.execute import _defer_subscribes_awaiting_slow_redeem
    snap = _snapshot(total_equity_usd="100", liquid_usdc_usd="5")
    out, deferred = _defer_subscribes_awaiting_slow_redeem(
        snap,
        [_subscribe("OnChain", "USDC", "40")],
        [_redeem("FlexibleSaving", "USDC", "40")],
        [],
    )
    assert deferred == set()
    assert out[0].kind == ActionKind.SUBSCRIBE_EARN


def test_no_defer_when_liquid_covers_subscribe() -> None:
    """`.63`: even with a slow OnChain redeem pending, a subscribe covered
    by liquid alone proceeds — the guard fires only for the part actually
    waiting on the slow redeem."""
    from agent.sandbox.execute import _defer_subscribes_awaiting_slow_redeem
    snap = _snapshot(total_equity_usd="100", liquid_usdc_usd="50")
    out, deferred = _defer_subscribes_awaiting_slow_redeem(
        snap,
        [_subscribe("FlexibleSaving", "USDC", "40")],
        [_redeem("OnChain", "USDC", "40")],
        [],
    )
    assert deferred == set()
    assert out[0].kind == ActionKind.SUBSCRIBE_EARN


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
    assert kwargs["qty_base"] == "52.50"
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


# ─── .48 advance-Earn position dedup ──────────────────────────────────────


def test_diff_advance_earn_skips_when_dual_assets_position_held() -> None:
    """`.48`: re-subscribing on top of an open DualAssets position would
    silently open a second position (Bybit's orderLinkId dedup window is
    only ~30min; advance-Earn positions live until expiry). Diff must
    SKIP with a clear reason instead of emitting SUBSCRIBE_ADVANCE_EARN."""
    quote = _dual_quote(base="BTC", quote_coin="USDT")
    snap = _snapshot(
        total_equity_usd="200",
        advance_earn_quotes={"DualAssets/da-1": quote},
        advance_earn_positions={
            "DualAssets/da-1": [
                {
                    "positionId": "pos-1",
                    "amount": "40",
                    "strikePrice": "62000",
                    "status": "Subscribed",
                }
            ]
        },
        advance_products={
            "DualAssets": [_advance_product("DualAssets", "da-1", "BTC/USDT")]
        },
    )
    d = _advance_decision("bybit_dual_asset", "da-1", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert not [a for a in actions if a.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN]
    dedup = [
        a
        for a in actions
        if a.kind == ActionKind.SKIP_OUT_OF_SCOPE
        and a.category == "DualAssets"
        and a.product_id == "da-1"
    ]
    assert len(dedup) == 1
    assert "existing position" in dedup[0].reason
    assert "double-lock" in dedup[0].reason


def test_diff_advance_earn_skips_when_discount_buy_position_held() -> None:
    """Mirror of the DualAssets dedup for DiscountBuy."""
    quote = _discount_quote(coin="USDT", inst_uid="inst-xyz")
    snap = _snapshot(
        total_equity_usd="100",
        advance_earn_quotes={"DiscountBuy/db-7": quote},
        advance_earn_positions={
            "DiscountBuy/db-7": [
                {
                    "positionId": "pos-2",
                    "purchaseAmount": "20",
                    "status": "Active",
                }
            ]
        },
        advance_products={
            "DiscountBuy": [_advance_product("DiscountBuy", "db-7", "USDT")]
        },
    )
    d = _advance_decision("bybit_discount_buy", "db-7", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert not [a for a in actions if a.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN]
    dedup = [
        a
        for a in actions
        if a.kind == ActionKind.SKIP_OUT_OF_SCOPE
        and a.category == "DiscountBuy"
    ]
    assert len(dedup) == 1


def test_diff_advance_earn_subscribes_when_position_list_empty() -> None:
    """Explicit empty list (positions fetch ran, no positions held) →
    happy-path SUBSCRIBE. Distinguishes from missing-key semantics
    (product fell outside the snapshot's top-K window)."""
    quote = _dual_quote(base="BTC", quote_coin="USDT")
    snap = _snapshot(
        total_equity_usd="200",
        advance_earn_quotes={"DualAssets/da-1": quote},
        advance_earn_positions={"DualAssets/da-1": []},
        advance_products={
            "DualAssets": [_advance_product("DualAssets", "da-1", "BTC/USDT")]
        },
    )
    d = _advance_decision("bybit_dual_asset", "da-1", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN]
    assert len(subs) == 1


def test_diff_advance_earn_subscribes_when_only_terminal_positions() -> None:
    """A row with status=Settled is past lifecycle — Bybit's open-position
    endpoint shouldn't return it, but if it slips through we must NOT
    block re-subscribe. Belt-and-braces for shape drift."""
    quote = _dual_quote(base="BTC", quote_coin="USDT")
    snap = _snapshot(
        total_equity_usd="200",
        advance_earn_quotes={"DualAssets/da-1": quote},
        advance_earn_positions={
            "DualAssets/da-1": [
                {"positionId": "old", "amount": "40", "status": "Settled"}
            ]
        },
        advance_products={
            "DualAssets": [_advance_product("DualAssets", "da-1", "BTC/USDT")]
        },
    )
    d = _advance_decision("bybit_dual_asset", "da-1", weight=0.2)
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    subs = [a for a in actions if a.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN]
    assert len(subs) == 1


def test_advance_earn_positions_held_handles_missing_amount_fields() -> None:
    """Bybit per-category schemas vary — the helper must scan every
    known amount alias before deciding a row is empty."""
    from agent.sandbox.execute import _advance_earn_positions_held

    # quoteAmount instead of amount (DualAssets variant on some products)
    assert _advance_earn_positions_held(
        [{"positionId": "x", "quoteAmount": "12.5"}]
    ) == Decimal("12.5")
    # purchaseAmount (DiscountBuy variant)
    assert _advance_earn_positions_held(
        [{"positionId": "y", "purchaseAmount": "8"}]
    ) == Decimal("8")
    # No usable amount field → treated as not held
    assert _advance_earn_positions_held(
        [{"positionId": "z", "note": "nothing here"}]
    ) == Decimal(0)
    # None / empty
    assert _advance_earn_positions_held(None) == Decimal(0)
    assert _advance_earn_positions_held([]) == Decimal(0)


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
async def test_execute_onchain_subscribe_transfers_unified_to_fund_for_non_stable(
    tmp_path: Path,
) -> None:
    """Live SUBSCRIBE_EARN for OnChain non-stable triggers
    UNIFIED→FUND auto-transfer before place_earn_order. Without this,
    Buy spot deposits the coin in UNIFIED but OnChain Earn expects
    FUND → retCode=180016 and the paired perp short is orphaned.
    """
    client = AsyncMock()
    # FUND has 0 TON, UNIFIED has 7.6 TON transferable (from the upstream
    # Buy swap). Settle-poll: first FUND probe returns 0, post-transfer 8.
    # The UNIFIED source amount is read via get_account_coin_balance too
    # (transferBalance — what inter-transfer honors), not get_wallet_balance.
    fund_probe_results = iter([Decimal("0"), Decimal("8")])

    def _coin_balance(*, account_type: str, coin: str) -> Decimal:
        if account_type == "UNIFIED":
            return Decimal("7.6")
        return next(fund_probe_results, Decimal("8"))

    client.get_account_coin_balance.side_effect = _coin_balance
    from agent.bybit_oracle.bybit_client import EarnOrderResult
    client.place_earn_order.return_value = EarnOrderResult(
        orderId="earn-001", orderLinkId="sandbox-earn-001"
    )
    action = Action(
        kind=ActionKind.SUBSCRIBE_EARN,
        category="OnChain",
        product_id="8",
        coin="TON",
        amount=Decimal("15.07"),
        amount_native=Decimal("7.45"),
        order_link_id="sandbox-earn-001",
        reason="subscribe TON OnChain",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260603T180000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok", results[0].error
    # Auto-transfer must have fired with native qty + 0.5% headroom.
    client.internal_transfer.assert_awaited()
    xfer_kwargs = client.internal_transfer.await_args.kwargs
    assert xfer_kwargs["coin"] == "TON"
    assert xfer_kwargs["from_account_type"] == "UNIFIED"
    assert xfer_kwargs["to_account_type"] == "FUND"
    # Earn subscribe placed with native qty (not USD).
    earn_kwargs = client.place_earn_order.await_args.kwargs
    assert earn_kwargs["amount"] == "7.45"
    assert earn_kwargs["account_type"] == "FUND"
    assert earn_kwargs["coin"] == "TON"


@pytest.mark.asyncio
async def test_ensure_fund_balance_quantizes_usdt_to_2dp() -> None:
    """`bybit-sandbox.68`: USDT UNIFIED→FUND transfer must round to 2dp.
    A 6dp move like `44.574262` 131210's ("transfer amount scale more than
    accuracy length") for USDT, even though USDC settles fine at 6dp. The
    high-precision UNIFIED balance would otherwise win the min() and carry
    its 8dp scale into the transfer."""
    from agent.sandbox.execute import _ensure_fund_balance

    client = AsyncMock()
    fund_probe = iter([Decimal("0"), Decimal("44.57")])  # empty → settled
    # UNIFIED source carries the wallet's full 8dp precision (transferBalance
    # from get_account_coin_balance); it would win the min() and drag its
    # scale into the transfer unless re-quantized to the coin's accuracy.
    def _coin_balance(*, account_type: str, coin: str) -> Decimal:
        if account_type == "UNIFIED":
            return Decimal("100.12345678")
        return next(fund_probe, Decimal("44.57"))

    client.get_account_coin_balance.side_effect = _coin_balance
    await _ensure_fund_balance(client, "USDT", Decimal("44.3525"))

    client.internal_transfer.assert_awaited_once()
    amt = client.internal_transfer.await_args.kwargs["amount"]
    # gap 44.3525 × 1.005 = 44.5742… → 2dp ROUND_DOWN = 44.57 (≥ required).
    assert amt == "44.57"
    assert Decimal(amt) >= Decimal("44.3525")


@pytest.mark.asyncio
async def test_ensure_fund_balance_clamps_move_to_unified_transferable() -> None:
    """2026-06-08: the UNIFIED→FUND move must be sized from the UNIFIED
    TRANSFERABLE balance (transferBalance via get_account_coin_balance), not
    walletBalance/equity. The UTA reserves a haircut, so moving the
    equity-sized gap reverts 131212 "insufficient balance" (prod: UNIFIED
    USDT equity 18.78 but transfer movable lower → UNIFIED→FUND $12.47
    failed, OnChain USDT subscribe stranded). The move must never exceed
    transferable."""
    from agent.sandbox.execute import _ensure_fund_balance

    client = AsyncMock()
    # FUND empty pre-transfer, credited to 12 after. UNIFIED transferable is
    # exactly 12 — the move must clamp to it, NOT the buffered gap 12.06.
    fund_probe = iter([Decimal("0"), Decimal("12")])

    def _coin_balance(*, account_type: str, coin: str) -> Decimal:
        if account_type == "UNIFIED":
            return Decimal("12")
        return next(fund_probe, Decimal("12"))

    client.get_account_coin_balance.side_effect = _coin_balance
    await _ensure_fund_balance(client, "USDT", Decimal("12"))

    client.internal_transfer.assert_awaited_once()
    amt = Decimal(client.internal_transfer.await_args.kwargs["amount"])
    assert amt == Decimal("12.00")  # clamped to transferable, not 12.06


@pytest.mark.asyncio
async def test_get_account_coin_balance_prefers_transfer_balance() -> None:
    """`get_account_coin_balance` returns `transferBalance` (what
    inter-transfer honors), not `walletBalance` — the UTA reserves a haircut
    so walletBalance overstates the movable amount."""
    from agent.bybit_oracle.bybit_client import BybitClient

    # Drive the real method against a stubbed _request.
    inner = AsyncMock()
    inner._request.return_value = {
        "result": {"balance": {"walletBalance": "12.44", "transferBalance": "9.46"}}
    }
    got = await BybitClient.get_account_coin_balance(
        inner, account_type="UNIFIED", coin="USDT"
    )
    assert got == Decimal("9.46")


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


# ─── `.2` hedged-pick fully-fund-or-skip pre-flight ─────────────────────────


def _buy_action(coin: str, amount: str, idx: int = 0) -> Action:
    """A `{coin}USDT` Buy swap (non-stable Earn funding leg)."""
    return _spot_action(
        product_id=f"{coin}USDT", coin=coin, amount=amount, side="Buy", idx=idx
    )


def _subscribe_action(
    coin: str, amount: str, *, category: str = "OnChain", native: str | None = None
) -> Action:
    return Action(
        kind=ActionKind.SUBSCRIBE_EARN,
        category=category,
        product_id="8",
        coin=coin,
        amount=Decimal(amount),
        amount_native=Decimal(native) if native is not None else None,
        order_link_id="s-001",
        reason="test subscribe",
    )


def test_hedged_pick_underfunded_drops_when_supply_short() -> None:
    """UNIFIED USDT can't fund margin+Buy for the hedged pick after the
    spend reserve → the whole coin is returned (binary skip)."""
    # ID short 80 × $0.5 × 1.05 = $42 margin; Buy $40 → demand $82.
    # liquid_usdt $42 → reserved $41.58 < $42 margin → ID unfundable.
    snap = _snapshot(
        total_equity_usd="1000",
        liquid_usdt_usd="42",
        perp_market={"ID": _perp("ID", mark="0.5")},
    )
    out = _hedged_pick_underfunded_coins(
        snap,
        hedge_opens=[_open_short_action("ID", "80")],
        hedge_closes=[],
        hedge_swaps=[],
        earn_swaps=[_buy_action("ID", "40")],
    )
    assert out == {"ID"}


def test_hedged_pick_underfunded_cent_exact_drop() -> None:
    """Cent-exact boundary. Demand = $42 margin (80×0.5×1.05) + $40 Buy = $82;
    reserve is max(1%·82, $0.20) = $0.82 of that demand. $82.81 liquid reserves
    to $81.99 < $82 → drop; $82.82 reserves to exactly $82.00 ≥ $82 → keep."""
    snap_drop = _snapshot(
        total_equity_usd="1000",
        liquid_usdt_usd="82.81",
        perp_market={"ID": _perp("ID", mark="0.5")},
    )
    assert _hedged_pick_underfunded_coins(
        snap_drop,
        hedge_opens=[_open_short_action("ID", "80")],
        hedge_closes=[],
        hedge_swaps=[],
        earn_swaps=[_buy_action("ID", "40")],
    ) == {"ID"}
    snap_keep = _snapshot(
        total_equity_usd="1000",
        liquid_usdt_usd="82.82",
        perp_market={"ID": _perp("ID", mark="0.5")},
    )
    assert _hedged_pick_underfunded_coins(
        snap_keep,
        hedge_opens=[_open_short_action("ID", "80")],
        hedge_closes=[],
        hedge_swaps=[],
        earn_swaps=[_buy_action("ID", "40")],
    ) == set()


def test_hedged_pick_underfunded_keeps_when_comfortable() -> None:
    """Ample liquid USDT covers margin+Buy+reserve → keep (empty set)."""
    snap = _snapshot(
        total_equity_usd="1000",
        liquid_usdt_usd="500",
        perp_market={"ID": _perp("ID", mark="0.5")},
    )
    assert _hedged_pick_underfunded_coins(
        snap,
        hedge_opens=[_open_short_action("ID", "80")],
        hedge_closes=[],
        hedge_swaps=[],
        earn_swaps=[_buy_action("ID", "40")],
    ) == set()


def test_hedged_pick_underfunded_reserve_floor_on_tiny_book() -> None:
    """On a tiny book the $0.20 reserve FLOOR (not the 1%) is what binds.
    ID short 2 × $0.5 × 1.05 = $1.05 margin; Buy $1 → demand $2.05.
    liquid $2.20 → 1% reserve $0.022 < $0.20 floor → reserved $2.00 < $2.05
    → drop. liquid $2.26 → reserved $2.06 ≥ $2.05 → keep."""
    snap_drop = _snapshot(
        total_equity_usd="10",
        liquid_usdt_usd="2.20",
        perp_market={"ID": _perp("ID", mark="0.5")},
    )
    assert _hedged_pick_underfunded_coins(
        snap_drop,
        hedge_opens=[_open_short_action("ID", "2")],
        hedge_closes=[],
        hedge_swaps=[],
        earn_swaps=[_buy_action("ID", "1")],
    ) == {"ID"}
    snap_keep = _snapshot(
        total_equity_usd="10",
        liquid_usdt_usd="2.26",
        perp_market={"ID": _perp("ID", mark="0.5")},
    )
    assert _hedged_pick_underfunded_coins(
        snap_keep,
        hedge_opens=[_open_short_action("ID", "2")],
        hedge_closes=[],
        hedge_swaps=[],
        earn_swaps=[_buy_action("ID", "1")],
    ) == set()


def test_hedged_pick_underfunded_fee_haircut_forces_drop() -> None:
    """The ~0.1% fee haircut on the funding-swap inflow forces a cent-exact
    drop that the un-haircut supply would have kept. Supply is almost entirely
    the USDC→USDT hedge swap ($82.85 Sell), USDC-backed. Demand $82, reserve
    max(1%·82, $0.20) = $0.82. Post-haircut: 0.01 + 82.85×0.999 = $82.777 →
    reserved $81.957 < $82 → drop. Without the haircut: 0.01 + 82.85 = $82.86 →
    reserved $82.04 ≥ $82 → keep. The haircut is the deciding factor."""
    snap = _snapshot(
        total_equity_usd="1000",
        liquid_usdt_usd="0.01",  # non-zero so the pre-flight doesn't early-out
        liquid_usdc_usd="100",  # backs the $82.85 funding swap (else inflow→0)
        perp_market={"ID": _perp("ID", mark="0.5")},
    )
    hedge_swap = _spot_action(
        product_id="USDCUSDT", coin="USDT", amount="82.85", side="Sell"
    )
    assert _hedged_pick_underfunded_coins(
        snap,
        hedge_opens=[_open_short_action("ID", "80")],
        hedge_closes=[],
        hedge_swaps=[hedge_swap],
        earn_swaps=[_buy_action("ID", "40")],
    ) == {"ID"}


def test_hedged_pick_underfunded_missing_mark() -> None:
    """Missing / non-positive mark → unfundable (can't size the margin)."""
    snap = _snapshot(
        total_equity_usd="1000",
        liquid_usdt_usd="500",
        perp_market={},  # no ID entry
    )
    assert _hedged_pick_underfunded_coins(
        snap,
        hedge_opens=[_open_short_action("ID", "80")],
        hedge_closes=[],
        hedge_swaps=[],
        earn_swaps=[_buy_action("ID", "40")],
    ) == {"ID"}


def test_hedged_pick_underfunded_no_false_positive_stable_cash() -> None:
    """No OPEN_PERP_SHORT (stable-only / cash-only cycle) → empty set, even
    with idle USDT and a stable Sell swap present."""
    snap = _snapshot(total_equity_usd="1000", liquid_usdt_usd="5")
    assert _hedged_pick_underfunded_coins(
        snap,
        hedge_opens=[],
        hedge_closes=[],
        hedge_swaps=[],
        earn_swaps=[_spot_action(
            product_id="USDCUSDT", coin="USDT", amount="20", side="Sell"
        )],
    ) == set()


def test_hedged_pick_underfunded_multi_coin_priority() -> None:
    """Two hedged coins, priority-ordered: all margins reserved first, then
    Buys. A fits, B's Buy overflows → B dropped atomically, A kept.
    A: short 40 × $0.5 × 1.05 = $21 margin, Buy $20 → $41.
    B: short 40 × $0.5 × 1.05 = $21 margin, Buy $20 → $41. Total $82.
    liquid $63: reserved $62.37. Margins $21+$21=$42 fit (avail $20.37);
    A-Buy $20 fits (avail $0.37); B-Buy $20 overflows → B dropped."""
    snap = _snapshot(
        total_equity_usd="1000",
        liquid_usdt_usd="63",
        perp_market={"AAA": _perp("AAA", mark="0.5"), "BBB": _perp("BBB", mark="0.5")},
    )
    out = _hedged_pick_underfunded_coins(
        snap,
        hedge_opens=[
            _open_short_action("AAA", "40", idx=0),
            _open_short_action("BBB", "40", idx=1),
        ],
        hedge_closes=[],
        hedge_swaps=[],
        earn_swaps=[_buy_action("AAA", "20", idx=0), _buy_action("BBB", "20", idx=1)],
    )
    assert out == {"BBB"}


def test_diff_hedged_pick_underfunded_skipped_atomically() -> None:
    """Integration: a hedged pick that can't be fully funded is BINARY-skipped
    — no subscribe, no Buy, no perp survives, and the funding Sell is re-sized
    to 0. Here $50 liquid USDT + $0 USDC can't cover the $103 two-leg demand
    (the funding swap has no USDC to sell), so the whole TON pick goes to cash
    atomically instead of stranding a leg on retCode=170131."""
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdt_usd="50",
        perp_market={"TON": _perp("TON", mark="2.0")},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260608T120000Z")
    assert not [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    assert not [a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]
    # No funding Sell survives (re-sized to 0 after the cascade).
    assert not [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Sell"
    ]
    # No TON Buy swap survives either.
    assert not [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT and a.side == "Buy" and a.coin == "TON"
    ]
    skips = [
        a for a in actions
        if a.kind == ActionKind.SKIP_OUT_OF_SCOPE and a.coin == "TON"
    ]
    assert any("170131 pre-flight" in s.reason for s in skips)


def test_diff_hedged_pick_survives_when_usdc_backs_swap() -> None:
    """Epic-critical counter-case to the atomic skip: a hedged non-stable pick
    funded by the USDC→USDT swap MUST survive when USDC is ample — this is
    exactly the high-net BERA/ME pick the book exists to harvest. With $0
    liquid USDT but $150 USDC, the funding swap is sized to cover both legs +
    the `.2` spend reserve, so the pre-flight keeps the whole pick (subscribe +
    Buy + perp + funding Sell all present, no 170131 skip). Guards against the
    over-skip where a too-thin funding swap nuked every swap-funded hedged
    pick and locked the book into single-digit stables."""
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdt_usd="0",
        liquid_usdc_usd="150",
        perp_market={"TON": _perp("TON", mark="2.0")},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260608T120000Z")
    # The whole hedged pick survives — all three legs present, nothing skipped.
    assert [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    assert [a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]
    assert [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT
        and a.product_id == "USDCUSDT"
        and a.side == "Sell"
    ]
    assert [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT and a.side == "Buy" and a.coin == "TON"
    ]
    assert not [
        a for a in actions
        if a.kind == ActionKind.SKIP_OUT_OF_SCOPE
        and a.coin == "TON"
        and "170131 pre-flight" in a.reason
    ]


# ─── `.3` non-stable subscribe no-spot-path planner guard ───────────────────


def test_unfunded_nonstable_subscribe_no_spot_path() -> None:
    """Non-stable subscribe with no native balance and no emitted Buy →
    unfunded (would 180016)."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    out = _unfunded_nonstable_subscribe_coins(
        snap,
        subscribes=[_subscribe_action("TON", "50", native="25")],
        earn_swaps=[],
        redeems=[],
    )
    assert out == {"TON"}


def test_unfunded_nonstable_subscribe_funded_by_buy() -> None:
    """Emitted `{coin}USDT` Buy covers the subscribe → funded (not flagged)."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    out = _unfunded_nonstable_subscribe_coins(
        snap,
        subscribes=[_subscribe_action("TON", "50", native="25")],
        earn_swaps=[_buy_action("TON", "50")],
        redeems=[],
    )
    assert out == set()


def test_unfunded_nonstable_subscribe_funded_by_native_no_overskip() -> None:
    """Wallet already holds the coin; shortfall < MIN_SWAP so no Buy was
    emitted — must NOT be flagged (the native balance covers it)."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    # Subscribe $50; native 24 TON × $2 = $48; shortfall $2 < MIN_SWAP $5.
    snap.wallet.unified_coin_balances = {"TON": Decimal("24")}
    out = _unfunded_nonstable_subscribe_coins(
        snap,
        subscribes=[_subscribe_action("TON", "50", native="25")],
        earn_swaps=[],
        redeems=[],
    )
    assert out == set()


def test_unfunded_nonstable_subscribe_missing_mark() -> None:
    """No mark → can't size native units → unfunded."""
    snap = _snapshot(total_equity_usd="100", perp_market={})
    out = _unfunded_nonstable_subscribe_coins(
        snap,
        subscribes=[_subscribe_action("TON", "50")],
        earn_swaps=[],
        redeems=[],
    )
    assert out == {"TON"}


def test_unfunded_nonstable_subscribe_credits_fast_redeem() -> None:
    """A fast-settling same-coin redeem credits toward coverage; a slow
    OnChain redeem does NOT (mirrors `_redeem_settles_in_cycle`)."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    fast = Action(
        kind=ActionKind.REDEEM_EARN, category="FlexibleSaving", product_id="9",
        coin="TON", amount=Decimal("50"), order_link_id="r", reason="fast",
    )
    assert _unfunded_nonstable_subscribe_coins(
        snap, subscribes=[_subscribe_action("TON", "50", native="25")],
        earn_swaps=[], redeems=[fast],
    ) == set()
    slow = Action(
        kind=ActionKind.REDEEM_EARN, category="OnChain", product_id="8",
        coin="TON", amount=Decimal("50"), order_link_id="r", reason="slow",
    )
    assert _unfunded_nonstable_subscribe_coins(
        snap, subscribes=[_subscribe_action("TON", "50", native="25")],
        earn_swaps=[], redeems=[slow],
    ) == {"TON"}


def test_unfunded_nonstable_subscribe_ignores_stables() -> None:
    """USDC / stable subscribes are never flagged (no spot-coin path)."""
    snap = _snapshot(total_equity_usd="100", perp_market={})
    out = _unfunded_nonstable_subscribe_coins(
        snap,
        subscribes=[
            _subscribe_action("USDC", "50", category="FlexibleSaving"),
            _subscribe_action("USDT", "50", category="FlexibleSaving"),
        ],
        earn_swaps=[],
        redeems=[],
    )
    assert out == set()


def test_diff_nonstable_no_spot_path_cascades_perp_to_skip() -> None:
    """Integration: a non-stable subscribe with no mark (no spot path)
    cascades its paired perp to SKIP so the short isn't left naked. Uses a
    redeem-funded scenario so the subscribe itself is otherwise plannable
    but the mark is missing for the open coin."""
    # TON pick but perp_market lacks TON → no mark → `.3` flags it; the
    # paired perp can't open anyway, but the guard's reason must name it.
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdt_usd="200",
        perp_market={},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260608T130000Z")
    assert not [a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]
    assert not [a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]
    assert any(
        a.kind == ActionKind.SKIP_OUT_OF_SCOPE
        and a.coin == "TON"
        and "no funded spot path" in a.reason
        for a in actions
    )


def test_diff_nonstable_funded_by_buy_keeps_both() -> None:
    """A non-stable pick whose USDT→coin Buy fully funds it keeps both the
    subscribe and the paired perp (no `.3` over-skip)."""
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdc_usd="200",
        liquid_usdt_usd="200",
        perp_market={"TON": _perp("TON", mark="2.0")},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260608T140000Z")
    assert len([a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]) == 1
    assert len([a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]) == 1
    # The funding Buy for TON is present.
    assert [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT and a.side == "Buy" and a.coin == "TON"
    ]


def test_diff_nonstable_funded_by_native_keeps_both() -> None:
    """Wallet already holds the coin (shortfall < MIN_SWAP, no Buy emitted)
    → both subscribe and perp kept. Guards against `.3` over-skipping a
    self-funded pick."""
    snap = _snapshot(
        total_equity_usd="100",
        liquid_usdc_usd="200",
        liquid_usdt_usd="200",
        perp_market={"TON": _perp("TON", mark="2.0")},
        onchain_products=[_TON_PRODUCT],
    )
    # $50 TON pick; native 24 TON × $2 = $48 → shortfall $2 < MIN_SWAP $5,
    # so `_swap_actions_for_earn_picks` emits no Buy.
    snap.wallet.unified_coin_balances = {"TON": Decimal("24")}
    d = _decision_with_hedge(hedge_notional=-50.0)
    actions = diff_to_actions(snap, d, snapshot_ts="20260608T150000Z")
    assert len([a for a in actions if a.kind == ActionKind.SUBSCRIBE_EARN]) == 1
    assert len([a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]) == 1
    # No TON Buy was emitted (shortfall too small) and the pick was NOT
    # skipped for lack of a spot path.
    assert not [
        a for a in actions
        if a.kind == ActionKind.SWAP_SPOT and a.side == "Buy" and a.coin == "TON"
    ]
    assert not [
        a for a in actions
        if a.kind == ActionKind.SKIP_OUT_OF_SCOPE
        and a.coin == "TON"
        and "no funded spot path" in a.reason
    ]


# ─── Bybit-side stop-loss on perp open (2026-06-03) ────────────────────────


def test_invalidate_for_coin_returns_pick_thresholds() -> None:
    """Helper extracts invalidate_at from the matching non-stable Earn
    pick, or {} when none set."""
    from agent.reason.schema import InvalidateAt
    from agent.sandbox.execute import _invalidate_for_coin
    snap = _snapshot(
        total_equity_usd="100",
        onchain_products=[_TON_PRODUCT],
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="1.0")},
    )
    d = Decision(
        thesis="TON OnChain with operator stop levels.",
        venues=[
            _venue("cash_usdc", 0.5),
            VenueAllocation(
                venue_id="bybit_onchain",  # type: ignore[arg-type]
                weight=0.5,
                picks=[Pick(
                    product_id="8",
                    weight=1.0,
                    invalidate_at=InvalidateAt(
                        price_below=1.50, price_above=2.50,
                    ),
                )],
            ),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=10.0,
    )
    inv = _invalidate_for_coin(d, snap, "TON")
    assert inv["price_below"] == 1.50
    assert inv["price_above"] == 2.50


def test_diff_attaches_stop_levels_to_open_perp_short_from_invalidate() -> None:
    """When the LLM sets invalidate_at on the matching pick, the
    OPEN_PERP_SHORT action carries stop_loss / take_profit in `extra`
    so the executor mirrors them to Bybit via set_trading_stop."""
    from agent.reason.schema import InvalidateAt
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="0.1")},
        onchain_products=[_TON_PRODUCT],
    )
    d = Decision(
        thesis="TON OnChain hedged with operator stop levels.",
        venues=[
            _venue("cash_usdc", 0.5),
            VenueAllocation(
                venue_id="bybit_onchain",  # type: ignore[arg-type]
                weight=0.5,
                picks=[Pick(
                    product_id="8",
                    weight=1.0,
                    invalidate_at=InvalidateAt(
                        price_below=1.40, price_above=2.60,
                    ),
                )],
            ),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=10.0,
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260603T200000Z")
    opens = [a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]
    assert len(opens) == 1
    op = opens[0]
    assert op.coin == "TON"
    assert op.extra.get("stop_loss") == "2.6"  # price_above → SL on short
    assert op.extra.get("take_profit") == "1.4"  # price_below → TP on short
    assert "SL=$2.6" in op.reason
    assert "TP=$1.4" in op.reason


def test_diff_open_perp_short_no_extra_when_invalidate_unset() -> None:
    """When invalidate_at is None on the pick, no stop levels attach —
    backward-compat: action.extra stays empty {}."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0", min_notional="0.1")},
        onchain_products=[_TON_PRODUCT],
    )
    d = _decision_with_hedge(hedge_notional=-50.0)  # no invalidate_at
    actions = diff_to_actions(snap, d, snapshot_ts="20260603T200000Z")
    opens = [a for a in actions if a.kind == ActionKind.OPEN_PERP_SHORT]
    assert len(opens) == 1
    assert opens[0].extra == {}


@pytest.mark.asyncio
async def test_execute_open_perp_short_calls_set_trading_stop_when_extras_present(
    tmp_path: Path,
) -> None:
    """Live OPEN_PERP_SHORT with stop_loss/take_profit in `extra` →
    executor calls set_trading_stop on the matching symbol post-open."""
    from agent.bybit_oracle.bybit_client import SpotOrderResult as PerpOrderResult
    client = AsyncMock()
    client.set_leverage.return_value = None
    client.place_perp_order.return_value = PerpOrderResult(
        orderId="perp-001", orderLinkId="sandbox-h-001",
    )
    client.set_trading_stop.return_value = None
    action = Action(
        kind=ActionKind.OPEN_PERP_SHORT,
        category="Perp",
        product_id="TONUSDT",
        coin="TON",
        amount=Decimal("4.1"),
        order_link_id="sandbox-h-001",
        reason="short TON",
        extra={"stop_loss": "2.60", "take_profit": "1.40"},
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260603T200000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    client.set_trading_stop.assert_awaited_once()
    kwargs = client.set_trading_stop.await_args.kwargs
    assert kwargs["stop_loss"] == "2.60"
    assert kwargs["take_profit"] == "1.40"
    assert client.set_trading_stop.await_args.args[0] == "TONUSDT"


@pytest.mark.asyncio
async def test_execute_open_perp_short_skips_set_trading_stop_when_no_extras(
    tmp_path: Path,
) -> None:
    """Action without stop_loss/take_profit in extras → no set_trading_stop
    call. Backward-compat for picks that don't carry invalidate_at."""
    from agent.bybit_oracle.bybit_client import SpotOrderResult as PerpOrderResult
    client = AsyncMock()
    client.set_leverage.return_value = None
    client.place_perp_order.return_value = PerpOrderResult(
        orderId="perp-002", orderLinkId="sandbox-h-002",
    )
    client.set_trading_stop.return_value = None
    action = Action(
        kind=ActionKind.OPEN_PERP_SHORT,
        category="Perp", product_id="TONUSDT", coin="TON",
        amount=Decimal("4.1"),
        order_link_id="sandbox-h-002",
        reason="short TON",
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260603T200000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    client.set_trading_stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_open_perp_short_retries_set_trading_stop_once(
    tmp_path: Path, monkeypatch
) -> None:
    """Fix #8 (2026-06-04): a transient set_trading_stop failure (e.g.
    price-band drift right after the perp opens) gets a second attempt
    after a short backoff. Tests cover both branches: retry succeeds,
    retry exhausted (perp stays open under watcher fallback)."""
    from agent.bybit_oracle.bybit_client import SpotOrderResult as PerpOrderResult
    # Stub asyncio.sleep so the 1s backoff doesn't actually wait.
    import agent.sandbox.execute as exec_mod
    monkeypatch.setattr(exec_mod.asyncio, "sleep", AsyncMock(return_value=None))

    client = AsyncMock()
    client.set_leverage.return_value = None
    client.place_perp_order.return_value = PerpOrderResult(
        orderId="perp-101", orderLinkId="sandbox-h-101",
    )
    # First attempt raises, second succeeds.
    client.set_trading_stop = AsyncMock(
        side_effect=[
            BybitAPIError(110007, "Insufficient margin", "/v5/position/trading-stop"),
            None,
        ]
    )
    action = Action(
        kind=ActionKind.OPEN_PERP_SHORT,
        category="Perp", product_id="TONUSDT", coin="TON",
        amount=Decimal("4.1"),
        order_link_id="sandbox-h-101",
        reason="short TON",
        extra={"stop_loss": "2.60", "take_profit": "1.40"},
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260604T120000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    assert client.set_trading_stop.await_count == 2
    response = results[0].response or {}
    assert response.get("stop_loss") == "2.60"
    assert response.get("stop_loss_retry_succeeded") is True
    assert "stop_loss_error" not in response


@pytest.mark.asyncio
async def test_execute_open_perp_short_surfaces_sl_error_after_both_attempts(
    tmp_path: Path, monkeypatch
) -> None:
    """When both set_trading_stop attempts fail, the perp stays open
    (cancelling it would create a fresh exposure window) and the
    `stop_loss_error` + `stop_loss_retry_exhausted` keys surface so
    the operator sees the gap in the cycle log. Watcher remains the
    fallback safety net."""
    from agent.bybit_oracle.bybit_client import SpotOrderResult as PerpOrderResult
    import agent.sandbox.execute as exec_mod
    monkeypatch.setattr(exec_mod.asyncio, "sleep", AsyncMock(return_value=None))

    client = AsyncMock()
    client.set_leverage.return_value = None
    client.place_perp_order.return_value = PerpOrderResult(
        orderId="perp-102", orderLinkId="sandbox-h-102",
    )
    persistent_err = BybitAPIError(
        110007, "Insufficient margin", "/v5/position/trading-stop"
    )
    client.set_trading_stop = AsyncMock(side_effect=persistent_err)
    action = Action(
        kind=ActionKind.OPEN_PERP_SHORT,
        category="Perp", product_id="TONUSDT", coin="TON",
        amount=Decimal("4.1"),
        order_link_id="sandbox-h-102",
        reason="short TON",
        extra={"stop_loss": "2.60", "take_profit": "1.40"},
    )
    results = await execute_actions(
        client, [action], snapshot_ts="20260604T120001Z",
        dry_run=False, executions_dir=tmp_path,
    )
    # Perp open succeeded — SL failure must NOT fail the whole action.
    assert results[0].status == "ok"
    assert client.set_trading_stop.await_count == 2
    response = results[0].response or {}
    assert "110007" in response.get("stop_loss_error", "")
    assert response.get("stop_loss_retry_exhausted") is True


# ─── Atomic redeem+close-pair guard (2026-06-03) ───────────────────────────


@pytest.mark.asyncio
async def test_execute_atomic_redeem_failure_skips_paired_close_perp(
    tmp_path: Path,
) -> None:
    """Regression for 2026-06-03 naked-long bug. When REDEEM_EARN for
    TON fails (Bybit 180020 — Processing/locked), the subsequent
    CLOSE_PERP for TON must be skipped so the perp short keeps hedging
    the still-staked spot. Pre-fix the close ran → naked long $15."""
    from agent.bybit_oracle.bybit_client import BybitAPIError
    client = AsyncMock()
    # REDEEM_EARN raises BybitAPIError(180020) like live
    client.place_earn_order.side_effect = BybitAPIError(
        180020, "Position not found", "/v5/earn/place-order"
    )
    # CLOSE_PERP would succeed if it ran — we assert it DIDN'T
    client.place_perp_order.return_value = None
    actions = [
        Action(
            kind=ActionKind.REDEEM_EARN,
            category="OnChain",
            product_id="8",
            coin="TON",
            amount=Decimal("8.0"),
            amount_native=Decimal("4.0"),
            order_link_id="r-001",
            reason="redeem TON",
        ),
        Action(
            kind=ActionKind.CLOSE_PERP,
            category="linear",
            product_id="TONUSDT",
            coin="TON",
            amount=Decimal("4.0"),
            order_link_id="c-001",
            reason="close TON short",
        ),
    ]
    results = await execute_actions(
        client, actions, snapshot_ts="20260603T210000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert len(results) == 2
    # Redeem errored
    assert results[0].status == "error"
    assert "180020" in (results[0].error or "")
    # Close was SKIPPED, not executed
    assert results[1].status == "skipped"
    assert results[1].action.kind == ActionKind.SKIP_OUT_OF_SCOPE
    assert "paired REDEEM_EARN failed" in results[1].action.reason
    # Critically: place_perp_order must NOT have been awaited
    client.place_perp_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_redeem_success_lets_paired_close_perp_run(
    tmp_path: Path,
) -> None:
    """Counter-test: when REDEEM succeeds, paired CLOSE_PERP runs
    normally — no false-positive skipping."""
    from agent.bybit_oracle.bybit_client import (
        EarnOrderResult,
        EarnPosition,
        SpotOrderResult,
    )
    client = AsyncMock()
    client.place_earn_order.return_value = EarnOrderResult(
        orderId="earn-ok", orderLinkId="r-001"
    )
    client.get_earn_positions.return_value = [
        EarnPosition(
            productId="8", coin="TON", amount="4.0", id="700",
            category="OnChain", status="Active",
        ),
    ]
    client.place_perp_order.return_value = SpotOrderResult(
        orderId="perp-ok", orderLinkId="c-001"
    )
    actions = [
        Action(
            kind=ActionKind.REDEEM_EARN,
            category="OnChain", product_id="8", coin="TON",
            amount=Decimal("8.0"), amount_native=Decimal("4.0"),
            order_link_id="r-001", reason="redeem",
        ),
        Action(
            kind=ActionKind.CLOSE_PERP,
            category="linear", product_id="TONUSDT", coin="TON",
            amount=Decimal("4.0"),
            order_link_id="c-001", reason="close",
        ),
    ]
    results = await execute_actions(
        client, actions, snapshot_ts="20260603T210001Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    assert results[1].status == "ok"
    client.place_perp_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_atomic_guard_does_not_affect_other_coins(
    tmp_path: Path,
) -> None:
    """REDEEM_EARN failure on TON must NOT block CLOSE_PERP on a
    different coin (DOGE) in the same batch."""
    from agent.bybit_oracle.bybit_client import BybitAPIError, SpotOrderResult
    client = AsyncMock()
    client.place_earn_order.side_effect = BybitAPIError(
        180020, "Position not found", "/v5/earn/place-order"
    )
    client.place_perp_order.return_value = SpotOrderResult(
        orderId="perp-doge-ok", orderLinkId="c-002"
    )
    actions = [
        Action(
            kind=ActionKind.REDEEM_EARN,
            category="OnChain", product_id="8", coin="TON",
            amount=Decimal("8.0"), amount_native=Decimal("4.0"),
            order_link_id="r-001", reason="redeem TON",
        ),
        Action(
            kind=ActionKind.CLOSE_PERP,
            category="linear", product_id="DOGEUSDT", coin="DOGE",
            amount=Decimal("100.0"),
            order_link_id="c-002", reason="close DOGE",
        ),
    ]
    results = await execute_actions(
        client, actions, snapshot_ts="20260603T210002Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "error"
    assert results[1].status == "ok"  # DOGE close should run
    client.place_perp_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_subscribe_failure_skips_paired_open_perp_short(
    tmp_path: Path,
) -> None:
    """Symmetric to the REDEEM/CLOSE_PERP guard. When SUBSCRIBE_EARN
    fails (most commonly 180016 insufficient balance, or product full),
    the paired OPEN_PERP_SHORT must be skipped — otherwise the short
    opens without the backing earn leg → naked SHORT. Live risk: tried
    to scale into TON, balance shortfall hit subscribe, short opened
    on the assumption the spot would land → $X naked short until next
    cycle."""
    from agent.bybit_oracle.bybit_client import BybitAPIError
    client = AsyncMock()
    client.place_earn_order.side_effect = BybitAPIError(
        180016, "Insufficient balance", "/v5/earn/place-order"
    )
    client.place_perp_order.return_value = None
    actions = [
        Action(
            kind=ActionKind.SUBSCRIBE_EARN,
            category="OnChain",
            product_id="8",
            coin="TON",
            amount=Decimal("8.0"),
            amount_native=Decimal("4.0"),
            order_link_id="s-001",
            reason="subscribe TON",
        ),
        Action(
            kind=ActionKind.OPEN_PERP_SHORT,
            category="linear",
            product_id="TONUSDT",
            coin="TON",
            amount=Decimal("4.0"),
            order_link_id="o-001",
            reason="open TON short",
        ),
    ]
    results = await execute_actions(
        client, actions, snapshot_ts="20260604T100000Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert len(results) == 2
    assert results[0].status == "error"
    assert "180016" in (results[0].error or "")
    assert results[1].status == "skipped"
    assert results[1].action.kind == ActionKind.SKIP_OUT_OF_SCOPE
    assert "SUBSCRIBE_EARN failed" in results[1].action.reason
    client.place_perp_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_subscribe_success_lets_paired_open_perp_short_run(
    tmp_path: Path,
) -> None:
    """Counter-test: successful SUBSCRIBE_EARN does not block paired
    OPEN_PERP_SHORT."""
    from agent.bybit_oracle.bybit_client import EarnOrderResult, SpotOrderResult
    client = AsyncMock()
    client.place_earn_order.return_value = EarnOrderResult(
        orderId="earn-ok", orderLinkId="s-001"
    )
    client.place_perp_order.return_value = SpotOrderResult(
        orderId="perp-ok", orderLinkId="o-001"
    )
    actions = [
        Action(
            kind=ActionKind.SUBSCRIBE_EARN,
            category="OnChain", product_id="8", coin="TON",
            amount=Decimal("8.0"), amount_native=Decimal("4.0"),
            order_link_id="s-001", reason="subscribe",
        ),
        Action(
            kind=ActionKind.OPEN_PERP_SHORT,
            category="linear", product_id="TONUSDT", coin="TON",
            amount=Decimal("4.0"),
            order_link_id="o-001", reason="open short",
        ),
    ]
    results = await execute_actions(
        client, actions, snapshot_ts="20260604T100001Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "ok"
    assert results[1].status == "ok"
    client.place_perp_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_subscribe_guard_does_not_block_close_perp(
    tmp_path: Path,
) -> None:
    """SUBSCRIBE failure on TON must NOT block a CLOSE_PERP on TON —
    those are unrelated semantically (CLOSE_PERP unwinds a prior short,
    not the new subscribe). Only OPEN_PERP_SHORT pairs with SUBSCRIBE."""
    from agent.bybit_oracle.bybit_client import BybitAPIError, SpotOrderResult
    client = AsyncMock()
    client.place_earn_order.side_effect = BybitAPIError(
        180016, "Insufficient balance", "/v5/earn/place-order"
    )
    client.place_perp_order.return_value = SpotOrderResult(
        orderId="perp-close-ok", orderLinkId="c-001"
    )
    actions = [
        Action(
            kind=ActionKind.SUBSCRIBE_EARN,
            category="OnChain", product_id="8", coin="TON",
            amount=Decimal("8.0"), amount_native=Decimal("4.0"),
            order_link_id="s-001", reason="subscribe TON",
        ),
        Action(
            kind=ActionKind.CLOSE_PERP,
            category="linear", product_id="TONUSDT", coin="TON",
            amount=Decimal("4.0"),
            order_link_id="c-001", reason="close TON short (unrelated)",
        ),
    ]
    results = await execute_actions(
        client, actions, snapshot_ts="20260604T100002Z",
        dry_run=False, executions_dir=tmp_path,
    )
    assert results[0].status == "error"
    # CLOSE_PERP ran — not gated by subscribe guard.
    assert results[1].status == "ok"
    client.place_perp_order.assert_awaited_once()


def test_current_positions_sums_multiple_entries_per_pid() -> None:
    """Regression for 2026-06-03 endless-subscribe bug. When Bybit
    returns multiple earn_positions rows for the same (category, pid)
    (e.g. one settled + one Processing on a fresh subscribe), the diff
    layer must SUM them, not overwrite. Pre-fix only the last entry
    counted → diff saw target $15 vs current $5 ≪ target → emitted
    SUBSCRIBE_EARN every cycle → endless growth of Processing entries."""
    from agent.sandbox.execute import _current_positions_by_pid
    from agent.sandbox.snapshot import PerpInfo
    positions = [
        {"category": "OnChain", "productId": "8", "coin": "TON",
         "amount": "4.154", "status": ""},
        {"category": "OnChain", "productId": "8", "coin": "TON",
         "amount": "3.3823", "status": "Processing"},
        {"category": "OnChain", "productId": "8", "coin": "TON",
         "amount": "2.9871", "status": "Processing"},
    ]
    perp_market = {
        "TON": PerpInfo(
            symbol="TONUSDT",
            funding_rate_8h=Decimal("0.0001"),
            mark_price=Decimal("2.0"),
            orderbook_depth_50bps_usd=Decimal("100000"),
            min_order_qty=Decimal("0.1"),
            min_notional_usd=Decimal("0.5"),
            max_leverage=Decimal("50"),
        )
    }
    out = _current_positions_by_pid(positions, perp_market)
    assert ("OnChain", "8") in out
    pos = out[("OnChain", "8")]
    # Native sum: 4.154 + 3.3823 + 2.9871 = 10.5234
    assert pos.amount_native == Decimal("10.5234")
    # USD: 10.5234 * $2 = $21.0468
    assert pos.amount_usd == Decimal("21.0468")
    assert pos.coin == "TON"


# ─── .42 reconcile_executions ─────────────────────────────────────────────


def _write_exec_log(
    path: Path, rows: list[dict], trailing_garbage: str = ""
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r) for r in rows)
    if trailing_garbage:
        text = text + "\n" + trailing_garbage
    path.write_text(text + "\n")


def test_reconcile_executions_missing_file(tmp_path: Path) -> None:
    """No file on disk → empty summary, not an error. The startup scan
    treats this as 'nothing to reconcile' (cleanly absent)."""
    summary = reconcile_executions("20260604T100000Z", executions_dir=tmp_path)
    assert summary["exists"] is False
    assert summary["total"] == 0
    assert summary["counts"] == {}


def test_reconcile_executions_counts_statuses(tmp_path: Path) -> None:
    """Mixed status batch → per-status histogram + error list with kind
    + product_id + error_msg for non-ok rows."""
    ts = "20260604T120000Z"
    _write_exec_log(
        tmp_path / f"{ts}.jsonl",
        [
            {
                "action": {"kind": "subscribe_earn", "product_id": "p1"},
                "status": "ok", "error": None,
                "started_at": "2026-06-04T12:00:01Z",
                "finished_at": "2026-06-04T12:00:02Z",
            },
            {
                "action": {"kind": "swap_spot", "product_id": "ETHUSDC"},
                "status": "error",
                "error": "retCode=170131",
                "started_at": "2026-06-04T12:00:03Z",
                "finished_at": "2026-06-04T12:00:03Z",
            },
            {
                "action": {"kind": "open_perp_short", "product_id": "TONUSDT"},
                "status": "orphan",
                "error": "spot fill not confirmed",
                "started_at": "2026-06-04T12:00:04Z",
                "finished_at": "2026-06-04T12:00:05Z",
            },
            {
                "action": {"kind": "skip_out_of_scope", "product_id": "x"},
                "status": "skipped", "error": None,
                "started_at": "2026-06-04T12:00:06Z",
                "finished_at": "2026-06-04T12:00:06Z",
            },
        ],
    )
    summary = reconcile_executions(ts, executions_dir=tmp_path)
    assert summary["exists"] is True
    assert summary["total"] == 4
    assert summary["counts"] == {
        "ok": 1, "error": 1, "orphan": 1, "skipped": 1,
    }
    assert len(summary["errors"]) == 2
    kinds = {e["kind"] for e in summary["errors"]}
    assert kinds == {"swap_spot", "open_perp_short"}
    assert summary["last_finished_at"] == "2026-06-04T12:00:06Z"


def test_reconcile_executions_handles_malformed_trailing_line(tmp_path: Path) -> None:
    """OS-kill at the boundary often leaves a half-written final line.
    The summarizer must keep going and bucket it as `malformed`
    instead of raising — operator gets visibility of corruption count."""
    ts = "20260604T130000Z"
    _write_exec_log(
        tmp_path / f"{ts}.jsonl",
        [
            {"action": {"kind": "subscribe_earn", "product_id": "p1"},
             "status": "ok", "error": None},
        ],
        trailing_garbage='{"action": {"kind": "swap_spot"',  # cut off
    )
    summary = reconcile_executions(ts, executions_dir=tmp_path)
    assert summary["total"] == 1
    assert summary["counts"]["ok"] == 1
    assert summary["counts"]["malformed"] == 1


def test_reconcile_executions_errors_capped_at_ten(tmp_path: Path) -> None:
    """Avoid unbounded growth of the errors list — operator only needs
    the head for triage."""
    ts = "20260604T140000Z"
    rows = [
        {
            "action": {"kind": "subscribe_earn", "product_id": f"p{i}"},
            "status": "error", "error": f"err{i}",
        }
        for i in range(25)
    ]
    _write_exec_log(tmp_path / f"{ts}.jsonl", rows)
    summary = reconcile_executions(ts, executions_dir=tmp_path)
    assert summary["total"] == 25
    assert summary["counts"]["error"] == 25
    assert len(summary["errors"]) == 10


# ─── .59 verify_executions_against_bybit ──────────────────────────────────


def _landed(order_id: str = "o1") -> list[OrderHistoryEntry]:
    return [OrderHistoryEntry(orderId=order_id, orderStatus="Filled")]


@pytest.mark.asyncio
async def test_verify_executions_missing_file(tmp_path: Path) -> None:
    """No executions log → empty result, no Bybit calls. Mirrors the
    reconcile read-only contract."""
    client = AsyncMock()
    result = await verify_executions_against_bybit(
        "20260605T100000Z", client, executions_dir=tmp_path
    )
    assert result["exists"] is False
    assert result["checked"] == 0
    assert result["counts"] == {}
    client.get_order_history.assert_not_called()


@pytest.mark.asyncio
async def test_verify_executions_classifies_against_history(tmp_path: Path) -> None:
    """The four confirmable outcomes + the unconfirmable bucket. The
    `open_perp_short` row logged `error` but its order DID land on Bybit —
    that's the double-spend trap a naive retry would spring, so it must
    classify `confirmed-landed`, not `no-trace`."""
    ts = "20260605T110000Z"
    _write_exec_log(
        tmp_path / f"{ts}.jsonl",
        [
            {"action": {"kind": "swap_spot", "product_id": "USDCUSDT",
                        "order_link_id": "olid-0"}, "status": "ok"},
            {"action": {"kind": "open_perp_short", "product_id": "TONUSDT",
                        "order_link_id": "olid-1"}, "status": "error"},
            {"action": {"kind": "close_perp", "product_id": "ETHUSDT",
                        "order_link_id": "olid-2"}, "status": "error"},
            {"action": {"kind": "swap_spot", "product_id": "USDCUSD1",
                        "order_link_id": "olid-3"}, "status": "ok"},
            {"action": {"kind": "subscribe_earn", "product_id": "1131",
                        "order_link_id": "olid-4"}, "status": "ok"},
        ],
    )

    landed_ids = {"olid-0", "olid-1"}
    calls: list[tuple[str, str, str | None]] = []

    def _history(*, category: str, order_link_id: str, symbol: str | None):
        calls.append((category, order_link_id, symbol))
        return _landed() if order_link_id in landed_ids else []

    client = AsyncMock()
    client.get_order_history.side_effect = _history

    result = await verify_executions_against_bybit(
        ts, client, executions_dir=tmp_path
    )

    assert result["checked"] == 4
    assert result["unconfirmable"] == 1
    assert result["counts"] == {
        "confirmed-landed": 2,  # olid-0 (ok), olid-1 (logged error but landed)
        "no-trace": 1,          # olid-2 (error, no Bybit trace)
        "desync": 1,            # olid-3 (logged ok, no Bybit trace)
    }
    # Earn was never queried — no order-history endpoint for it.
    assert "olid-4" not in {c[1] for c in calls}
    # Category routing: spot for swaps, linear for perps.
    by_id = {c[1]: c[0] for c in calls}
    assert by_id["olid-0"] == "spot"
    assert by_id["olid-1"] == "linear"
    assert by_id["olid-2"] == "linear"


@pytest.mark.asyncio
async def test_verify_executions_query_error_does_not_abort(tmp_path: Path) -> None:
    """A transient history-lookup failure on one row is recorded as
    `query-error` and the scan keeps going — one bad lookup can't blind
    the whole reconcile."""
    ts = "20260605T120000Z"
    _write_exec_log(
        tmp_path / f"{ts}.jsonl",
        [
            {"action": {"kind": "swap_spot", "product_id": "USDCUSDT",
                        "order_link_id": "olid-0"}, "status": "ok"},
            {"action": {"kind": "close_perp", "product_id": "ETHUSDT",
                        "order_link_id": "olid-1"}, "status": "error"},
        ],
    )

    def _history(*, category: str, order_link_id: str, symbol: str | None):
        if order_link_id == "olid-0":
            raise BybitAPIError(10001, "rate limited", "/v5/order/history")
        return []

    client = AsyncMock()
    client.get_order_history.side_effect = _history

    result = await verify_executions_against_bybit(
        ts, client, executions_dir=tmp_path
    )

    assert result["checked"] == 2
    assert result["counts"]["query-error"] == 1
    assert result["counts"]["no-trace"] == 1


def test_hedge_kept_while_onchain_redeem_unbonding() -> None:
    """When the LLM drops a non-stable OnChain pick but the coin is STILL
    held (redeem placed but unbonding/settling, Bybit keeps the row), the
    perp hedge must NOT close — the in-flight redeem would otherwise be a
    naked directional long for the whole settlement window."""
    snap = _snapshot(
        total_equity_usd="100",
        earn_positions=[_pos("OnChain", "8", "4.154", coin="TON")],
        perp_positions=[_short_pos("TON", size="4.1", position_value="8.2")],
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    d = _decision([_venue("cash_usdc", 1.0)])  # TON pick dropped
    actions = diff_to_actions(snap, d, snapshot_ts="20260607T120000Z")
    closes = [
        a for a in actions
        if a.kind == ActionKind.CLOSE_PERP and a.coin == "TON"
    ]
    assert closes == []  # hedge kept while TON still held / unbonding


def test_hedge_closes_once_onchain_position_cleared() -> None:
    """Counter-test: TON Earn position gone (redeem settled) + open short
    + dropped pick → the hedge closes."""
    snap = _snapshot(
        total_equity_usd="100",
        earn_positions=[],  # TON cleared
        perp_positions=[_short_pos("TON", size="4.1", position_value="8.2")],
        perp_market={"TON": _perp("TON", mark="2.0")},
    )
    d = _decision([_venue("cash_usdc", 1.0)])
    actions = diff_to_actions(snap, d, snapshot_ts="20260607T120000Z")
    closes = [
        a for a in actions
        if a.kind == ActionKind.CLOSE_PERP and a.coin == "TON"
    ]
    assert len(closes) >= 1  # hedge closes now that underlying is gone


# ─── Stable consolidation (idle non-core stable → USDC) ─────────────────────


def test_stable_consolidate_emits_usd1_sell() -> None:
    """Live symptom (2026-06-08): ~$42 USD1 idle in the wallet, invisible to
    the USDC+USDT liquid budget. The consolidation must emit exactly one
    SWAP_SPOT Sell on USD1USDT (the real Bybit pair; USD1USDC does not
    exist) → USDT, and never touch the core stables."""
    snap = _snapshot(total_equity_usd="178")
    snap.wallet.unified_coin_balances = {
        "USD1": Decimal("41.90"),
        "USDC": Decimal("17.75"),  # core stable — must NOT be swept
        "USDT": Decimal("1.57"),   # core stable — must NOT be swept
    }
    actions = _stable_consolidate_actions(
        snap, "20260608T150000Z", idx_offset=700
    )
    assert len(actions) == 1, [a.product_id for a in actions]
    a = actions[0]
    assert a.kind == ActionKind.SWAP_SPOT
    assert a.side == "Sell"
    assert a.product_id == "USD1USDT"
    assert a.coin == "USDT"
    assert a.amount == Decimal("41.90")
    assert a.extra.get("skip_fund_transfer") is True
    assert "consolidate" in a.reason.lower()


def test_stable_consolidate_floors_each_account_to_avoid_oversell() -> None:
    """Regression for the live 2026-06-08 reject: UNIFIED 41.8966 + a sub-lot
    FUND dust (0.008, can't transfer) must NOT size the Sell to 41.90 (more
    than UNIFIED holds). Each account is floored to the 0.01 lot first →
    41.89 + 0.00 = 41.89, safely ≤ the tradable balance."""
    snap = _snapshot(total_equity_usd="178")
    snap.wallet.unified_coin_balances = {"USD1": Decimal("41.896606")}
    snap.wallet.fund_coin_balances = {"USD1": Decimal("0.008042")}
    actions = _stable_consolidate_actions(snap, "20260608T150000Z", idx_offset=700)
    assert len(actions) == 1
    assert actions[0].amount == Decimal("41.89")


def test_stable_consolidate_skips_dust_below_min() -> None:
    """A USD1 balance under MIN_SWAP_USDC ($5) is fee-dominated dust — skip."""
    snap = _snapshot(total_equity_usd="100")
    snap.wallet.unified_coin_balances = {"USD1": Decimal("3.00")}
    assert _stable_consolidate_actions(snap, "20260608T150000Z", idx_offset=700) == []


def test_stable_consolidate_merges_fund_balance() -> None:
    """USD1 split across UNIFIED + FUND is summed (FUND→UNIFIED transfer
    happens at dispatch via _ensure_unified_balance)."""
    snap = _snapshot(total_equity_usd="100")
    snap.wallet.unified_coin_balances = {"USD1": Decimal("20.00")}
    snap.wallet.fund_coin_balances = {"USD1": Decimal("22.50")}
    actions = _stable_consolidate_actions(snap, "20260608T150000Z", idx_offset=700)
    assert len(actions) == 1
    assert actions[0].amount == Decimal("42.50")


def test_stable_consolidate_skips_unsupported_noncore_stable() -> None:
    """A non-core stable with no confirmed {coin}USDC pair (not in the
    allowlist) is left idle, NOT emitted as an unlisted symbol that would
    reject at dispatch."""
    snap = _snapshot(total_equity_usd="100")
    snap.wallet.unified_coin_balances = {"FDUSD": Decimal("30.00")}
    assert _stable_consolidate_actions(snap, "20260608T150000Z", idx_offset=700) == []


def _consolidate_action() -> Action:
    return Action(
        kind=ActionKind.SWAP_SPOT,
        category="Spot",
        product_id="USD1USDT",
        coin="USDT",
        amount=Decimal("41.89"),
        order_link_id="lid_consolidate",
        reason="consolidate idle 41.89 USD1 → USDT",
        side="Sell",
        extra={"skip_fund_transfer": True},
    )


@pytest.mark.asyncio
async def test_dispatch_skip_fund_transfer_forces_the_sell(monkeypatch) -> None:
    """With skip_fund_transfer set, the disposal Sell must NOT consult the
    transfer-satisfies optimization (which is for ACQUIRING a target) — even
    when FUND holds plenty of the destination coin. Otherwise the sell would
    no-op and the USD1 stays stranded — the exact bug we are fixing."""
    import agent.sandbox.execute as ex

    called = {"transfer_satisfies": False}

    async def _spy_transfer(client, target_coin, required):  # noqa: ANN001
        called["transfer_satisfies"] = True
        return True  # would short-circuit the sell if consulted

    async def _noop_ensure(client, coin, required):  # noqa: ANN001
        return None

    monkeypatch.setattr(ex, "_transfer_satisfies_swap", _spy_transfer)
    monkeypatch.setattr(ex, "_ensure_unified_balance", _noop_ensure)

    client = AsyncMock()
    client.place_spot_order = AsyncMock(
        return_value=SimpleNamespace(orderId="SELL1")
    )
    res = await _execute_one(client, _consolidate_action(), dry_run=False)

    assert called["transfer_satisfies"] is False  # bypassed
    client.place_spot_order.assert_awaited_once()
    kwargs = client.place_spot_order.await_args.kwargs
    assert kwargs["symbol"] == "USD1USDT"
    assert kwargs["side"] == "Sell"
    assert res.status == "ok"


@pytest.mark.asyncio
async def test_dispatch_default_sell_still_consults_transfer(monkeypatch) -> None:
    """Contrast: a normal SWAP_SPOT Sell (no skip flag) keeps the
    transfer-satisfies optimization — proving the guard is opt-in and the
    legacy funding-swap path is untouched."""
    import agent.sandbox.execute as ex

    called = {"transfer_satisfies": False}

    async def _spy_transfer(client, target_coin, required):  # noqa: ANN001
        called["transfer_satisfies"] = True
        return True  # satisfied → sell short-circuited

    monkeypatch.setattr(ex, "_transfer_satisfies_swap", _spy_transfer)

    client = AsyncMock()
    client.place_spot_order = AsyncMock(
        return_value=SimpleNamespace(orderId="SELL2")
    )
    action = _consolidate_action()
    action.extra = {}  # no skip flag → legacy behavior
    res = await _execute_one(client, action, dry_run=False)

    assert called["transfer_satisfies"] is True
    client.place_spot_order.assert_not_awaited()  # transfer satisfied the swap
    assert res.status == "ok"
