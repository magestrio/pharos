"""Redeem-and-swap orchestration for the withdraw flow.

Mirror of `.12e` `swap_stake.py`. Takes a staked Earn position and a redeem
amount; produces USDC available for Bybit→Mantle withdrawal:

  HEDGE_CLOSED | HEDGE_CLOSE_SKIPPED
    → place Earn Redeem order
    → poll Bybit spot wallet until coin credited
  REDEEMED
    → if staked_coin == "USDC": skip → SWAP_SKIPPED, return credited
    → else: Sell market on {coin}USDC, wait fill → SWAPPED_TO_USDC, return delivered

Returns the USDC quantity available for the next phase (.13d Mantle withdrawal).

Pre-condition contract (caller's job): row is at HEDGE_CLOSED or
HEDGE_CLOSE_SKIPPED. The orchestrator's per-phase status guard enforces
this — executor itself doesn't try to recover from arbitrary entry states.

The MVP `FlexibleUsdcPicker` only stakes USDC, so the volatile branch is
dead code in `.15` smoke but matches the structure so an LLM-picker drop-in
in the future works without orchestrator changes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .bybit_client import BybitClient
from .state import (
    WITHDRAW_HEDGE_CLOSE_SKIPPED,
    WITHDRAW_HEDGE_CLOSED,
    WITHDRAW_REDEEMED,
    WITHDRAW_SWAP_SKIPPED,
    WITHDRAW_SWAPPED_TO_USDC,
    advance_withdraw_status,
)
from .structured_log import get_logger

log = get_logger(__name__)


_TRANSIENT = (httpx.HTTPError, ConnectionError, TimeoutError)


@dataclass(frozen=True)
class WithdrawSource:
    """The Earn position to redeem from. Computed by the orchestrator (.13e)
    based on current Bybit positions + requested withdrawal amount.
    """

    product_id: str
    staked_coin: str  # "USDC" or volatile ticker (e.g. "ETH")
    redeem_amount: Decimal  # in staked_coin units


def _link_id(tx_id: int, step: str) -> str:
    """Deterministic per-(tx_id, step) ID so Bybit dedupes on retry."""
    return f"v8004-wd-{tx_id}-{step}"


def _entry_status(conn: sqlite3.Connection, tx_id: int) -> str:
    """Read the row's current status to figure out which of the two valid
    entry points (HEDGE_CLOSED / HEDGE_CLOSE_SKIPPED) we're transitioning
    from. Either flows the same advance to REDEEMED.
    """
    row = conn.execute(
        "SELECT status FROM withdraw_requests WHERE tx_id = ?", (tx_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"no withdraw row for tx_id={tx_id}")
    status = row["status"]
    if status not in (WITHDRAW_HEDGE_CLOSED, WITHDRAW_HEDGE_CLOSE_SKIPPED):
        raise RuntimeError(
            f"redeem_swap entry guard: tx_id={tx_id} status={status!r}, "
            f"expected hedge_closed or hedge_close_skipped"
        )
    return status


class RedeemSwapExecutor:
    def __init__(self, client: BybitClient) -> None:
        self._client = client

    async def execute(
        self,
        conn: sqlite3.Connection,
        tx_id: int,
        source: WithdrawSource,
    ) -> Decimal:
        """Run redeem + optional swap-back. Returns USDC qty available
        for the next phase (Bybit→Mantle withdrawal).
        """
        from_status = _entry_status(conn, tx_id)

        redeem_order_id = await self._place_redeem(
            product_id=source.product_id,
            amount=source.redeem_amount,
            tx_id=tx_id,
        )

        credited = await self._client.poll_redemption_credited(
            coin=source.staked_coin, min_credit=source.redeem_amount
        )
        log.info(
            "redeem_credited",
            extra={
                "tx_id": tx_id,
                "coin": source.staked_coin,
                "credited": str(credited),
                "redeem_order_id": redeem_order_id,
            },
        )

        advance_withdraw_status(
            conn,
            tx_id,
            from_status,
            WITHDRAW_REDEEMED,
            bybit_earn_redeem_id=redeem_order_id,
        )

        if source.staked_coin.upper() == "USDC":
            advance_withdraw_status(
                conn, tx_id, WITHDRAW_REDEEMED, WITHDRAW_SWAP_SKIPPED
            )
            return credited

        # Volatile path: sell on spot, qty in BASE coin.
        symbol = f"{source.staked_coin}USDC"
        swap_order_id = await self._place_swap_sell(
            symbol=symbol, qty=credited, tx_id=tx_id
        )
        delivered_usdc = await self._wait_swap_filled(swap_order_id)

        advance_withdraw_status(
            conn,
            tx_id,
            WITHDRAW_REDEEMED,
            WITHDRAW_SWAPPED_TO_USDC,
            bybit_swap_order_id=swap_order_id,
        )
        return delivered_usdc

    @retry(
        retry=retry_if_exception_type(_TRANSIENT),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _place_redeem(self, *, product_id: str, amount: Decimal, tx_id: int) -> str:
        result = await self._client.redeem_from_earn(
            product_id=product_id,
            amount=str(amount),
            order_link_id=_link_id(tx_id, "redeem"),
        )
        log.info(
            "redeem_order_placed",
            extra={
                "tx_id": tx_id,
                "order_id": result.orderId,
                "product_id": product_id,
                "amount": str(amount),
            },
        )
        return result.orderId

    @retry(
        retry=retry_if_exception_type(_TRANSIENT),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _place_swap_sell(self, *, symbol: str, qty: Decimal, tx_id: int) -> str:
        result = await self._client.place_spot_order(
            symbol=symbol,
            side="Sell",
            qty=str(qty),
            order_type="Market",
            order_link_id=_link_id(tx_id, "swap-back"),
        )
        log.info(
            "swap_back_placed",
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
        """Wait for the sell fill. BybitOrderError (terminal non-Filled)
        propagates without retry.
        """
        return await self._client.poll_spot_order_filled(order_id=order_id)
