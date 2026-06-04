from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.bybit_oracle.bybit_client import BybitOrderError, EarnOrderResult, SpotOrderResult
from agent.bybit_oracle.product_picker import PickedProduct
from agent.bybit_oracle.state import (
    DEPOSIT_PRODUCT_SELECTED,
    DEPOSIT_RECEIVED,
    DEPOSIT_STAKED,
    DEPOSIT_SWAPPED,
    open_db,
    upsert_deposit_request,
)
from agent.bybit_oracle.swap_stake import SwapStakeExecutor, _link_id


@pytest.fixture
def db(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    yield conn
    conn.close()


@pytest.fixture
def deposit_at_product_selected(db):
    """Seed a deposit row already advanced to PRODUCT_SELECTED, which is
    swap_stake's precondition.
    """
    upsert_deposit_request(db, tx_id=1, amount=100_000_000, status=DEPOSIT_RECEIVED)
    db.execute(
        "UPDATE deposit_requests SET status = ? WHERE tx_id = 1",
        (DEPOSIT_PRODUCT_SELECTED,),
    )
    db.commit()
    return db


def _row(conn, tx_id: int = 1):
    return conn.execute(
        "SELECT * FROM deposit_requests WHERE tx_id = ?", (tx_id,)
    ).fetchone()


def _mock_client(*, swap_qty: str = "0.5", swap_order_id: str = "swap-1",
                 earn_order_id: str = "earn-1") -> AsyncMock:
    client = AsyncMock()
    client.place_spot_order.return_value = SpotOrderResult(orderId=swap_order_id)
    client.poll_spot_order_filled.return_value = Decimal(swap_qty)
    client.place_earn_order.return_value = EarnOrderResult(orderId=earn_order_id)
    return client


@pytest.mark.asyncio
async def test_usdc_path_skips_swap(deposit_at_product_selected):
    client = _mock_client()
    picked = PickedProduct(
        product_id="prod-USDC-flex", target_coin="USDC", estimated_apr=Decimal("4.5")
    )

    order_id = await SwapStakeExecutor(client).execute(
        deposit_at_product_selected, tx_id=1, picked=picked,
        source_amount_usdc=Decimal("100"),
    )

    assert order_id == "earn-1"
    client.place_spot_order.assert_not_awaited()
    client.poll_spot_order_filled.assert_not_awaited()
    client.place_earn_order.assert_awaited_once_with(
        category="FlexibleSaving",
        product_id="prod-USDC-flex",
        amount="100",
        side="Stake",
        coin="USDC",
        account_type="FUND",
        order_link_id=_link_id(1, "stake"),
    )

    row = _row(deposit_at_product_selected)
    assert row["status"] == DEPOSIT_STAKED
    assert row["bybit_earn_order_id"] == "earn-1"
    assert row["bybit_swap_order_id"] is None


@pytest.mark.asyncio
async def test_usdc_path_case_insensitive(deposit_at_product_selected):
    """Lowercase `usdc` from a non-canonical Bybit response shouldn't trigger
    a swap-against-itself.
    """
    client = _mock_client()
    picked = PickedProduct(
        product_id="p1", target_coin="usdc", estimated_apr=Decimal("3.0")
    )
    await SwapStakeExecutor(client).execute(
        deposit_at_product_selected, 1, picked, Decimal("50")
    )
    client.place_spot_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_volatile_path_swaps_then_stakes(deposit_at_product_selected):
    client = _mock_client(swap_qty="0.025", earn_order_id="earn-eth-1")
    picked = PickedProduct(
        product_id="prod-ETH-flex", target_coin="ETH", estimated_apr=Decimal("3.2")
    )

    order_id = await SwapStakeExecutor(client).execute(
        deposit_at_product_selected, tx_id=1, picked=picked,
        source_amount_usdc=Decimal("100"),
    )

    assert order_id == "earn-eth-1"
    client.place_spot_order.assert_awaited_once_with(
        symbol="ETHUSDC",
        side="Buy",
        qty_quote="100",
        order_type="Market",
        order_link_id=_link_id(1, "swap"),
    )
    client.poll_spot_order_filled.assert_awaited_once_with(order_id="swap-1")
    # Earn stake gets the FILLED qty, NOT the original USDC amount.
    # And the staked coin is the swapped-into ETH, not the source USDC.
    client.place_earn_order.assert_awaited_once_with(
        category="FlexibleSaving",
        product_id="prod-ETH-flex",
        amount="0.025",
        side="Stake",
        coin="ETH",
        account_type="FUND",
        order_link_id=_link_id(1, "stake"),
    )

    row = _row(deposit_at_product_selected)
    assert row["status"] == DEPOSIT_STAKED
    assert row["bybit_swap_order_id"] == "swap-1"
    assert row["bybit_earn_order_id"] == "earn-eth-1"


@pytest.mark.asyncio
async def test_volatile_path_advances_through_swapped(deposit_at_product_selected):
    """Even though the test sees only the final state, the FSM rejects
    PRODUCT_SELECTED→STAKED for the volatile path — the swap step's transition
    to SWAPPED must happen first. Verify by inspecting state mid-flight via
    a side_effect callback that records the row's status between the swap and
    stake calls.
    """
    captured_status: list[str] = []
    client = _mock_client(swap_qty="0.5", earn_order_id="e")

    async def capture_then_stake(**kwargs):
        captured_status.append(_row(deposit_at_product_selected)["status"])
        return EarnOrderResult(orderId="e")

    client.place_earn_order.side_effect = capture_then_stake

    picked = PickedProduct(
        product_id="p", target_coin="ETH", estimated_apr=Decimal("3")
    )
    await SwapStakeExecutor(client).execute(
        deposit_at_product_selected, 1, picked, Decimal("100")
    )

    assert captured_status == [DEPOSIT_SWAPPED], (
        "row should be at SWAPPED when stake is called (proves we transitioned through it)"
    )


@pytest.mark.asyncio
async def test_transient_http_error_retried(deposit_at_product_selected):
    """First call to place_earn_order raises httpx error, second succeeds.
    Tenacity should retry. orderLinkId stays the same so Bybit dedupes.
    """
    client = _mock_client()
    client.place_earn_order.side_effect = [
        httpx.ConnectError("blip"),
        EarnOrderResult(orderId="earn-after-retry"),
    ]

    picked = PickedProduct(
        product_id="p", target_coin="USDC", estimated_apr=Decimal("4")
    )
    executor = SwapStakeExecutor(client)
    # Shrink tenacity wait for test speed.
    executor._place_earn_order.retry.wait = lambda *_a, **_k: 0  # type: ignore[attr-defined]

    order_id = await executor.execute(
        deposit_at_product_selected, 1, picked, Decimal("50")
    )

    assert order_id == "earn-after-retry"
    assert client.place_earn_order.await_count == 2
    # Same orderLinkId on both attempts → Bybit-side idempotency.
    link_ids = {call.kwargs["order_link_id"] for call in client.place_earn_order.await_args_list}
    assert link_ids == {_link_id(1, "stake")}


@pytest.mark.asyncio
async def test_bybit_order_error_not_retried(deposit_at_product_selected):
    """BybitOrderError on stake (e.g. Earn product paused) is terminal —
    don't retry, propagate so caller can mark FAILED.
    """
    client = _mock_client()
    client.place_earn_order.side_effect = BybitOrderError("product paused")

    picked = PickedProduct(
        product_id="p", target_coin="USDC", estimated_apr=Decimal("4")
    )
    executor = SwapStakeExecutor(client)
    executor._place_earn_order.retry.wait = lambda *_a, **_k: 0  # type: ignore[attr-defined]

    with pytest.raises(BybitOrderError, match="product paused"):
        await executor.execute(deposit_at_product_selected, 1, picked, Decimal("50"))

    assert client.place_earn_order.await_count == 1
    # FSM still at PRODUCT_SELECTED — caller decides next step (failed/retry).
    assert _row(deposit_at_product_selected)["status"] == DEPOSIT_PRODUCT_SELECTED


@pytest.mark.asyncio
async def test_idempotent_replay_after_stake_done(deposit_at_product_selected):
    """If execute() runs again after the row already reached STAKED (because
    a prior crash-restart already finished), the advance call returns False
    and we don't unstake or panic — we surface the (still-correct) order_id.
    """
    # Pre-advance to STAKED with a known order_id, simulating prior successful run.
    deposit_at_product_selected.execute(
        "UPDATE deposit_requests SET status = ?, bybit_earn_order_id = ? WHERE tx_id = 1",
        (DEPOSIT_STAKED, "earn-from-prior-run"),
    )
    deposit_at_product_selected.commit()

    client = _mock_client(earn_order_id="earn-from-replay")
    picked = PickedProduct(
        product_id="p", target_coin="USDC", estimated_apr=Decimal("4")
    )
    # execute() will dispatch a new stake call (it doesn't read prior state —
    # that's the orchestrator's job in .12f). It returns the NEW order_id but
    # FSM advance is a no-op (row already at STAKED).
    order_id = await SwapStakeExecutor(client).execute(
        deposit_at_product_selected, 1, picked, Decimal("50")
    )
    assert order_id == "earn-from-replay"
    # bybit_earn_order_id NOT overwritten — advance returned False (no UPDATE).
    row = _row(deposit_at_product_selected)
    assert row["bybit_earn_order_id"] == "earn-from-prior-run"
