from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.bybit_oracle.bybit_client import (
    BybitOrderError,
    DepositChain,
)
from agent.bybit_oracle.chain_writer import ChainSendError
from agent.bybit_oracle.events import DepositRequested
from agent.bybit_oracle.orchestrator import DepositOrchestrator
from agent.bybit_oracle.product_picker import NoProductAvailable, PickedProduct
from agent.bybit_oracle.state import (
    DEPOSIT_CONFIRMED,
    DEPOSIT_FAILED,
    DEPOSIT_ON_BYBIT,
    DEPOSIT_PRODUCT_SELECTED,
    DEPOSIT_RECEIVED,
    DEPOSIT_STAKED,
    open_db,
    upsert_deposit_request,
)


@pytest.fixture
def db(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    yield conn
    conn.close()


@pytest.fixture
def event():
    return DepositRequested(
        tx_id=42,
        amount=100_000_000,  # 100 USDC (6 decimals)
        tx_hash="0xabc",
        log_index=0,
        block_number=1,
    )


def _make_orchestrator(
    *,
    current_attested: int = 0,
    deposit_addr: str = "0xbybit-mantle-addr",
    bridge_delta: Decimal = Decimal("100"),
    picked: PickedProduct | None = None,
    earn_order_id: str = "earn-1",
):
    chain = MagicMock(name="ChainWriter")
    chain.read_attested_balance.return_value = current_attested
    chain.push_confirm_deposit.return_value = "0xtxhash-confirm"
    chain.transfer_usdc.return_value = "0xtxhash-bridge"

    bybit = AsyncMock(name="BybitClient")
    bybit.get_deposit_address.return_value = DepositChain(
        chain="MANTLE", addressDeposit=deposit_addr
    )
    bybit.poll_deposit_credited.return_value = bridge_delta

    picker = AsyncMock(name="ProductPicker")
    picker.pick.return_value = picked or PickedProduct(
        product_id="prod-USDC-flex",
        target_coin="USDC",
        estimated_apr=Decimal("4.5"),
    )

    # SwapStakeExecutor is the only collaborator that writes to the SQLite
    # FSM itself (its `execute` advances PRODUCT_SELECTED → STAKED). Mock
    # it to mimic that: take conn, advance the row.
    swap_stake = AsyncMock(name="SwapStakeExecutor")

    async def fake_execute(conn, tx_id, picked, source_amount_usdc):
        from agent.bybit_oracle.state import (
            DEPOSIT_PRODUCT_SELECTED,
            DEPOSIT_STAKED,
            advance_deposit_status,
        )
        advance_deposit_status(
            conn,
            tx_id,
            DEPOSIT_PRODUCT_SELECTED,
            DEPOSIT_STAKED,
            bybit_earn_order_id=earn_order_id,
        )
        return earn_order_id

    swap_stake.execute.side_effect = fake_execute

    orch = DepositOrchestrator(
        chain_writer=chain,
        bybit_client=bybit,
        picker=picker,
        swap_stake=swap_stake,
    )
    return orch, chain, bybit, picker, swap_stake


def _status(db, tx_id):
    row = db.execute(
        "SELECT status FROM deposit_requests WHERE tx_id = ?", (tx_id,)
    ).fetchone()
    return row["status"] if row else None


@pytest.mark.asyncio
async def test_happy_path_end_to_end(db, event):
    orch, chain, bybit, picker, swap_stake = _make_orchestrator(current_attested=0)

    await orch.handle(db, event)

    assert _status(db, 42) == DEPOSIT_CONFIRMED

    # Phase 1: confirmDeposit with newAttested = 0 + 100_000_000
    chain.read_attested_balance.assert_called_once()
    chain.push_confirm_deposit.assert_called_once_with(42, 100_000_000)

    # Phase 2: address resolve + USDC transfer + credit poll
    bybit.get_deposit_address.assert_awaited_once_with(coin="USDC", chain="MANTLE")
    chain.transfer_usdc.assert_called_once_with("0xbybit-mantle-addr", 100_000_000)
    bybit.poll_deposit_credited.assert_awaited_once_with(
        coin="USDC", min_credit=Decimal("100")
    )

    # Phase 3 + 4
    picker.pick.assert_awaited_once_with(bybit)
    swap_stake.execute.assert_awaited_once()
    call_kwargs = swap_stake.execute.await_args.kwargs
    assert call_kwargs["tx_id"] == 42
    assert call_kwargs["source_amount_usdc"] == Decimal("100")

    # Row carries the deposit address persisted in phase 2.
    row = db.execute("SELECT * FROM deposit_requests WHERE tx_id = 42").fetchone()
    assert row["bybit_deposit_address"] == "0xbybit-mantle-addr"
    assert row["bybit_earn_order_id"] == "earn-1"


@pytest.mark.asyncio
async def test_confirm_deposit_uses_current_attested_plus_amount(db, event):
    """If the contract already has 200 USDC attested from a prior cycle,
    confirmDeposit must push 200 + new amount.
    """
    orch, chain, *_ = _make_orchestrator(current_attested=200_000_000)
    await orch.handle(db, event)
    chain.push_confirm_deposit.assert_called_once_with(42, 300_000_000)


@pytest.mark.asyncio
async def test_resume_from_on_bybit_skips_first_two_phases(db, event):
    """Crash-restart: row already at ON_BYBIT. Only product+swap+hedge+confirm
    should run; no escrow withdraw, no bridge.
    """
    upsert_deposit_request(db, tx_id=42, amount=event.amount, status=DEPOSIT_RECEIVED)
    db.execute(
        "UPDATE deposit_requests SET status = ? WHERE tx_id = 42",
        (DEPOSIT_ON_BYBIT,),
    )
    db.commit()

    orch, chain, bybit, picker, swap_stake = _make_orchestrator()
    await orch.handle(db, event)

    assert _status(db, 42) == DEPOSIT_CONFIRMED
    chain.push_confirm_deposit.assert_not_called()
    chain.transfer_usdc.assert_not_called()
    bybit.poll_deposit_credited.assert_not_awaited()
    bybit.get_deposit_address.assert_not_awaited()
    picker.pick.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_from_staked_only_runs_tail(db, event):
    """Row already STAKED. Only hedge skip + confirm should fire."""
    upsert_deposit_request(db, tx_id=42, amount=event.amount, status=DEPOSIT_RECEIVED)
    db.execute(
        "UPDATE deposit_requests SET status = ? WHERE tx_id = 42",
        (DEPOSIT_STAKED,),
    )
    db.commit()

    orch, chain, bybit, picker, swap_stake = _make_orchestrator()
    await orch.handle(db, event)

    assert _status(db, 42) == DEPOSIT_CONFIRMED
    chain.push_confirm_deposit.assert_not_called()
    swap_stake.execute.assert_not_awaited()
    picker.pick.assert_not_awaited()


@pytest.mark.asyncio
async def test_chain_send_error_marks_failed(db, event):
    """confirmDeposit reverts → row should land at FAILED, not stuck at
    RECEIVED. Caller (listener) catches the re-raised exception.
    """
    orch, chain, *_ = _make_orchestrator()
    chain.push_confirm_deposit.side_effect = ChainSendError("simulated revert")

    with pytest.raises(ChainSendError):
        await orch.handle(db, event)

    assert _status(db, 42) == DEPOSIT_FAILED
    row = db.execute("SELECT * FROM deposit_requests WHERE tx_id = 42").fetchone()
    assert row["retry_count"] == 1
    assert "simulated revert" in row["last_error"]


@pytest.mark.asyncio
async def test_bybit_order_error_marks_failed_mid_flight(db, event):
    """BybitOrderError from swap_stake (e.g. Earn product paused). FSM was
    advanced to PRODUCT_SELECTED before the failure — must end at FAILED.
    """
    orch, _chain, _bybit, _picker, swap_stake = _make_orchestrator()
    swap_stake.execute.side_effect = BybitOrderError("product paused")

    with pytest.raises(BybitOrderError):
        await orch.handle(db, event)

    assert _status(db, 42) == DEPOSIT_FAILED


@pytest.mark.asyncio
async def test_no_product_available_marks_failed(db, event):
    orch, _chain, _bybit, picker, _swap_stake = _make_orchestrator()
    picker.pick.side_effect = NoProductAvailable("none enabled")

    with pytest.raises(NoProductAvailable):
        await orch.handle(db, event)

    assert _status(db, 42) == DEPOSIT_FAILED


@pytest.mark.asyncio
async def test_idempotent_replay_after_confirmed(db, event):
    """handle() called twice — second call should no-op (row at CONFIRMED,
    every phase guard short-circuits) and NOT re-call any infra.
    """
    orch, chain, bybit, picker, swap_stake = _make_orchestrator()
    await orch.handle(db, event)
    assert _status(db, 42) == DEPOSIT_CONFIRMED

    # Reset call counts and replay.
    chain.reset_mock()
    bybit.reset_mock()
    picker.reset_mock()
    swap_stake.reset_mock()

    await orch.handle(db, event)
    assert _status(db, 42) == DEPOSIT_CONFIRMED

    chain.push_confirm_deposit.assert_not_called()
    chain.transfer_usdc.assert_not_called()
    bybit.poll_deposit_credited.assert_not_awaited()
    bybit.get_deposit_address.assert_not_awaited()
    picker.pick.assert_not_awaited()
    swap_stake.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_volatile_picker_passes_target_coin_to_swap_stake(db, event):
    """Future LLM-picker may return ETH (or similar). Orchestrator must pass
    the PickedProduct through to swap_stake unchanged — that module decides
    whether to swap.
    """
    eth_pick = PickedProduct(
        product_id="prod-ETH-flex", target_coin="ETH", estimated_apr=Decimal("3.5")
    )
    orch, _chain, _bybit, _picker, swap_stake = _make_orchestrator(picked=eth_pick)

    await orch.handle(db, event)

    forwarded = swap_stake.execute.await_args.kwargs["picked"]
    assert forwarded.target_coin == "ETH"
    assert forwarded.product_id == "prod-ETH-flex"


@pytest.mark.asyncio
async def test_resume_re_queries_picker_when_orchestrator_lost_state(db, event):
    """If the bot restarted between PRODUCT_SELECTED advance and swap_stake
    call, the in-memory `_last_picked` is lost. Orchestrator must re-pick
    rather than crash on missing state.
    """
    upsert_deposit_request(db, tx_id=42, amount=event.amount, status=DEPOSIT_RECEIVED)
    db.execute(
        "UPDATE deposit_requests SET status = ? WHERE tx_id = 42",
        (DEPOSIT_PRODUCT_SELECTED,),
    )
    db.commit()

    orch, _chain, _bybit, picker, swap_stake = _make_orchestrator()
    # Fresh orchestrator instance has no _last_picked attribute.
    await orch.handle(db, event)

    assert _status(db, 42) == DEPOSIT_CONFIRMED
    # Picker queried once during resume.
    picker.pick.assert_awaited_once()
    swap_stake.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_after_escrow_withdrawn_lands_at_failed(db, event):
    """Failure during bridge phase (after ESCROW_WITHDRAWN advance) — the row
    transitions ESCROW_WITHDRAWN → FAILED via the per-state allowed set.
    Verifies the catch-all FAILED edge from each non-terminal state.
    """
    orch, _chain, bybit, *_ = _make_orchestrator()
    bybit.poll_deposit_credited.side_effect = TimeoutError("not credited in time")

    with pytest.raises(TimeoutError):
        await orch.handle(db, event)

    assert _status(db, 42) == DEPOSIT_FAILED
    row = db.execute("SELECT last_error FROM deposit_requests WHERE tx_id = 42").fetchone()
    assert "not credited" in row["last_error"]
