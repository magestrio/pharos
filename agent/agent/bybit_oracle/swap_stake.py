"""Swap-and-stake orchestration for the deposit flow.

Takes USDC sitting in the Bybit wallet (just bridged in by `.12c`) plus a
`PickedProduct` from `.12d`, and:

  1. If `target_coin == "USDC"` — skip swap, advance FSM directly to staked.
  2. Else — place a spot market Buy on `{target_coin}USDC`, wait for fill,
     advance to swapped, then stake the filled qty.

State is persisted via `.12a` FSM helpers after each external step so a
crash leaves the row at the last-completed boundary. `orderLinkId` derived
from `tx_id` makes a Bybit-side retry idempotent (Bybit dedupes by linkId).

The MVP `FlexibleUsdcPicker` always returns USDC, so the swap branch is dead
code in `.15` smoke but lives behind the same surface for the LLM picker.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .bybit_client import BybitClient, BybitOrderError
from .product_picker import PickedProduct
from .state import (
    DEPOSIT_PRODUCT_SELECTED,
    DEPOSIT_STAKED,
    DEPOSIT_SWAPPED,
    advance_deposit_status,
)
from .structured_log import get_logger

log = get_logger(__name__)


_TRANSIENT = (httpx.HTTPError, ConnectionError, TimeoutError)


def _link_id(tx_id: int, step: str) -> str:
    """Deterministic per-(tx_id, step) ID so Bybit dedupes if we retry after
    a network blip ate the response but the order was placed.
    """
    return f"v8004-dep-{tx_id}-{step}"


class SwapStakeExecutor:
    def __init__(self, client: BybitClient) -> None:
        self._client = client

    async def execute(
        self,
        conn: sqlite3.Connection,
        tx_id: int,
        picked: PickedProduct,
        source_amount_usdc: Decimal,
    ) -> str:
        """Run swap (if needed) + stake. Returns the Bybit earn order id on
        success. Raises on any non-recoverable failure — caller advances FSM
        to failed and surfaces.

        Pre-condition: row is at DEPOSIT_PRODUCT_SELECTED. On a fresh crash,
        caller is responsible for checking the row's status BEFORE calling
        execute and skipping if already past STAKED.
        """
        if picked.target_coin.upper() == "USDC":
            return await self._stake_only(conn, tx_id, picked, source_amount_usdc)
        return await self._swap_then_stake(conn, tx_id, picked, source_amount_usdc)

    async def _stake_only(
        self,
        conn: sqlite3.Connection,
        tx_id: int,
        picked: PickedProduct,
        amount: Decimal,
    ) -> str:
        order_id = await self._place_earn_order(picked.product_id, amount, tx_id)
        ok = advance_deposit_status(
            conn,
            tx_id,
            DEPOSIT_PRODUCT_SELECTED,
            DEPOSIT_STAKED,
            bybit_earn_order_id=order_id,
        )
        if not ok:
            # Row wasn't at PRODUCT_SELECTED — either already staked (race
            # with a prior crash-restart) or operator manually advanced.
            # Don't unstake; surface to caller.
            log.warning(
                "swap_stake_advance_no_op",
                extra={"tx_id": tx_id, "step": "stake_only", "earn_order_id": order_id},
            )
        return order_id

    async def _swap_then_stake(
        self,
        conn: sqlite3.Connection,
        tx_id: int,
        picked: PickedProduct,
        source_amount_usdc: Decimal,
    ) -> str:
        # Bybit spot pair convention: BASE / QUOTE. Buy {coin} with USDC =
        # symbol {coin}USDC, side Buy, qty in quote (USDC amount we spend).
        symbol = f"{picked.target_coin}USDC"
        swap_order_id = await self._place_swap_order(
            symbol=symbol, qty_usdc=source_amount_usdc, tx_id=tx_id
        )
        filled_qty = await self._wait_swap_filled(swap_order_id)

        advance_deposit_status(
            conn,
            tx_id,
            DEPOSIT_PRODUCT_SELECTED,
            DEPOSIT_SWAPPED,
            bybit_swap_order_id=swap_order_id,
        )

        earn_order_id = await self._place_earn_order(
            picked.product_id, filled_qty, tx_id
        )
        advance_deposit_status(
            conn,
            tx_id,
            DEPOSIT_SWAPPED,
            DEPOSIT_STAKED,
            bybit_earn_order_id=earn_order_id,
        )
        return earn_order_id

    @retry(
        retry=retry_if_exception_type(_TRANSIENT),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _place_swap_order(
        self, *, symbol: str, qty_usdc: Decimal, tx_id: int
    ) -> str:
        result = await self._client.place_spot_order(
            symbol=symbol,
            side="Buy",
            qty=str(qty_usdc),
            order_type="Market",
            order_link_id=_link_id(tx_id, "swap"),
        )
        log.info(
            "swap_order_placed",
            extra={"tx_id": tx_id, "order_id": result.orderId, "symbol": symbol},
        )
        return result.orderId

    @retry(
        retry=retry_if_exception_type(_TRANSIENT),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _wait_swap_filled(self, order_id: str) -> Decimal:
        """Wait for fill. BybitOrderError (terminal non-Filled) propagates
        without retry — same order won't change state by polling more.
        """
        return await self._client.poll_spot_order_filled(order_id=order_id)

    @retry(
        retry=retry_if_exception_type(_TRANSIENT),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _place_earn_order(
        self, product_id: str, amount: Decimal, tx_id: int
    ) -> str:
        try:
            result = await self._client.place_earn_order(
                product_id=product_id,
                amount=str(amount),
                side="Stake",
                order_link_id=_link_id(tx_id, "stake"),
            )
        except BybitOrderError:
            # Earn-side terminal failures (insufficient bal, lockup) are not
            # transport — don't retry, surface to caller.
            raise
        log.info(
            "earn_order_placed",
            extra={
                "tx_id": tx_id,
                "order_id": result.orderId,
                "product_id": product_id,
                "amount": str(amount),
            },
        )
        return result.orderId
