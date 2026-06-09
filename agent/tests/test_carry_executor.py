"""Funding-carry executor tests (`bybit-strategy-expansion.5`).

Covers state-file round-trip, the diff layer (OPEN / CLOSE / no-op
branches), and the dispatch sequence (atomic-pair guard + paired
notional check). State + diff tests live first because they don't
need a mocked client; dispatch tests appear at the end of the file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.bybit_oracle.bybit_client import (
    BybitAPIError,
    SpotOrderResult,
    SpotOrderStatus,
)
from agent.reason.schema import Decision, Pick, VenueAllocation
from agent.sandbox.carry_state import (
    CarryPositionRecord,
    CarryState,
    read_carry_state,
    write_carry_state,
)
from agent.bybit_oracle.bybit_client import PerpPosition
from agent.sandbox.execute import (
    Action,
    ActionKind,
    ActionResult,
    _CARRY_OPEN_USDT_FACTOR,
    _carry_liq_close_actions,
    _carry_open_usdc_reserve,
    _confirmable_order_links,
    _funding_carry_diff,
    _funding_carry_targets,
    _execute_one,
    _hedge_diff_actions,
    apply_carry_results_to_state,
    diff_to_actions,
    verify_order_links,
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


def _peg(deviation_bps: float | None = -3.0) -> UsdcPegSnapshot:
    return UsdcPegSnapshot(
        price_usd=Decimal("1.0") if deviation_bps is None else None,
        deviation_bps=None if deviation_bps is None else Decimal(str(deviation_bps)),
        fetched_at=datetime.now(UTC),
    )


def _carry_product(coin: str = "TON") -> ProductSummary:
    return ProductSummary(
        category="FundingCarry",
        product_id=f"{coin}USDT",
        coin=coin,
        effective_apr=Decimal("0.20"),
        apr_source="funding_carry",
        base_apr_string="0.218",
        redeem_lockup_minutes=0,
        notes=[],
    )


def _perp(
    coin: str,
    *,
    mark: str = "2.0",
    qty_step: str = "0.001",
    min_order_qty: str = "0.1",
    funding_7d: str = "0.0001",
) -> PerpInfo:
    return PerpInfo(
        symbol=f"{coin}USDT",
        funding_rate_8h=Decimal(funding_7d),
        funding_rate_7d_avg=Decimal(funding_7d),
        funding_interval_hours=Decimal("8"),
        mark_price=Decimal(mark),
        orderbook_depth_50bps_usd=Decimal("1000000"),
        min_order_qty=Decimal(min_order_qty),
        min_notional_usd=Decimal("0.5"),
        qty_step=Decimal(qty_step),
        max_leverage=Decimal("10"),
    )


def _carry_snapshot(
    *,
    carry_products: list[ProductSummary] | None = None,
    perp_market: dict[str, PerpInfo] | None = None,
    total_equity_usd: str = "1000",
) -> Snapshot:
    # Explicit None checks so callers can pass empty dicts/lists to
    # exercise "missing" branches (perp_market={} is structurally
    # different from `None`).
    if carry_products is None:
        carry_products = [_carry_product("TON")]
    if perp_market is None:
        perp_market = {"TON": _perp("TON")}
    return Snapshot(
        captured_at=datetime.now(UTC),
        wallet=WalletSnapshot(
            total_equity_usd=Decimal(total_equity_usd),
            liquid_usdc_usd=Decimal("500"),
            liquid_usdt_usd=Decimal("500"),
        ),
        products={"FundingCarry": carry_products},
        market=MarketSnapshot(),
        perp_market=perp_market,
        usdc_peg=_peg(),
        errors=[],
    )


def _carry_decision(
    carry_weight: float = 0.1,
    picks: list[tuple[str, float]] | None = None,
) -> Decision:
    return Decision(
        thesis="Funding-carry on TON for diff testing.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=1.0 - carry_weight, picks=[]),
            VenueAllocation(
                venue_id="bybit_funding_carry",
                weight=carry_weight,
                picks=[
                    Pick(product_id=pid, weight=w)
                    for pid, w in (picks or [("TONUSDT", 1.0)])
                ],
            ),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=10.0,
    )


def _record(coin: str = "TON", target_usd: str = "100") -> CarryPositionRecord:
    return CarryPositionRecord(
        coin=coin,
        opened_at=datetime.now(UTC),
        target_pick_usd=Decimal(target_usd),
        spot_qty_base=Decimal(target_usd) / Decimal("2.0"),
        perp_qty_base=Decimal(target_usd) / Decimal("2.0"),
        mark_price_at_open=Decimal("2.0"),
        spot_order_link_id="abc_spot",
        perp_order_link_id="abc_perp",
    )


# ─── State file ─────────────────────────────────────────────────────────────


def test_carry_state_roundtrip(tmp_path: Path) -> None:
    s = CarryState(positions=[_record("TON"), _record("SOL", "50")])
    p = tmp_path / "carry.json"
    write_carry_state(s, p)
    loaded = read_carry_state(p)
    assert loaded.active_coins() == {"TON", "SOL"}
    ton = loaded.get("TON")
    assert ton is not None
    assert ton.target_pick_usd == Decimal("100")


def test_read_carry_state_missing_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "missing.json"
    s = read_carry_state(p)
    assert s.active_coins() == set()


def test_read_carry_state_corrupt_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "carry.json"
    p.write_text("{ not valid json")
    s = read_carry_state(p)
    assert s.active_coins() == set()


def test_carry_state_upsert_replaces_existing_record() -> None:
    s = CarryState(positions=[_record("TON", "100")])
    s2 = s.upsert(_record("TON", "200"))
    assert len(s2.positions) == 1
    assert s2.positions[0].target_pick_usd == Decimal("200")


def test_carry_state_remove_drops_coin() -> None:
    s = CarryState(positions=[_record("TON"), _record("SOL")])
    s2 = s.remove("TON")
    assert s2.active_coins() == {"SOL"}


def test_carry_state_active_coins_normalizes_case() -> None:
    s = CarryState(positions=[_record("ton")])
    assert s.active_coins() == {"TON"}


# ─── _funding_carry_targets ─────────────────────────────────────────────────


def test_carry_targets_derives_pick_usd_from_book() -> None:
    snap = _carry_snapshot(total_equity_usd="1000")
    d = _carry_decision(carry_weight=0.1)
    # pick_usd = book × venue × pick = 1000 × 0.1 × 1.0 = 100
    assert _funding_carry_targets(d, snap, Decimal("1000")) == {
        "TON": Decimal("100")
    }


def test_carry_targets_empty_when_venue_absent() -> None:
    snap = _carry_snapshot()
    d = Decision(
        thesis="No carry venue picked this cycle; all-cash baseline for diff testing.",
        venues=[VenueAllocation(venue_id="cash_usdc", weight=1.0)],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=0.0,
    )
    assert _funding_carry_targets(d, snap, Decimal("1000")) == {}


def test_carry_targets_skips_pick_when_product_missing_in_snapshot() -> None:
    """If the LLM hallucinates a product_id not in the snapshot's
    FundingCarry list, the target for that pick gets dropped — the
    `check_product_ids_in_snapshot` validator will have already
    rejected this scenario, but the diff is defensive."""
    snap = _carry_snapshot(carry_products=[_carry_product("TON")])
    d = _carry_decision(picks=[("HALLU_USDT", 1.0)])
    assert _funding_carry_targets(d, snap, Decimal("1000")) == {}


# ─── _carry_open_usdc_reserve (ah.6 / dispatch-1) ───────────────────────────


def test_carry_open_reserve_withholds_usdc_for_new_open() -> None:
    snap = _carry_snapshot(total_equity_usd="1000")
    d = _carry_decision(carry_weight=0.1)  # target TON = 100
    reserve = _carry_open_usdc_reserve(d, snap, CarryState(), Decimal("1000"))
    assert reserve == Decimal("100") * _CARRY_OPEN_USDT_FACTOR


def test_carry_open_reserve_zero_when_already_held() -> None:
    snap = _carry_snapshot(total_equity_usd="1000")
    d = _carry_decision(carry_weight=0.1)
    state = CarryState(positions=[_record("TON", "100")])
    assert _carry_open_usdc_reserve(d, snap, state, Decimal("1000")) == Decimal(0)


def test_carry_open_reserve_zero_when_below_min_action() -> None:
    snap = _carry_snapshot(total_equity_usd="1000")
    # target = 1000 × 0.0004 × 1.0 = 0.4 < MIN_ACTION_USDC (0.50)
    d = _carry_decision(carry_weight=0.0004)
    assert (
        _carry_open_usdc_reserve(d, snap, CarryState(), Decimal("1000"))
        == Decimal(0)
    )


def test_carry_open_reserve_zero_when_perp_market_missing() -> None:
    snap = _carry_snapshot(total_equity_usd="1000", perp_market={})
    d = _carry_decision(carry_weight=0.1)
    assert (
        _carry_open_usdc_reserve(d, snap, CarryState(), Decimal("1000"))
        == Decimal(0)
    )


# ─── _funding_carry_diff ────────────────────────────────────────────────────


def test_carry_diff_opens_when_target_and_no_state() -> None:
    snap = _carry_snapshot(total_equity_usd="1000")
    d = _carry_decision(carry_weight=0.1)
    closes, opens = _funding_carry_diff(
        snap, d, CarryState(), "20260603T000000Z",
        idx_offset=0, total_book_usd=Decimal("1000"),
    )
    assert closes == []
    assert len(opens) == 1
    op = opens[0]
    assert op.kind == ActionKind.OPEN_FUNDING_CARRY
    assert op.coin == "TON"
    assert op.amount == Decimal("100")
    # 100 USD / 2.0 mark = 50.0 base coin, rounded to step 0.001
    assert op.amount_native == Decimal("50.000")
    assert op.product_id == "TONUSDT"
    assert op.extra["spot_order_link_id"].endswith("_spot")
    assert op.extra["perp_order_link_id"].endswith("_perp")
    assert op.extra["mark_price"] == "2.0"


def test_carry_diff_closes_when_state_and_no_target() -> None:
    snap = _carry_snapshot(total_equity_usd="1000")
    d = Decision(
        thesis="Drop the carry; LLM removed the bybit_funding_carry venue this cycle.",
        venues=[VenueAllocation(venue_id="cash_usdc", weight=1.0)],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=0.0,
    )
    state = CarryState(positions=[_record("TON", "100")])
    closes, opens = _funding_carry_diff(
        snap, d, state, "20260603T000000Z",
        idx_offset=0, total_book_usd=Decimal("1000"),
    )
    assert opens == []
    assert len(closes) == 1
    cl = closes[0]
    assert cl.kind == ActionKind.CLOSE_FUNDING_CARRY
    assert cl.coin == "TON"
    assert cl.product_id == "TONUSDT"
    assert cl.amount_native == Decimal("50")  # 100/2 from record


def test_carry_diff_skips_close_when_attempts_at_max() -> None:
    """Fix #3 (2026-06-04): a stuck CLOSE (perp leg failing every
    cycle) must stop auto-retrying after MAX_CARRY_CLOSE_ATTEMPTS so
    the operator can take over. State record stays in place so the
    coin remains visible for manual unwind."""
    from agent.sandbox.execute import MAX_CARRY_CLOSE_ATTEMPTS
    snap = _carry_snapshot(total_equity_usd="1000")
    d = Decision(
        thesis=(
            "Drop the carry; LLM removed bybit_funding_carry this cycle "
            "(retry-counter regression scenario)."
        ),
        venues=[VenueAllocation(venue_id="cash_usdc", weight=1.0)],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=0.0,
    )
    stuck = _record("TON", "100").model_copy(
        update={"close_attempts": MAX_CARRY_CLOSE_ATTEMPTS}
    )
    state = CarryState(positions=[stuck])
    closes, opens = _funding_carry_diff(
        snap, d, state, "20260603T000000Z",
        idx_offset=0, total_book_usd=Decimal("1000"),
    )
    assert opens == []
    # No CLOSE emitted — stuck position is left for operator review.
    assert closes == []


# ─── _carry_liq_close_actions (liquidation de-risk sweep) ────────────────────


def test_carry_liq_close_closes_both_legs() -> None:
    """A near-liq carry coin present in carry_state emits one
    CLOSE_FUNDING_CARRY sized from the record (unwinds spot + perp)."""
    snap = _carry_snapshot()
    state = CarryState(positions=[_record("TON", "100")])
    actions = _carry_liq_close_actions(
        snap, state, {"TON"}, "20260609T000000Z", idx_offset=840
    )
    assert len(actions) == 1
    cl = actions[0]
    assert cl.kind == ActionKind.CLOSE_FUNDING_CARRY
    assert cl.coin == "TON"
    assert cl.product_id == "TONUSDT"
    assert cl.amount == Decimal("100")
    assert cl.amount_native == Decimal("50")  # 100/2 from record
    assert cl.extra["spot_order_link_id"].endswith("_spot")
    assert cl.extra["perp_order_link_id"].endswith("_perp")


def test_carry_liq_close_skips_coin_not_in_state() -> None:
    """A near-liq coin with no carry record (manual naked short) is left to
    the orphan-perp / LLM path, not closed here."""
    snap = _carry_snapshot()
    state = CarryState(positions=[_record("TON", "100")])
    assert _carry_liq_close_actions(
        snap, state, {"SOL"}, "20260609T000000Z", idx_offset=840
    ) == []
    # Empty trigger set → nothing closes either.
    assert _carry_liq_close_actions(
        snap, state, set(), "20260609T000000Z", idx_offset=840
    ) == []


def test_carry_liq_close_respects_max_attempts() -> None:
    """A persistently-failing close stops auto-retrying after
    MAX_CARRY_CLOSE_ATTEMPTS (mirrors the diff-layer guard)."""
    from agent.sandbox.execute import MAX_CARRY_CLOSE_ATTEMPTS
    snap = _carry_snapshot()
    stuck = _record("TON", "100").model_copy(
        update={"close_attempts": MAX_CARRY_CLOSE_ATTEMPTS}
    )
    state = CarryState(positions=[stuck])
    assert _carry_liq_close_actions(
        snap, state, {"TON"}, "20260609T000000Z", idx_offset=840
    ) == []


def test_carry_diff_noop_when_both_target_and_state() -> None:
    """MVP behavior: existing carry position + non-zero target = hold.
    No ADJUST action emitted (sizing changes deferred to follow-up)."""
    snap = _carry_snapshot(total_equity_usd="1000")
    d = _carry_decision(carry_weight=0.1)
    state = CarryState(positions=[_record("TON", "100")])
    closes, opens = _funding_carry_diff(
        snap, d, state, "20260603T000000Z",
        idx_offset=0, total_book_usd=Decimal("1000"),
    )
    assert opens == []
    assert closes == []


def test_carry_diff_skips_open_when_perp_market_missing() -> None:
    """Defensive: if perp_market lacks the coin (e.g. fan-out budget
    truncated), the OPEN can't be sized — skip rather than emit a
    broken action."""
    snap = _carry_snapshot(
        carry_products=[_carry_product("TON")],
        perp_market={},
    )
    d = _carry_decision()
    closes, opens = _funding_carry_diff(
        snap, d, CarryState(), "20260603T000000Z",
        idx_offset=0, total_book_usd=Decimal("1000"),
    )
    assert closes == [] and opens == []


def test_carry_diff_skips_open_when_qty_below_min() -> None:
    """When pick_usd / mark rounds down below min_order_qty after
    qty_step rounding, OPEN is skipped (would be rejected at execute)."""
    snap = _carry_snapshot(
        perp_market={
            "TON": _perp("TON", mark="100", min_order_qty="1", qty_step="1")
        },
        total_equity_usd="1000",
    )
    # pick_usd = 5 (0.5% × 1000 × 1.0). 5/100 = 0.05 base, rounded to
    # qty_step=1 → 0; below min_order_qty=1 → skip.
    d = _carry_decision(carry_weight=0.005)
    closes, opens = _funding_carry_diff(
        snap, d, CarryState(), "20260603T000000Z",
        idx_offset=0, total_book_usd=Decimal("1000"),
    )
    assert closes == [] and opens == []


def test_carry_diff_handles_multiple_coins_with_stable_ordering() -> None:
    """Same cycle: SOL needs OPEN, TON needs CLOSE — both appear in
    their respective lists in coin-sorted order so `orderLinkId`s are
    deterministic across runs."""
    snap = _carry_snapshot(
        carry_products=[_carry_product("SOL")],
        perp_market={
            "SOL": _perp("SOL", mark="50"),
            "TON": _perp("TON", mark="2.0"),
        },
        total_equity_usd="1000",
    )
    d = _carry_decision(picks=[("SOLUSDT", 1.0)])
    state = CarryState(positions=[_record("TON", "100")])
    closes, opens = _funding_carry_diff(
        snap, d, state, "20260603T000000Z",
        idx_offset=10, total_book_usd=Decimal("1000"),
    )
    assert [o.coin for o in opens] == ["SOL"]
    assert [c.coin for c in closes] == ["TON"]
    # Coins iterated in sorted order — SOL (offset 0 + base 10), TON (1+10)
    assert opens[0].order_link_id.endswith("10")
    assert closes[0].order_link_id.endswith("11")


def test_carry_diff_skips_open_when_pick_usd_below_min_action() -> None:
    """Tiny targets below MIN_ACTION_USDC are dropped — not worth the
    round-trip + Bybit's order-min."""
    snap = _carry_snapshot(total_equity_usd="1000")
    d = _carry_decision(carry_weight=0.0001)  # 1000 × 0.0001 = $0.10
    closes, opens = _funding_carry_diff(
        snap, d, CarryState(), "20260603T000000Z",
        idx_offset=0, total_book_usd=Decimal("1000"),
    )
    assert opens == [] and closes == []


# ─── Dispatch (_execute_one) ────────────────────────────────────────────────


def _open_action() -> Action:
    return Action(
        kind=ActionKind.OPEN_FUNDING_CARRY,
        category="FundingCarry",
        product_id="TONUSDT",
        coin="TON",
        amount=Decimal("100"),  # USD target, used as spot Buy quote qty
        amount_native=Decimal("50.000"),  # base coin qty (TON)
        order_link_id="lid",
        reason="test open",
        extra={
            "mark_price": "2.0",
            "spot_order_link_id": "lid_spot",
            "perp_order_link_id": "lid_perp",
        },
    )


def _close_action() -> Action:
    return Action(
        kind=ActionKind.CLOSE_FUNDING_CARRY,
        category="FundingCarry",
        product_id="TONUSDT",
        coin="TON",
        amount=Decimal("100"),
        amount_native=Decimal("50"),
        order_link_id="lidc",
        reason="test close",
        extra={
            "spot_order_link_id": "lidc_spot",
            "perp_order_link_id": "lidc_perp",
        },
    )


def _carry_wallet(usdt: str, usdc: str) -> list[SimpleNamespace]:
    """UNIFIED wallet-balance shape `_coin_equity_from_wallet` walks: a list of
    accounts each carrying a `coinDetail` of `{coin, equity}` entries."""
    return [
        SimpleNamespace(coinDetail=[
            {"coin": "USDT", "equity": usdt},
            {"coin": "USDC", "equity": usdc},
        ])
    ]


def _mock_client(
    *,
    spot_result: SpotOrderResult | Exception | None = None,
    perp_result: SpotOrderResult | Exception | None = None,
    spot_fill: SpotOrderStatus | Exception | None = None,
    unified_usdt: str = "100000",
    unified_usdc: str = "0",
) -> AsyncMock:
    """Build a minimal client mock for `_execute_one` happy / failure paths.
    `*_result` may be an `Exception` instance to drive the failure
    branch — `AsyncMock.side_effect` accepts a single exception.

    `spot_fill` controls what `get_spot_order_status` returns. Default
    is a Filled status with cumExecQty=50, cumExecValue=100 (matches
    the canonical `_open_action()`: spot $100 buy → 50 TON @ mark 2.0).
    Pass a SpotOrderStatus to simulate slippage / partial-fill /
    terminal-bad states. Pass an Exception to simulate the realtime
    lookup raising (e.g. order already cleared from realtime cache).

    `unified_usdt` defaults high so the ah.6 carry funding step finds USDT
    already provisioned and emits no USDC→USDT swap — pass "0" to simulate a
    USDC-only vault and exercise the swap path.
    """
    client = AsyncMock()
    client.set_leverage = AsyncMock(return_value=None)
    client.get_wallet_balance = AsyncMock(
        return_value=_carry_wallet(unified_usdt, unified_usdc)
    )
    client.get_account_coin_balance = AsyncMock(return_value=Decimal("0"))
    client.internal_transfer = AsyncMock(return_value=None)
    spot_default = SpotOrderResult(orderId="SPOT123", orderLinkId="lid_spot")
    perp_default = SpotOrderResult(orderId="PERP456", orderLinkId="lid_perp")
    if isinstance(spot_result, Exception):
        client.place_spot_order = AsyncMock(side_effect=spot_result)
    else:
        client.place_spot_order = AsyncMock(
            return_value=spot_result or spot_default
        )
    if isinstance(perp_result, Exception):
        client.place_perp_order = AsyncMock(side_effect=perp_result)
    else:
        client.place_perp_order = AsyncMock(
            return_value=perp_result or perp_default
        )
    fill_default = SpotOrderStatus(
        orderId="SPOT123",
        orderStatus="Filled",
        cumExecQty="50.000",
        cumExecValue="100",
    )
    if isinstance(spot_fill, Exception):
        client.get_spot_order_status = AsyncMock(side_effect=spot_fill)
    else:
        client.get_spot_order_status = AsyncMock(
            return_value=spot_fill or fill_default
        )
    return client


@pytest.mark.asyncio
async def test_dispatch_open_happy_path_sets_leverage_then_spot_then_perp() -> None:
    client = _mock_client()
    res = await _execute_one(client, _open_action(), dry_run=False)
    assert res.status == "ok"
    # set_leverage(1) before any orders
    client.set_leverage.assert_awaited_once_with("TONUSDT", 1)
    # Spot Buy with quote USDT qty (=100) and tagged orderLinkId
    client.place_spot_order.assert_awaited_once()
    spot_call = client.place_spot_order.await_args
    assert spot_call.kwargs["side"] == "Buy"
    assert spot_call.kwargs["symbol"] == "TONUSDT"
    assert spot_call.kwargs["qty_quote"] == "100"
    assert spot_call.kwargs["order_link_id"] == "lid_spot"
    # Perp Sell with base qty (=50) and tagged orderLinkId
    perp_call = client.place_perp_order.await_args
    assert perp_call.kwargs["side"] == "Sell"
    assert perp_call.kwargs["qty"] == "50.000"
    assert perp_call.kwargs["order_link_id"] == "lid_perp"
    # Response carries both legs
    assert res.response is not None
    assert res.response["legs"]["spot"]["orderId"] == "SPOT123"
    assert res.response["legs"]["perp"]["orderId"] == "PERP456"


@pytest.mark.asyncio
async def test_dispatch_open_funds_usdt_via_swap_on_usdc_vault() -> None:
    """ah.6 (dispatch-1): on a USDC-only vault (UNIFIED USDT=0) the carry OPEN
    first swaps USDC→USDT (USDCUSDT Sell) to fund the spot Buy + perp margin,
    THEN buys the coin. Pre-fix the spot Buy 170131'd for lack of USDT."""
    client = _mock_client(unified_usdt="0", unified_usdc="100000")
    res = await _execute_one(client, _open_action(), dry_run=False)
    assert res.status == "ok"
    symbols = [c.kwargs["symbol"] for c in client.place_spot_order.await_args_list]
    sides = [c.kwargs["side"] for c in client.place_spot_order.await_args_list]
    # USDC→USDT funding swap precedes the {coin}USDT Buy.
    assert symbols == ["USDCUSDT", "TONUSDT"]
    assert sides == ["Sell", "Buy"]
    assert res.response["legs"]["funding"]["swap"] == "USDCUSDT Sell"
    # Sized to cover spot (1x) + perp margin (1.05x) of the $100 pick.
    assert res.response["legs"]["funding"]["required_usdt"] == "205.000000"


@pytest.mark.asyncio
async def test_dispatch_open_skips_swap_when_usdt_already_funded() -> None:
    """When UNIFIED already holds enough USDT, the carry OPEN emits NO funding
    swap — just the single {coin}USDT spot Buy."""
    client = _mock_client(unified_usdt="100000")
    res = await _execute_one(client, _open_action(), dry_run=False)
    assert res.status == "ok"
    client.place_spot_order.assert_awaited_once()
    assert client.place_spot_order.await_args.kwargs["symbol"] == "TONUSDT"
    assert "funding" not in res.response["legs"]


@pytest.mark.asyncio
async def test_dispatch_open_spot_failure_does_not_call_perp() -> None:
    """Atomic-pair guard: if the spot Buy raises, the perp Sell must
    NOT be submitted — caller is left with no position (and no naked
    short, which would be unrecoverable)."""
    client = _mock_client(
        spot_result=BybitAPIError(170140, "Order value below limit", "/v5/order/create")
    )
    res = await _execute_one(client, _open_action(), dry_run=False)
    assert res.status == "error"
    assert res.error is not None and "170140" in res.error
    client.place_perp_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_open_perp_failure_after_spot_marks_orphan() -> None:
    """Spot filled but perp leg failed → orphan. Status is `orphan`,
    response carries the successful spot leg AND the perp error so the
    cycle log shows what's stuck on Bybit."""
    client = _mock_client(
        perp_result=BybitAPIError(110007, "Insufficient margin", "/v5/order/create")
    )
    res = await _execute_one(client, _open_action(), dry_run=False)
    assert res.status == "orphan"
    assert res.response is not None
    assert res.response["legs"]["spot"]["orderId"] == "SPOT123"
    assert "110007" in res.response["legs"]["perp"]["error"]
    assert res.error is not None and "perp leg failed after spot fill" in res.error


@pytest.mark.asyncio
async def test_dispatch_open_dry_run_skips_all_calls() -> None:
    client = _mock_client()
    res = await _execute_one(client, _open_action(), dry_run=True)
    assert res.status == "dry-run"
    client.set_leverage.assert_not_awaited()
    client.place_spot_order.assert_not_awaited()
    client.place_perp_order.assert_not_awaited()
    assert res.response is not None
    assert res.response["would_call"] == "open_funding_carry"
    assert res.response["spot_qty_quote_usdt"] == "100"
    assert res.response["perp_qty_base"] == "50.000"


@pytest.mark.asyncio
async def test_dispatch_open_paired_notional_drift_marks_orphan() -> None:
    """Drift check now compares ACTUAL spot fill USD vs perp notional
    sized from actual base qty (post 2026-06-04 fix #2). Triggers when
    the realized fill price diverges from the snapshot mark beyond 5%
    — e.g. a thin order book filled 80 base for $100 quote (fill price
    $1.25 vs mark $2.0), so perp_notional = 80 × 2.0 = $160 while
    spot_notional = $100 → 60% drift. Perp leg is skipped; the spot is then
    UNWOUND atomically (ah.9) so no naked long is left."""
    action = _open_action()
    # Anomalous spot fill: received 80 TON for $100 (slippage to $1.25).
    fill = SpotOrderStatus(
        orderId="SPOT123",
        orderStatus="Filled",
        cumExecQty="80",
        cumExecValue="100",
    )
    client = _mock_client(spot_fill=fill)
    res = await _execute_one(client, action, dry_run=False)
    assert res.status == "orphan"
    # Two spot orders: the Buy, then the ah.9 compensating unwind Sell.
    assert client.place_spot_order.await_count == 2
    unwind_call = client.place_spot_order.await_args_list[1]
    assert unwind_call.kwargs["side"] == "Sell"
    assert unwind_call.kwargs["symbol"] == "TONUSDT"
    assert res.response["legs"]["unwind"]["unwound"] is True
    # Perp leg NOT submitted (drift gate fires before the place_perp_order)
    client.place_perp_order.assert_not_awaited()
    assert res.error is not None and "paired-notional check failed" in res.error


@pytest.mark.asyncio
async def test_dispatch_open_sizes_perp_from_actual_fill_not_plan() -> None:
    """Post 2026-06-04 fix #2: perp short qty MUST come from real spot
    fill (cumExecQty), not the planner's amount_native. Without this
    the short stays sized to plan while the spot leg actually received
    a different qty — leaves a delta-unbalanced pair on every slipped
    fill. Regression scenario: plan native=50, actual fill=49.5 (mild
    slippage); perp must be Sell 49.5, not 50."""
    action = _open_action()  # amount_native=50.000, amount=$100, mark=2.0
    # Slipped fill: 49.5 base for $99 quote (price ≈ $2.0, drift well
    # under 5% so the drift gate passes).
    fill = SpotOrderStatus(
        orderId="SPOT123",
        orderStatus="Filled",
        cumExecQty="49.5",
        cumExecValue="99",
    )
    client = _mock_client(spot_fill=fill)
    res = await _execute_one(client, action, dry_run=False)
    assert res.status == "ok"
    perp_call = client.place_perp_order.await_args
    # qty comes from cumExecQty, not amount_native ("50.000").
    assert perp_call.kwargs["qty"] == "49.5"
    assert res.response is not None
    assert res.response["legs"]["spot"]["cumExecQty"] == "49.5"
    assert res.response["legs"]["spot"]["cumExecValue"] == "99"


@pytest.mark.asyncio
async def test_dispatch_open_orphans_when_spot_fill_unconfirmed() -> None:
    """If the spot order can't be confirmed Filled within the poll
    window (terminal-bad state, or realtime lookup never returns
    Filled), the perp leg MUST be skipped. Pre-fix #2 the executor
    would charge ahead and open a sized-from-plan short on top of an
    indeterminate spot leg → unbounded naked exposure if the spot
    later rejected or was cancelled."""
    action = _open_action()
    cancelled = SpotOrderStatus(
        orderId="SPOT123",
        orderStatus="Cancelled",
        cumExecQty="0",
        cumExecValue="0",
        rejectReason="EC_NoEnoughCash",
    )
    client = _mock_client(spot_fill=cancelled)
    res = await _execute_one(client, action, dry_run=False)
    assert res.status == "orphan"
    client.place_spot_order.assert_awaited_once()
    # Perp leg NOT submitted — spot fill not confirmed.
    client.place_perp_order.assert_not_awaited()
    assert res.error is not None and "spot fill verification" in res.error


@pytest.mark.asyncio
async def test_dispatch_close_happy_path() -> None:
    client = _mock_client()
    res = await _execute_one(client, _close_action(), dry_run=False)
    assert res.status == "ok"
    # No leverage call on close
    client.set_leverage.assert_not_awaited()
    # Spot Sell with base qty
    spot_call = client.place_spot_order.await_args
    assert spot_call.kwargs["side"] == "Sell"
    assert spot_call.kwargs["qty_base"] == "50"
    # Perp Buy with reduce_only=True
    perp_call = client.place_perp_order.await_args
    assert perp_call.kwargs["side"] == "Buy"
    assert perp_call.kwargs["reduce_only"] is True
    assert perp_call.kwargs["qty"] == "50"


@pytest.mark.asyncio
async def test_dispatch_close_spot_failure_does_not_call_perp() -> None:
    client = _mock_client(
        spot_result=BybitAPIError(170140, "Sell below min", "/v5/order/create")
    )
    res = await _execute_one(client, _close_action(), dry_run=False)
    assert res.status == "error"
    client.place_perp_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_close_perp_failure_after_spot_marks_orphan() -> None:
    """Spot Sell succeeded (USDT back), perp Buy failed → naked short
    remains. Cycle records orphan; state record persists so next
    cycle's CLOSE branch retries."""
    client = _mock_client(
        perp_result=BybitAPIError(110007, "Margin insufficient", "/v5/order/create")
    )
    res = await _execute_one(client, _close_action(), dry_run=False)
    assert res.status == "orphan"
    assert res.response["legs"]["spot"]["orderId"] == "SPOT123"
    assert "110007" in res.response["legs"]["perp"]["error"]


@pytest.mark.asyncio
async def test_dispatch_close_rejects_missing_amount_native() -> None:
    action = _close_action()
    action.amount_native = None
    client = _mock_client()
    res = await _execute_one(client, action, dry_run=False)
    assert res.status == "error"
    assert res.error == "amount_native missing on CLOSE_FUNDING_CARRY"
    client.place_spot_order.assert_not_awaited()
    client.place_perp_order.assert_not_awaited()


# ─── Coordination: hedge layer skips carry coins ────────────────────────────


def test_hedge_diff_skips_perp_shorts_owned_by_carry() -> None:
    """A TON perp short already attributed to carry must NOT be
    reconciled by the Earn-hedge layer (which would CLOSE it because
    no Earn pick on TON exists)."""
    snap = _carry_snapshot()
    # Add a current perp short on TON that LOOKS orphan to the hedge
    # layer — must be skipped because carry_state owns it.
    snap.perp_positions = [
        PerpPosition(
            symbol="TONUSDT",
            side="Sell",
            size="50",
            mark_price="2.0",
            positionValue="100",
        ),
    ]
    d = _carry_decision()  # no Earn picks → would normally drive CLOSE
    closes, opens = _hedge_diff_actions(
        snap,
        d,
        "20260603T000000Z",
        idx_offset=0,
        total_book_usd=Decimal("1000"),
        carry_coins={"TON"},
    )
    # No hedge close emitted for TON — carry owns it.
    assert all(c.coin != "TON" for c in closes)


def test_hedge_diff_still_reconciles_non_carry_perp_shorts() -> None:
    """Pre-existing hedge behavior preserved when carry_coins doesn't
    include the position's coin."""
    snap = _carry_snapshot()
    snap.perp_positions = [
        PerpPosition(
            symbol="SOLUSDT",
            side="Sell",
            size="2",
            mark_price="50.0",
            positionValue="100",
        ),
    ]
    d = _carry_decision()
    closes, opens = _hedge_diff_actions(
        snap, d, "20260603T000000Z",
        idx_offset=0, total_book_usd=Decimal("1000"),
        carry_coins={"TON"},  # only TON owned by carry
    )
    # SOL short is orphan to BOTH hedge and carry — hedge closes it.
    assert any(c.coin == "SOL" for c in closes)


# ─── State application from results ─────────────────────────────────────────


def _result(action: Action, status: str = "ok") -> ActionResult:
    return ActionResult(
        action=action,
        status=status,
        response=None,
        error=None,
        started_at=datetime.now(UTC).isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
    )


def test_apply_results_inserts_open_record_on_ok() -> None:
    state = CarryState()
    res = [_result(_open_action(), "ok")]
    new = apply_carry_results_to_state(state, res)
    assert new.active_coins() == {"TON"}
    rec = new.get("TON")
    assert rec is not None
    assert rec.target_pick_usd == Decimal("100")
    assert rec.spot_qty_base == Decimal("50.000")
    assert rec.mark_price_at_open == Decimal("2.0")


def test_apply_results_removes_record_on_close_ok() -> None:
    state = CarryState(positions=[_record("TON", "100")])
    res = [_result(_close_action(), "ok")]
    new = apply_carry_results_to_state(state, res)
    assert new.active_coins() == set()


def test_apply_results_keeps_record_on_close_orphan() -> None:
    """CLOSE orphan = spot sold, perp leg failed → naked short still
    open. Next cycle must retry CLOSE; keep the record."""
    state = CarryState(positions=[_record("TON", "100")])
    res = [_result(_close_action(), "orphan")]
    new = apply_carry_results_to_state(state, res)
    assert new.active_coins() == {"TON"}


def test_apply_results_increments_close_attempts_on_orphan() -> None:
    """Fix #3 (2026-06-04): every CLOSE orphan bumps `close_attempts`
    on the kept record so the diff layer can stop emitting after a
    threshold — without this, a perp leg failing identically every
    cycle would re-emit CLOSE forever."""
    state = CarryState(positions=[_record("TON", "100")])
    assert state.get("TON").close_attempts == 0
    res = [_result(_close_action(), "orphan")]
    state2 = apply_carry_results_to_state(state, res)
    assert state2.get("TON").close_attempts == 1
    # Second orphan bumps again.
    state3 = apply_carry_results_to_state(state2, res)
    assert state3.get("TON").close_attempts == 2


def test_apply_results_does_not_increment_close_attempts_on_ok() -> None:
    """Successful CLOSE removes the record entirely — counter doesn't
    persist."""
    state = CarryState(positions=[_record("TON", "100")])
    res = [_result(_close_action(), "ok")]
    new = apply_carry_results_to_state(state, res)
    assert new.get("TON") is None  # record gone, counter doesn't matter


def test_apply_results_skips_open_orphan() -> None:
    """OPEN orphan = spot bought, perp leg failed → naked spot, no
    record (the orphan log is what operator follows)."""
    state = CarryState()
    res = [_result(_open_action(), "orphan")]
    new = apply_carry_results_to_state(state, res)
    assert new.active_coins() == set()


def test_apply_results_ignores_dry_run_and_errors() -> None:
    state = CarryState()
    res = [
        _result(_open_action(), "dry-run"),
        _result(_close_action(), "error"),
    ]
    new = apply_carry_results_to_state(state, res)
    assert new.positions == state.positions


# ─── End-to-end: diff_to_actions integrates carry layer ─────────────────────


def test_diff_to_actions_includes_carry_open_after_hedge_opens() -> None:
    snap = _carry_snapshot()
    d = _carry_decision()
    actions = diff_to_actions(
        snap, d, "20260603T000000Z", carry_state=CarryState()
    )
    carry_idx = next(
        (i for i, a in enumerate(actions)
         if a.kind == ActionKind.OPEN_FUNDING_CARRY),
        None,
    )
    assert carry_idx is not None
    # Position relative to other kinds: after subscribes/hedge_opens
    # by the ordering contract.
    open_perp_idx = next(
        (i for i, a in enumerate(actions)
         if a.kind == ActionKind.OPEN_PERP_SHORT),
        None,
    )
    if open_perp_idx is not None:
        assert open_perp_idx < carry_idx


# ─── Kill-switch: funding flip → auto CLOSE (`.6` smoke invariant) ──────────


def _no_carry_decision() -> Decision:
    """A decision that does NOT include `bybit_funding_carry` at all —
    simulates the LLM dropping the pick after a funding regime
    change. Cash-only baseline."""
    return Decision(
        thesis=(
            "Funding regime flipped negative; rotate out of carry. "
            "All-cash this cycle while the watcher re-baselines."
        ),
        venues=[VenueAllocation(venue_id="cash_usdc", weight=1.0)],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=0.0,
    )


def test_kill_switch_funding_flip_drops_coin_from_snapshot_category() -> None:
    """When a coin's 7d-avg funding crosses below `FUNDING_FLOOR_CARRY_ANNUAL`,
    `_build_funding_carry_products` excludes it — the LLM literally
    cannot re-pick it because the snapshot's `products.FundingCarry`
    no longer carries the row. Defense-in-depth before validator."""
    from agent.sandbox.snapshot import _build_funding_carry_products

    # 4h coin at +0.00001 → annualized ≈ +2.2% (below +5.475% floor).
    perp_market = {
        "TON": _perp("TON", funding_7d="0.00001"),
    }
    # Override the default 8h interval — flip rate to 4h to exercise
    # the annualization path that the post-fix code uses.
    perp_market["TON"].funding_interval_hours = Decimal("4")
    rows = _build_funding_carry_products(perp_market, Decimal("10000"))
    assert rows == []  # below floor → filtered out


def test_kill_switch_emits_close_when_state_holds_but_decision_drops_carry() -> None:
    """Funding flips → next cycle's snapshot drops TON from
    `products.FundingCarry`; LLM (or even validator) drops the
    `bybit_funding_carry` venue from the decision; diff sees state
    only → emits CLOSE_FUNDING_CARRY for TON. No human in the loop
    required to wind down the position."""
    # Snapshot WITHOUT TON in products.FundingCarry (funding flip
    # already filtered the coin from the carry catalog).
    snap = _carry_snapshot(carry_products=[])
    state = CarryState(positions=[_record("TON", "100")])
    d = _no_carry_decision()
    actions = diff_to_actions(
        snap, d, "20260603T000000Z", carry_state=state
    )
    close_acts = [a for a in actions if a.kind == ActionKind.CLOSE_FUNDING_CARRY]
    assert len(close_acts) == 1
    assert close_acts[0].coin == "TON"
    assert close_acts[0].amount_native == Decimal("50")


def test_kill_switch_close_action_carries_state_recorded_qty() -> None:
    """CLOSE is sized from the persisted `spot_qty_base`, NOT from
    the current snapshot perp_market mark — survives the case where
    the carry coin's perp_market entry was dropped this cycle (e.g.
    rate-limited fan-out, exchange downtime)."""
    snap = _carry_snapshot(perp_market={})  # perp_market completely gone
    state = CarryState(positions=[_record("TON", "100")])
    d = _no_carry_decision()
    actions = diff_to_actions(
        snap, d, "20260603T000000Z", carry_state=state
    )
    close = next(a for a in actions if a.kind == ActionKind.CLOSE_FUNDING_CARRY)
    # 100 / 2.0 = 50 (from record), not from snapshot
    assert close.amount_native == Decimal("50")


def test_kill_switch_state_persists_through_orphan_close_for_retry() -> None:
    """Critical safety property: CLOSE that ends as orphan (perp leg
    failed after spot Sell) keeps the carry record so the next
    cycle's diff re-emits CLOSE. Without this, naked shorts would
    silently accumulate."""
    state = CarryState(positions=[_record("TON", "100")])
    # Simulate an orphan close — spot Sell succeeded, perp Buy failed.
    res = [_result(_close_action(), "orphan")]
    new_state = apply_carry_results_to_state(state, res)
    assert new_state.active_coins() == {"TON"}
    # Now next cycle: state still has TON, snapshot still has no carry
    # target, diff re-emits CLOSE.
    snap = _carry_snapshot(carry_products=[])
    d = _no_carry_decision()
    next_actions = diff_to_actions(
        snap, d, "20260603T000010Z", carry_state=new_state
    )
    assert any(
        a.kind == ActionKind.CLOSE_FUNDING_CARRY and a.coin == "TON"
        for a in next_actions
    )


def test_validator_rejects_decision_picking_dropped_carry_coin() -> None:
    """Belt-and-suspenders: if LLM picks `TONUSDT` from a stale
    memory but the snapshot's FundingCarry list no longer includes
    it (funding flipped), `check_product_ids_in_snapshot` catches it.
    This is the safety net behind the snapshot-level filter."""
    from agent.validate.rules import check_product_ids_in_snapshot

    snap = _carry_snapshot(carry_products=[])  # TON dropped from carry list
    d = _carry_decision(picks=[("TONUSDT", 1.0)])
    ok, msg = check_product_ids_in_snapshot(d, snap)
    assert ok is False
    assert msg is not None and "TONUSDT" in msg


def test_kill_switch_close_orders_before_open() -> None:
    """Sequencing invariant: in the same cycle where SOL is being
    opened (positive funding) AND TON is being closed (funding flip),
    the CLOSE runs first to free USDT margin / spot principal that
    the OPEN consumes. Verified via list order in diff_to_actions."""
    # SOL is the only valid carry candidate; TON dropped after flip.
    snap = _carry_snapshot(
        carry_products=[_carry_product("SOL")],
        perp_market={
            "TON": _perp("TON", funding_7d="0.0001"),  # data for state-sizing
            "SOL": _perp("SOL", mark="50", funding_7d="0.0001"),
        },
    )
    state = CarryState(positions=[_record("TON", "100")])
    d = _carry_decision(picks=[("SOLUSDT", 1.0)])
    actions = diff_to_actions(
        snap, d, "20260603T000000Z", carry_state=state
    )
    close_idx = next(
        (i for i, a in enumerate(actions)
         if a.kind == ActionKind.CLOSE_FUNDING_CARRY),
        None,
    )
    open_idx = next(
        (i for i, a in enumerate(actions)
         if a.kind == ActionKind.OPEN_FUNDING_CARRY),
        None,
    )
    assert close_idx is not None and open_idx is not None
    assert close_idx < open_idx


# ─── ah.7 crash recovery: pending-intent marker + order-link verification ────


def test_pending_intent_roundtrip(tmp_path: Path) -> None:
    """Write → read → clear a pending-intent marker (durable across a crash)."""
    from agent.sandbox.pending_intent import (
        PendingIntent,
        PendingOrderLink,
        clear_pending_intent,
        read_pending_intent,
        write_pending_intent,
    )
    path = tmp_path / "pending_intent.json"
    intent = PendingIntent(
        snapshot_ts="20260609T120000Z",
        links=[PendingOrderLink(
            order_link_id="lid_perp", category="linear", symbol="TONUSDT",
            kind="open_funding_carry", coin="TON",
        )],
    )
    write_pending_intent(intent, path)
    loaded = read_pending_intent(path)
    assert loaded is not None
    assert loaded.snapshot_ts == "20260609T120000Z"
    assert loaded.links[0].order_link_id == "lid_perp"
    clear_pending_intent(path)
    assert read_pending_intent(path) is None
    # Clearing an already-absent marker is a no-op (idempotent).
    clear_pending_intent(path)


def test_read_pending_intent_missing_returns_none(tmp_path: Path) -> None:
    from agent.sandbox.pending_intent import read_pending_intent
    assert read_pending_intent(tmp_path / "nope.json") is None


def test_confirmable_order_links_expands_carry_into_both_legs() -> None:
    """A carry open contributes BOTH a spot and a linear leg link (under the
    derived `_spot` / `_perp` ids); a perp short contributes one linear link;
    an Earn subscribe is unconfirmable and contributes none."""
    carry = _open_action()  # spot_order_link_id=lid_spot, perp_order_link_id=lid_perp
    perp = Action(
        kind=ActionKind.OPEN_PERP_SHORT, category="hedge", product_id="IDUSDT",
        coin="ID", amount=Decimal("3"), order_link_id="p1", reason="hedge",
    )
    subscribe = Action(
        kind=ActionKind.SUBSCRIBE_EARN, category="FlexibleSaving",
        product_id="1131", coin="USDC", amount=Decimal("10"),
        order_link_id="s1", reason="earn",
    )
    links = _confirmable_order_links([carry, perp, subscribe])
    by_link = {link["order_link_id"]: link for link in links}
    assert by_link["lid_spot"]["category"] == "spot"
    assert by_link["lid_perp"]["category"] == "linear"
    assert by_link["p1"]["category"] == "linear"
    # Earn subscribe is omitted (no order-history endpoint).
    assert "s1" not in by_link
    assert len(links) == 3


@pytest.mark.asyncio
async def test_verify_order_links_classifies_landed_and_no_trace() -> None:
    """`verify_order_links` marks a link whose order is on Bybit as
    confirmed-landed and one with no order as no-trace."""
    client = AsyncMock()
    # First link lands (history non-empty), second doesn't.
    client.get_order_history = AsyncMock(side_effect=[[{"orderId": "X"}], []])
    res = await verify_order_links(client, [
        {"order_link_id": "lid_perp", "category": "linear", "symbol": "TONUSDT"},
        {"order_link_id": "lid_spot", "category": "spot", "symbol": "TONUSDT"},
    ])
    assert res["counts"].get("confirmed-landed") == 1
    assert res["counts"].get("no-trace") == 1


@pytest.mark.asyncio
async def test_verify_order_links_marks_query_error() -> None:
    """A history lookup that raises is classified query-error (can't confirm →
    the startup gate treats it as blocking)."""
    from agent.bybit_oracle.bybit_client import BybitAPIError
    client = AsyncMock()
    client.get_order_history = AsyncMock(
        side_effect=BybitAPIError(10001, "boom", "/v5/order/history")
    )
    res = await verify_order_links(client, [
        {"order_link_id": "l", "category": "linear", "symbol": "TONUSDT"},
    ])
    assert res["counts"].get("query-error") == 1


# ─── ah.9 actual-fill persistence + atomic orphan unwind ────────────────────


def test_apply_carry_open_records_actual_fill_not_plan() -> None:
    """dispatch-3: the carry_state record is sized from the ACTUAL spot fill
    (cumExecQty + realized price), not the planner's amount_native/mark — so a
    later CLOSE sizes both legs off the real position."""
    action = _open_action()  # planned amount_native=50, mark=2.0
    result = ActionResult(
        action=action,
        status="ok",
        response={"legs": {"spot": {"cumExecQty": "80", "cumExecValue": "100"}}},
        error=None,
        started_at="2026-06-09T00:00:00+00:00",
        finished_at="2026-06-09T00:00:01+00:00",
    )
    rec = apply_carry_results_to_state(CarryState(), [result]).get("TON")
    assert rec is not None
    assert rec.spot_qty_base == Decimal("80")  # actual fill, not planned 50
    assert rec.perp_qty_base == Decimal("80")
    assert rec.mark_price_at_open == Decimal("1.25")  # 100/80 realized price


def test_apply_carry_open_falls_back_to_plan_without_fill() -> None:
    """No cumExecQty in the response → fall back to the planned amount_native /
    mark (older actions / partial response shapes)."""
    result = ActionResult(
        action=_open_action(), status="ok", response={"legs": {"spot": {}}},
        error=None, started_at="t", finished_at="t",
    )
    rec = apply_carry_results_to_state(CarryState(), [result]).get("TON")
    assert rec is not None
    assert rec.spot_qty_base == Decimal("50.000")  # planned amount_native
    assert rec.mark_price_at_open == Decimal("2.0")  # planned mark


def test_apply_carry_open_orphan_writes_no_record() -> None:
    """state-3: an orphan OPEN writes NO position record — the dispatch unwinds
    the spot and the orphan-seller sweeps any residual; a perp_qty=0 record
    would collide with that sweep."""
    result = ActionResult(
        action=_open_action(), status="orphan",
        response={"legs": {"spot": {"cumExecQty": "50"}, "unwind": {"unwound": True}}},
        error="perp leg failed", started_at="t", finished_at="t",
    )
    state = apply_carry_results_to_state(CarryState(), [result])
    assert state.get("TON") is None


@pytest.mark.asyncio
async def test_dispatch_open_perp_failure_unwinds_spot() -> None:
    """ah.9: when the perp leg fails after the spot filled, the spot is sold
    back atomically — no naked long left."""
    client = _mock_client(
        perp_result=BybitAPIError(110007, "Insufficient margin", "/v5/order/create")
    )
    res = await _execute_one(client, _open_action(), dry_run=False)
    assert res.status == "orphan"
    assert client.place_spot_order.await_count == 2  # Buy + unwind Sell
    assert client.place_spot_order.await_args_list[1].kwargs["side"] == "Sell"
    assert res.response["legs"]["unwind"]["unwound"] is True


@pytest.mark.asyncio
async def test_dispatch_open_unwind_failure_surfaces_naked() -> None:
    """If the perp fails AND the compensating unwind Sell also fails, the
    result flags the genuinely-naked spot (unwound=False) for the operator /
    next-cycle sweep."""
    client = _mock_client(
        perp_result=BybitAPIError(110007, "Insufficient margin", "/v5/order/create")
    )
    # Spot Buy succeeds; the unwind Sell (2nd spot call) raises.
    client.place_spot_order = AsyncMock(side_effect=[
        SpotOrderResult(orderId="SPOT123", orderLinkId="lid_spot"),
        BybitAPIError(170131, "Insufficient balance", "/v5/order/create"),
    ])
    res = await _execute_one(client, _open_action(), dry_run=False)
    assert res.status == "orphan"
    assert res.response["legs"]["unwind"]["unwound"] is False
    assert "170131" in res.response["legs"]["unwind"]["error"]
