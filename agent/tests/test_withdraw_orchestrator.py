from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from agent.bybit_oracle.bybit_client import BybitOrderError, WithdrawResult
from agent.bybit_oracle.chain_writer import ChainSendError
from agent.bybit_oracle.events import WithdrawRequested
from agent.bybit_oracle.product_picker import NoProductAvailable, PickedProduct
from agent.bybit_oracle.state import (
    WITHDRAW_CONFIRMED,
    WITHDRAW_FAILED,
    WITHDRAW_HEDGE_CLOSE_SKIPPED,
    WITHDRAW_ON_MANTLE,
    WITHDRAW_RECEIVED,
    WITHDRAW_SWAP_SKIPPED,
    open_db,
    upsert_withdraw_request,
)
from agent.bybit_oracle.withdraw_orchestrator import WithdrawOrchestrator


@pytest.fixture
def db(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    yield conn
    conn.close()


@pytest.fixture
def event():
    return WithdrawRequested(
        tx_id=42,
        amount=50_000_000,  # 50 USDC
        tx_hash="0xabc",
        log_index=0,
        block_number=1,
    )


ATTESTOR_ADDR = "0xattestor"
CONTRACT_ADDR = "0xbybitattestor-contract"


def _make_orchestrator(
    *,
    picked: PickedProduct | None = None,
    hedge_close_outcome: str = "skipped",
    delivered_usdc: Decimal = Decimal("49.5"),
    baseline_micro: int = 1_000_000,
    credited_micro: int = 49_000_000,
    bybit_withdraw_id: str = "wd-1",
):
    chain = MagicMock(name="ChainWriter")
    # Properties via PropertyMock so `chain.address` etc. behave like properties.
    type(chain).address = PropertyMock(return_value=ATTESTOR_ADDR)
    type(chain).attestor_contract_address = PropertyMock(return_value=CONTRACT_ADDR)
    chain.read_usdc_balance.return_value = baseline_micro
    chain.approve_usdc.return_value = "0xapprove-tx"
    chain.push_confirm_withdraw.return_value = "0xconfirm-tx"

    bybit = AsyncMock(name="BybitClient")
    bybit.withdraw_to_mantle.return_value = WithdrawResult(id=bybit_withdraw_id)

    picker = AsyncMock(name="ProductPicker")
    picker.pick.return_value = picked or PickedProduct(
        product_id="prod-USDC", target_coin="USDC", estimated_apr=Decimal("4.5")
    )

    # RedeemSwapExecutor's execute advances FSM through REDEEMED →
    # SWAP_SKIPPED (USDC) or SWAPPED_TO_USDC (volatile). Mock it to mimic.
    redeem_swap = AsyncMock(name="RedeemSwapExecutor")

    from agent.bybit_oracle import state as _s

    async def fake_execute(conn, tx_id, source):
        row = conn.execute(
            "SELECT status FROM withdraw_requests WHERE tx_id = ?", (tx_id,)
        ).fetchone()
        from_status = row["status"]
        assert from_status in (_s.WITHDRAW_HEDGE_CLOSED, _s.WITHDRAW_HEDGE_CLOSE_SKIPPED)
        _s.advance_withdraw_status(
            conn, tx_id, from_status, _s.WITHDRAW_REDEEMED,
            bybit_earn_redeem_id="redeem-mock",
        )
        if source.staked_coin.upper() == "USDC":
            _s.advance_withdraw_status(
                conn, tx_id, _s.WITHDRAW_REDEEMED, _s.WITHDRAW_SWAP_SKIPPED
            )
        else:
            _s.advance_withdraw_status(
                conn, tx_id, _s.WITHDRAW_REDEEMED, _s.WITHDRAW_SWAPPED_TO_USDC,
                bybit_swap_order_id="swap-back-mock",
            )
        return delivered_usdc

    redeem_swap.execute.side_effect = fake_execute

    hedge = AsyncMock(name="HedgeTrigger")
    hedge.maybe_close.return_value = hedge_close_outcome

    orch = WithdrawOrchestrator(
        chain_writer=chain,
        bybit_client=bybit,
        picker=picker,
        redeem_swap=redeem_swap,
        hedge=hedge,
    )
    return orch, chain, bybit, picker, redeem_swap, hedge


def _patch_mantle_poll(credited_micro: int):
    """The orchestrator imports `poll_mantle_usdc_credit` from chain_writer.
    Patch at the orchestrator's import site (where the symbol is bound).
    """
    async def _fake(**kwargs):
        return credited_micro

    return patch(
        "agent.bybit_oracle.withdraw_orchestrator.poll_mantle_usdc_credit",
        new=_fake,
    )


def _status(db, tx_id):
    row = db.execute(
        "SELECT status FROM withdraw_requests WHERE tx_id = ?", (tx_id,)
    ).fetchone()
    return row["status"] if row else None


@pytest.mark.asyncio
async def test_happy_path_usdc_end_to_end(db, event):
    orch, chain, bybit, picker, redeem_swap, hedge = _make_orchestrator(
        credited_micro=49_000_000,
    )

    with _patch_mantle_poll(49_000_000):
        await orch.handle(db, event)

    assert _status(db, 42) == WITHDRAW_CONFIRMED

    # Phase 1: picker queried, hedge close attempted
    picker.pick.assert_awaited_once_with(bybit)
    hedge.maybe_close.assert_awaited_once_with(coin="USDC", amount=Decimal("50"))

    # Phase 2: redeem_swap executed
    redeem_swap.execute.assert_awaited_once()
    src_kwarg = redeem_swap.execute.await_args.args[2]
    assert src_kwarg.product_id == "prod-USDC"
    assert src_kwarg.staked_coin == "USDC"
    assert src_kwarg.redeem_amount == Decimal("50")

    # Phase 3: baseline read, Bybit withdraw, Mantle poll (patched)
    chain.read_usdc_balance.assert_called_with(ATTESTOR_ADDR)
    bybit.withdraw_to_mantle.assert_awaited_once_with(
        coin="USDC", amount="49.5", address=ATTESTOR_ADDR,
    )

    # Phase 4: approve + confirmWithdraw with delivered_micro from row
    chain.approve_usdc.assert_called_once_with(CONTRACT_ADDR, 49_000_000)
    chain.push_confirm_withdraw.assert_called_once_with(42, 49_000_000)

    row = db.execute("SELECT * FROM withdraw_requests WHERE tx_id = 42").fetchone()
    assert row["bybit_withdraw_id"] == "wd-1"
    assert row["delivered_amount"] == 49_000_000
    assert row["mantle_tx_hash"] == "0xconfirm-tx"


@pytest.mark.asyncio
async def test_volatile_path_routes_through_hedged_and_swap(db, event):
    eth_pick = PickedProduct(
        product_id="prod-ETH", target_coin="ETH", estimated_apr=Decimal("3.5")
    )
    orch, _chain, _bybit, _picker, redeem_swap, hedge = _make_orchestrator(
        picked=eth_pick, hedge_close_outcome="closed",
    )

    with _patch_mantle_poll(49_000_000):
        await orch.handle(db, event)

    assert _status(db, 42) == WITHDRAW_CONFIRMED
    hedge.maybe_close.assert_awaited_once_with(coin="ETH", amount=Decimal("50"))
    # redeem_swap got ETH source
    src_kwarg = redeem_swap.execute.await_args.args[2]
    assert src_kwarg.staked_coin == "ETH"


@pytest.mark.asyncio
async def test_resume_from_swap_skipped(db, event):
    """Crash-restart: row already at SWAP_SKIPPED. Hedge + redeem must not
    re-fire; only Mantle withdraw + confirm should run.
    """
    upsert_withdraw_request(db, 42, event.amount, WITHDRAW_RECEIVED)
    db.execute("UPDATE withdraw_requests SET status = ? WHERE tx_id = 42", (WITHDRAW_SWAP_SKIPPED,))
    db.commit()

    orch, chain, bybit, picker, redeem_swap, hedge = _make_orchestrator()

    with _patch_mantle_poll(49_000_000):
        await orch.handle(db, event)

    assert _status(db, 42) == WITHDRAW_CONFIRMED
    picker.pick.assert_not_awaited()
    hedge.maybe_close.assert_not_awaited()
    redeem_swap.execute.assert_not_awaited()
    # Mantle phase still ran
    bybit.withdraw_to_mantle.assert_awaited_once()
    chain.approve_usdc.assert_called_once()
    chain.push_confirm_withdraw.assert_called_once()


@pytest.mark.asyncio
async def test_resume_from_on_mantle_only_finalises(db, event):
    upsert_withdraw_request(db, 42, event.amount, WITHDRAW_RECEIVED)
    db.execute(
        "UPDATE withdraw_requests SET status = ?, delivered_amount = ? "
        "WHERE tx_id = 42",
        (WITHDRAW_ON_MANTLE, 49_500_000),
    )
    db.commit()

    orch, chain, bybit, _picker, _redeem_swap, _hedge = _make_orchestrator()
    await orch.handle(db, event)

    assert _status(db, 42) == WITHDRAW_CONFIRMED
    bybit.withdraw_to_mantle.assert_not_awaited()
    chain.approve_usdc.assert_called_once_with(CONTRACT_ADDR, 49_500_000)
    chain.push_confirm_withdraw.assert_called_once_with(42, 49_500_000)


@pytest.mark.asyncio
async def test_mantle_baseline_is_snapshot_before_bybit_call(db, event):
    """The baseline read must precede the Bybit withdraw call — otherwise the
    Mantle poll could see the credit already and return delta=0.
    """
    orch, chain, bybit, _picker, _redeem_swap, _hedge = _make_orchestrator()

    call_order: list[str] = []
    chain.read_usdc_balance.side_effect = lambda _addr: (call_order.append("read"), 1_000_000)[1]

    async def withdraw_then_record(*a, **kw):
        call_order.append("bybit_withdraw")
        return WithdrawResult(id="wd-1")

    bybit.withdraw_to_mantle.side_effect = withdraw_then_record

    with _patch_mantle_poll(49_000_000):
        await orch.handle(db, event)

    # read must come before bybit_withdraw in the call sequence.
    read_idx = call_order.index("read")
    bybit_idx = call_order.index("bybit_withdraw")
    assert read_idx < bybit_idx, f"baseline must precede Bybit call: {call_order}"


@pytest.mark.asyncio
async def test_chain_send_error_on_confirm_marks_failed(db, event):
    """confirmWithdraw reverts → row → WITHDRAW_FAILED."""
    orch, chain, *_ = _make_orchestrator()
    chain.push_confirm_withdraw.side_effect = ChainSendError("simulated revert")

    with _patch_mantle_poll(49_000_000), pytest.raises(ChainSendError):
        await orch.handle(db, event)

    assert _status(db, 42) == WITHDRAW_FAILED
    row = db.execute("SELECT * FROM withdraw_requests WHERE tx_id = 42").fetchone()
    assert row["retry_count"] == 1
    assert "simulated revert" in row["last_error"]


@pytest.mark.asyncio
async def test_bybit_order_error_in_redeem_marks_failed(db, event):
    orch, _chain, _bybit, _picker, redeem_swap, _hedge = _make_orchestrator()
    redeem_swap.execute.side_effect = BybitOrderError("redeem rejected")

    with pytest.raises(BybitOrderError):
        await orch.handle(db, event)

    assert _status(db, 42) == WITHDRAW_FAILED


@pytest.mark.asyncio
async def test_no_product_available_marks_failed(db, event):
    orch, *_, picker, _redeem_swap, _hedge = _make_orchestrator()
    picker.pick.side_effect = NoProductAvailable("nothing enabled")

    with pytest.raises(NoProductAvailable):
        await orch.handle(db, event)
    assert _status(db, 42) == WITHDRAW_FAILED


@pytest.mark.asyncio
async def test_timeout_on_mantle_credit_marks_failed(db, event):
    orch, *_ = _make_orchestrator()

    async def timeout_poll(**kwargs):
        raise TimeoutError("mantle USDC not credited")

    with patch(
        "agent.bybit_oracle.withdraw_orchestrator.poll_mantle_usdc_credit",
        new=timeout_poll,
    ), pytest.raises(TimeoutError):
        await orch.handle(db, event)

    assert _status(db, 42) == WITHDRAW_FAILED
    row = db.execute(
        "SELECT last_error FROM withdraw_requests WHERE tx_id = 42"
    ).fetchone()
    assert "not credited" in row["last_error"]


@pytest.mark.asyncio
async def test_idempotent_replay_after_confirmed(db, event):
    orch, chain, bybit, picker, redeem_swap, hedge = _make_orchestrator()

    with _patch_mantle_poll(49_000_000):
        await orch.handle(db, event)
    assert _status(db, 42) == WITHDRAW_CONFIRMED

    chain.reset_mock()
    bybit.reset_mock()
    picker.reset_mock()
    redeem_swap.reset_mock()
    hedge.reset_mock()

    await orch.handle(db, event)
    assert _status(db, 42) == WITHDRAW_CONFIRMED

    chain.approve_usdc.assert_not_called()
    chain.push_confirm_withdraw.assert_not_called()
    bybit.withdraw_to_mantle.assert_not_awaited()
    picker.pick.assert_not_awaited()
    redeem_swap.execute.assert_not_awaited()
    hedge.maybe_close.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_re_queries_picker_when_state_lost(db, event):
    """Fresh orchestrator instance hitting a row already at HEDGE_CLOSE_SKIPPED
    — `_last_picked` was never set, so redeem_swap phase must re-pick.
    """
    upsert_withdraw_request(db, 42, event.amount, WITHDRAW_RECEIVED)
    db.execute(
        "UPDATE withdraw_requests SET status = ? WHERE tx_id = 42",
        (WITHDRAW_HEDGE_CLOSE_SKIPPED,),
    )
    db.commit()

    orch, _chain, _bybit, picker, _redeem_swap, _hedge = _make_orchestrator()
    with _patch_mantle_poll(49_000_000):
        await orch.handle(db, event)

    picker.pick.assert_awaited_once()
    assert _status(db, 42) == WITHDRAW_CONFIRMED


@pytest.mark.asyncio
async def test_confirm_phase_raises_when_no_delivered_amount(db, event):
    """Defensive: if the row reached ON_MANTLE without delivered_amount
    (shouldn't happen in normal flow, but DB corruption / manual override),
    the confirm phase must raise instead of pushing 0.
    """
    upsert_withdraw_request(db, 42, event.amount, WITHDRAW_RECEIVED)
    db.execute(
        "UPDATE withdraw_requests SET status = ?, delivered_amount = NULL "
        "WHERE tx_id = 42",
        (WITHDRAW_ON_MANTLE,),
    )
    db.commit()

    orch, *_ = _make_orchestrator()
    with pytest.raises(RuntimeError, match="no delivered_amount"):
        await orch.handle(db, event)
    assert _status(db, 42) == WITHDRAW_FAILED
