from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.bybit_oracle.bybit_client import (
    BybitOrderError,
    EarnOrderResult,
    SpotOrderResult,
)
from agent.bybit_oracle.redeem_swap import (
    RedeemSwapExecutor,
    WithdrawSource,
    _link_id,
)
from agent.bybit_oracle.state import (
    WITHDRAW_HEDGE_CLOSE_SKIPPED,
    WITHDRAW_HEDGE_CLOSED,
    WITHDRAW_RECEIVED,
    WITHDRAW_REDEEMED,
    WITHDRAW_SWAP_SKIPPED,
    WITHDRAW_SWAPPED_TO_USDC,
    open_db,
    upsert_withdraw_request,
)


@pytest.fixture
def db(tmp_path: Path):
    conn = open_db(tmp_path / "test.sqlite")
    yield conn
    conn.close()


def _seed_at(db, tx_id, status):
    upsert_withdraw_request(db, tx_id=tx_id, amount=50_000_000, status=WITHDRAW_RECEIVED)
    db.execute(
        "UPDATE withdraw_requests SET status = ? WHERE tx_id = ?", (status, tx_id)
    )
    db.commit()
    return db


def _row(conn, tx_id=1):
    return conn.execute(
        "SELECT * FROM withdraw_requests WHERE tx_id = ?", (tx_id,)
    ).fetchone()


def _client(
    *,
    redeem_order_id: str = "redeem-1",
    credited: Decimal = Decimal("50"),
    swap_order_id: str = "swap-back-1",
    delivered_usdc: Decimal = Decimal("49.5"),
) -> AsyncMock:
    c = AsyncMock()
    c.redeem_from_earn.return_value = EarnOrderResult(orderId=redeem_order_id)
    c.poll_redemption_credited.return_value = credited
    c.place_spot_order.return_value = SpotOrderResult(orderId=swap_order_id)
    c.poll_spot_order_filled.return_value = delivered_usdc
    return c


@pytest.mark.asyncio
async def test_usdc_path_skips_swap_from_hedge_close_skipped(db):
    _seed_at(db, 1, WITHDRAW_HEDGE_CLOSE_SKIPPED)
    client = _client(credited=Decimal("50"))

    delivered = await RedeemSwapExecutor(client).execute(
        db,
        tx_id=1,
        source=WithdrawSource(
            product_id="prod-USDC", staked_coin="USDC", redeem_amount=Decimal("50")
        ),
    )

    assert delivered == Decimal("50")
    # Spot was NOT called for USDC path.
    client.place_spot_order.assert_not_awaited()
    client.poll_spot_order_filled.assert_not_awaited()
    # Redeem went out with correct args + linkId.
    client.redeem_from_earn.assert_awaited_once_with(
        category="FlexibleSaving",
        product_id="prod-USDC",
        amount="50",
        coin="USDC",
        account_type="FUND",
        order_link_id=_link_id(1, "redeem"),
    )

    row = _row(db)
    assert row["status"] == WITHDRAW_SWAP_SKIPPED
    assert row["bybit_earn_redeem_id"] == "redeem-1"
    assert row["bybit_swap_order_id"] is None


@pytest.mark.asyncio
async def test_executor_handles_both_entry_statuses(db):
    """USDC path entering from HEDGE_CLOSED (vs the common HEDGE_CLOSE_SKIPPED)
    must still work — both are valid entry points to REDEEMED per FSM.
    Doesn't co-occur with MVP picker but the executor must handle uniformly.
    """
    _seed_at(db, 1, WITHDRAW_HEDGE_CLOSED)
    client = _client()

    delivered = await RedeemSwapExecutor(client).execute(
        db,
        tx_id=1,
        source=WithdrawSource(
            product_id="p", staked_coin="USDC", redeem_amount=Decimal("50")
        ),
    )

    assert delivered == Decimal("50")
    assert _row(db)["status"] == WITHDRAW_SWAP_SKIPPED


@pytest.mark.asyncio
async def test_volatile_path_redeems_then_sells(db):
    _seed_at(db, 1, WITHDRAW_HEDGE_CLOSED)
    client = _client(
        credited=Decimal("0.025"),  # ETH redeemed
        delivered_usdc=Decimal("49.5"),  # after spot Sell ETH→USDC
    )

    delivered = await RedeemSwapExecutor(client).execute(
        db,
        tx_id=1,
        source=WithdrawSource(
            product_id="prod-ETH", staked_coin="ETH", redeem_amount=Decimal("0.025")
        ),
    )

    assert delivered == Decimal("49.5")

    # Redeem went out for ETH
    client.redeem_from_earn.assert_awaited_once_with(
        category="FlexibleSaving",
        product_id="prod-ETH",
        amount="0.025",
        coin="ETH",
        account_type="FUND",
        order_link_id=_link_id(1, "redeem"),
    )
    # Polled the ETH spot wallet
    client.poll_redemption_credited.assert_awaited_once_with(
        coin="ETH", min_credit=Decimal("0.025")
    )
    # Sold ETH for USDC (qty in base coin = redeemed amount)
    client.place_spot_order.assert_awaited_once_with(
        symbol="ETHUSDC",
        side="Sell",
        qty_base="0.025",
        order_type="Market",
        order_link_id=_link_id(1, "swap-back"),
    )

    row = _row(db)
    assert row["status"] == WITHDRAW_SWAPPED_TO_USDC
    assert row["bybit_earn_redeem_id"] == "redeem-1"
    assert row["bybit_swap_order_id"] == "swap-back-1"


@pytest.mark.asyncio
async def test_usdc_case_insensitive(db):
    """Lowercase `usdc` from a non-canonical Bybit response shouldn't trigger
    a swap-against-itself.
    """
    _seed_at(db, 1, WITHDRAW_HEDGE_CLOSE_SKIPPED)
    client = _client()

    await RedeemSwapExecutor(client).execute(
        db,
        tx_id=1,
        source=WithdrawSource(
            product_id="p", staked_coin="usdc", redeem_amount=Decimal("50")
        ),
    )
    client.place_spot_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_guard_rejects_wrong_status(db):
    _seed_at(db, 1, WITHDRAW_REDEEMED)  # already past entry point
    client = _client()

    with pytest.raises(RuntimeError, match="entry guard"):
        await RedeemSwapExecutor(client).execute(
            db,
            tx_id=1,
            source=WithdrawSource(
                product_id="p", staked_coin="USDC", redeem_amount=Decimal("50")
            ),
        )


@pytest.mark.asyncio
async def test_entry_guard_rejects_missing_row(db):
    client = _client()
    with pytest.raises(RuntimeError, match="no withdraw row"):
        await RedeemSwapExecutor(client).execute(
            db,
            tx_id=999,
            source=WithdrawSource(
                product_id="p", staked_coin="USDC", redeem_amount=Decimal("50")
            ),
        )


@pytest.mark.asyncio
async def test_transient_http_error_on_redeem_retried(db):
    _seed_at(db, 1, WITHDRAW_HEDGE_CLOSE_SKIPPED)
    client = _client()
    client.redeem_from_earn.side_effect = [
        httpx.ConnectError("blip"),
        EarnOrderResult(orderId="redeem-after-retry"),
    ]

    executor = RedeemSwapExecutor(client)
    executor._place_redeem.retry.wait = lambda *_a, **_k: 0  # type: ignore[attr-defined]

    delivered = await executor.execute(
        db,
        tx_id=1,
        source=WithdrawSource(
            product_id="p", staked_coin="USDC", redeem_amount=Decimal("50")
        ),
    )

    assert delivered == Decimal("50")
    assert client.redeem_from_earn.await_count == 2
    link_ids = {
        call.kwargs["order_link_id"] for call in client.redeem_from_earn.await_args_list
    }
    assert link_ids == {_link_id(1, "redeem")}


@pytest.mark.asyncio
async def test_bybit_order_error_on_sell_propagates_without_advance(db):
    """Sell rejected (e.g. min-notional, exchange halted) → BybitOrderError
    bubbles up, row stays at REDEEMED (not advanced to SWAPPED_TO_USDC).
    Orchestrator catches and marks FAILED.
    """
    _seed_at(db, 1, WITHDRAW_HEDGE_CLOSED)
    client = _client(credited=Decimal("0.025"))
    client.poll_spot_order_filled.side_effect = BybitOrderError("min notional")

    executor = RedeemSwapExecutor(client)
    executor._wait_swap_filled.retry.wait = lambda *_a, **_k: 0  # type: ignore[attr-defined]

    with pytest.raises(BybitOrderError, match="min notional"):
        await executor.execute(
            db,
            tx_id=1,
            source=WithdrawSource(
                product_id="p", staked_coin="ETH", redeem_amount=Decimal("0.025")
            ),
        )

    # Row stopped at REDEEMED — swap_back didn't complete.
    assert _row(db)["status"] == WITHDRAW_REDEEMED
    assert _row(db)["bybit_earn_redeem_id"] == "redeem-1"
    assert _row(db)["bybit_swap_order_id"] is None


@pytest.mark.asyncio
async def test_volatile_uses_credited_qty_not_requested_amount(db):
    """If we requested 0.025 ETH redeem but actually got 0.0249 (rounding,
    slippage), the Sell order must use the ACTUAL credited qty, otherwise
    Bybit rejects with insufficient balance.
    """
    _seed_at(db, 1, WITHDRAW_HEDGE_CLOSED)
    client = _client(credited=Decimal("0.0249"))

    await RedeemSwapExecutor(client).execute(
        db,
        tx_id=1,
        source=WithdrawSource(
            product_id="p", staked_coin="ETH", redeem_amount=Decimal("0.025")
        ),
    )

    # Sell order uses 0.0249 (credited), not 0.025 (requested).
    sell_call = client.place_spot_order.await_args
    assert sell_call.kwargs["qty_base"] == "0.0249"
