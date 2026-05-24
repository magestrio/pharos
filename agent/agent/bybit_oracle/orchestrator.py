"""Deposit-flow orchestrator: wires .12a-.12e + .12g into one end-to-end
handler for `DepositRequested` events.

**Flow A — confirmDeposit-FIRST** (decided 2026-05-24, see epic Log for
rationale and alternatives):

  RECEIVED
    → push confirmDeposit(tx_id, currentAttested + amount)  [escrow → wallet]
  ESCROW_WITHDRAWN
    → resolve Bybit deposit address (Mantle/USDC)
    → ERC-20 transfer(usdc, attestor → bybit_addr, amount)
    → poll Bybit wallet credited
  ON_BYBIT
    → picker.pick() → PickedProduct
  PRODUCT_SELECTED
    → swap_stake.execute()  [USDC-only: skip swap; volatile: swap then stake]
  STAKED
    → hedge skip (stub — .12g real impl blocked by hedge-engine)
  HEDGE_SKIPPED
  CONFIRMED

Each phase is gated on the current FSM status: if the row is already past
that phase, the method returns silently. This makes the orchestrator
idempotent across crashes — restart with the same event replays only the
incomplete tail.

Failures advance the row to `failed` and re-raise. Caller (listener loop)
catches at the top level and logs; manual intervention picks up from there.

**Known MVP gap** (carried over from `.12e`): mid-phase crashes within
`swap_then_stake` aren't recoverable here — if the swap order was placed
but `poll_spot_order_filled` died, restart will try to swap *again*. The
`order_link_id` derived from tx_id makes Bybit dedupe the swap itself, but
the orchestrator won't know about the prior placement. Acceptable for `.15`
$50 smoke; document in `.16`.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from decimal import Decimal

from .bybit_client import BybitClient, BybitOrderError
from .chain_writer import ChainSendError, ChainWriter
from .events import DepositRequested
from .hedge import HedgeTrigger, NullHedgeTrigger
from .product_picker import NoProductAvailable, ProductPicker
from .state import (
    DEPOSIT_CONFIRMED,
    DEPOSIT_ESCROW_WITHDRAWN,
    DEPOSIT_FAILED,
    DEPOSIT_HEDGE_SKIPPED,
    DEPOSIT_HEDGED,
    DEPOSIT_ON_BYBIT,
    DEPOSIT_PRODUCT_SELECTED,
    DEPOSIT_RECEIVED,
    DEPOSIT_STAKED,
    advance_deposit_status,
    increment_deposit_retry,
    upsert_deposit_request,
)
from .structured_log import get_logger
from .swap_stake import SwapStakeExecutor

log = get_logger(__name__)


_USDC_DECIMALS = Decimal(10) ** 6


def _micro_to_decimal(micro: int) -> Decimal:
    """uint256 micro-USDC (6 dec) → Decimal USDC string-friendly value."""
    return Decimal(micro) / _USDC_DECIMALS


def _row_status(conn: sqlite3.Connection, tx_id: int) -> str | None:
    row = conn.execute(
        "SELECT status FROM deposit_requests WHERE tx_id = ?", (tx_id,)
    ).fetchone()
    return row["status"] if row else None


class DepositOrchestrator:
    def __init__(
        self,
        chain_writer: ChainWriter,
        bybit_client: BybitClient,
        picker: ProductPicker,
        swap_stake: SwapStakeExecutor,
        hedge: HedgeTrigger | None = None,
    ) -> None:
        self._chain = chain_writer
        self._bybit = bybit_client
        self._picker = picker
        self._swap_stake = swap_stake
        self._hedge: HedgeTrigger = hedge or NullHedgeTrigger()

    async def handle(self, conn: sqlite3.Connection, event: DepositRequested) -> None:
        # Seed/refresh the row. If the row already exists at a later state
        # (crash-restart), upsert leaves status at the more-advanced value
        # because upsert_deposit_request currently *overwrites* status — that
        # would be a bug here. We work around by only inserting when missing.
        if _row_status(conn, event.tx_id) is None:
            upsert_deposit_request(
                conn, event.tx_id, event.amount, status=DEPOSIT_RECEIVED
            )

        try:
            await self._phase_escrow_withdraw(conn, event)
            await self._phase_bridge_to_bybit(conn, event)
            await self._phase_pick_product(conn, event)
            await self._phase_swap_and_stake(conn, event)
            await self._phase_hedge(conn, event)
            await self._phase_confirm(conn, event)
        except (ChainSendError, BybitOrderError, NoProductAvailable, TimeoutError) as exc:
            self._mark_failed(conn, event.tx_id, exc)
            raise

    # --- Phase 1: confirmDeposit on-chain → escrow released to attestor ----

    async def _phase_escrow_withdraw(
        self, conn: sqlite3.Connection, event: DepositRequested
    ) -> None:
        if _row_status(conn, event.tx_id) != DEPOSIT_RECEIVED:
            return

        current = await asyncio.to_thread(self._chain.read_attested_balance)
        new_attested = current + event.amount
        tx_hash = await asyncio.to_thread(
            self._chain.push_confirm_deposit, event.tx_id, new_attested
        )
        log.info(
            "phase_escrow_withdraw_done",
            extra={"tx_id": event.tx_id, "tx_hash": tx_hash, "new_attested": new_attested},
        )
        advance_deposit_status(
            conn, event.tx_id, DEPOSIT_RECEIVED, DEPOSIT_ESCROW_WITHDRAWN
        )

    # --- Phase 2: Mantle USDC → Bybit deposit address → wait credit --------

    async def _phase_bridge_to_bybit(
        self, conn: sqlite3.Connection, event: DepositRequested
    ) -> None:
        if _row_status(conn, event.tx_id) != DEPOSIT_ESCROW_WITHDRAWN:
            return

        address_entry = await self._bybit.get_deposit_address(coin="USDC", chain="MANTLE")
        bybit_addr = address_entry.addressDeposit

        tx_hash = await asyncio.to_thread(
            self._chain.transfer_usdc, bybit_addr, event.amount
        )
        log.info(
            "phase_bridge_sent",
            extra={
                "tx_id": event.tx_id,
                "to": bybit_addr,
                "amount_micro": event.amount,
                "tx_hash": tx_hash,
            },
        )

        min_credit = _micro_to_decimal(event.amount)
        delta = await self._bybit.poll_deposit_credited(
            coin="USDC", min_credit=min_credit
        )
        log.info(
            "phase_bridge_credited",
            extra={"tx_id": event.tx_id, "delta": str(delta)},
        )
        advance_deposit_status(
            conn,
            event.tx_id,
            DEPOSIT_ESCROW_WITHDRAWN,
            DEPOSIT_ON_BYBIT,
            bybit_deposit_address=bybit_addr,
        )

    # --- Phase 3: pick Earn product ----------------------------------------

    async def _phase_pick_product(
        self, conn: sqlite3.Connection, event: DepositRequested
    ) -> None:
        if _row_status(conn, event.tx_id) != DEPOSIT_ON_BYBIT:
            return

        picked = await self._picker.pick(self._bybit)
        # Stash picker output on the orchestrator instance so phase 4 can
        # reuse without re-querying Bybit. Per-instance is safe — each event
        # is handled serially by the listener loop (single-process bot).
        self._last_picked = picked
        advance_deposit_status(
            conn, event.tx_id, DEPOSIT_ON_BYBIT, DEPOSIT_PRODUCT_SELECTED
        )

    # --- Phase 4: swap (if needed) + stake on Earn -------------------------

    async def _phase_swap_and_stake(
        self, conn: sqlite3.Connection, event: DepositRequested
    ) -> None:
        if _row_status(conn, event.tx_id) != DEPOSIT_PRODUCT_SELECTED:
            return

        picked = getattr(self, "_last_picked", None)
        if picked is None:
            # Resume after crash: picker output wasn't persisted. Re-query.
            picked = await self._picker.pick(self._bybit)
            self._last_picked = picked

        source_amount = _micro_to_decimal(event.amount)
        await self._swap_stake.execute(
            conn, tx_id=event.tx_id, picked=picked, source_amount_usdc=source_amount
        )
        # swap_stake.execute already advances the FSM to STAKED.

    # --- Phase 5: hedge (stub — .12g) --------------------------------------

    async def _phase_hedge(
        self, conn: sqlite3.Connection, event: DepositRequested
    ) -> None:
        if _row_status(conn, event.tx_id) != DEPOSIT_STAKED:
            return

        picked = getattr(self, "_last_picked", None)
        coin = picked.target_coin if picked is not None else "USDC"
        amount = _micro_to_decimal(event.amount)

        outcome = await self._hedge.maybe_trigger(coin=coin, amount=amount)
        next_status = DEPOSIT_HEDGED if outcome == "hedged" else DEPOSIT_HEDGE_SKIPPED
        advance_deposit_status(conn, event.tx_id, DEPOSIT_STAKED, next_status)
        log.info(
            "phase_hedge_done",
            extra={"tx_id": event.tx_id, "outcome": outcome, "coin": coin},
        )

    # --- Phase 6: finalize FSM ---------------------------------------------

    async def _phase_confirm(
        self, conn: sqlite3.Connection, event: DepositRequested
    ) -> None:
        current = _row_status(conn, event.tx_id)
        if current not in (DEPOSIT_HEDGE_SKIPPED, DEPOSIT_HEDGED):
            return

        # No on-chain action here — confirmDeposit was already pushed in
        # phase 1 (Flow A). The periodic `.14` updateBalance cron will start
        # reporting yield-grown values; this terminal advance just marks the
        # cycle complete for accounting / dashboards.
        advance_deposit_status(conn, event.tx_id, current, DEPOSIT_CONFIRMED)
        log.info("deposit_cycle_done", extra={"tx_id": event.tx_id, "from": current})

    # --- Failure handling --------------------------------------------------

    def _mark_failed(
        self, conn: sqlite3.Connection, tx_id: int, exc: BaseException
    ) -> None:
        current = _row_status(conn, tx_id)
        if current is None or current in (DEPOSIT_FAILED, DEPOSIT_CONFIRMED):
            # Either no row to update (race) or already terminal — don't
            # clobber. retry counter still gets bumped if row exists.
            if current is not None:
                with contextlib.suppress(LookupError):
                    increment_deposit_retry(conn, tx_id, repr(exc))
            return

        with contextlib.suppress(LookupError):
            increment_deposit_retry(conn, tx_id, repr(exc))
        advance_deposit_status(
            conn, tx_id, current, DEPOSIT_FAILED, last_error=repr(exc)
        )
        log.error(
            "deposit_cycle_failed",
            extra={"tx_id": tx_id, "at_status": current, "err": repr(exc)},
        )
