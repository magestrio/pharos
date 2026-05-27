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
    _order_link_id,
)
from agent.sandbox.snapshot import (
    MarketSnapshot,
    PerpInfo,
    Snapshot,
    UsdcPegSnapshot,
    WalletSnapshot,
)


# ─── Fixture factories ──────────────────────────────────────────────────────


def _snapshot(
    *,
    total_equity_usd: str = "100",
    earn_positions: list[dict] | None = None,
    perp_market: dict[str, PerpInfo] | None = None,
    perp_positions: list[PerpPosition] | None = None,
) -> Snapshot:
    return Snapshot(
        captured_at=datetime.now(UTC),
        wallet=WalletSnapshot(total_equity_usd=Decimal(total_equity_usd)),
        earn_positions=earn_positions or [],
        lm_positions=[],
        products={"FlexibleSaving": [], "OnChain": [], "LiquidityMining": []},
        market=MarketSnapshot(),
        perp_market=perp_market or {},
        perp_positions=perp_positions or [],
        usdc_peg=UsdcPegSnapshot(
            price_usd=Decimal("1.0"),
            deviation_bps=Decimal("0"),
            fetched_at=datetime.now(UTC),
        ),
        errors=[],
    )


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


def test_diff_lm_pick_emits_skip_not_action() -> None:
    snap = _snapshot(total_equity_usd="100")
    d = _decision(
        [
            _venue("cash_usdc", 0.7),
            _venue("bybit_lm", 0.3, [("24", 1.0)]),
        ]
    )
    actions = diff_to_actions(snap, d, snapshot_ts="20260527T120000Z")
    assert len(actions) == 1
    assert actions[0].kind == ActionKind.SKIP_OUT_OF_SCOPE
    assert "LiquidityMining" in actions[0].category
    assert "not wired" in actions[0].reason


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


def _decision_with_hedge(hedge_notional: float = -50.0) -> Decision:
    """Decision with TON OnChain pick + matching TON short hedge."""
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
    snap = _snapshot(total_equity_usd="100", perp_market={})
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
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={"TON": _perp("TON", mark="2.0")},
        perp_positions=[
            _short_pos("TON", size="25", position_value="50.00"),
        ],
    )
    d = _decision_with_hedge(hedge_notional=-80.0)
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
    but we cannot reopen at any meaningful target size → skip the open."""
    snap = _snapshot(
        total_equity_usd="100",
        perp_market={},  # no TON entry
        perp_positions=[
            _short_pos("TON", size="25", position_value="50.00"),
        ],
    )
    d = _decision_with_hedge(hedge_notional=-80.0)  # asks to resize
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
