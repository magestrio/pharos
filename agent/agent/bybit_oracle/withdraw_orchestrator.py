"""Withdraw-flow orchestrator (mirror of `.12f` DepositOrchestrator).

Phases (per `.13a` FSM):

  RECEIVED
    → picker → maybe_close hedge → HEDGE_CLOSED | HEDGE_CLOSE_SKIPPED
  HEDGE_CLOSED | HEDGE_CLOSE_SKIPPED
    → RedeemSwapExecutor.execute()  [redeem from Earn + optional swap back]
      [internally advances REDEEMED → SWAP_SKIPPED | SWAPPED_TO_USDC]
  SWAP_SKIPPED | SWAPPED_TO_USDC
    → snapshot Mantle USDC baseline
    → bybit.withdraw_to_mantle(coin=USDC, amount=delivered, address=attestor)
    → poll_mantle_usdc_credit(...)
  ON_MANTLE
    → approve_usdc(BybitAttestor, delivered_micro)
    → push confirmWithdraw(tx_id, delivered_micro)
  CONFIRMED

Per-phase resume: each method reads current FSM status, skips if past.
Failures advance row to WITHDRAW_FAILED and re-raise.

**Known MVP gaps** (for `.16` docs sweep):
- `_last_picked` / `_last_delivered` are per-instance in-memory. After a
  process restart, picker is re-queried (cheap), and delivered USDC falls
  back to `event.amount` (conservative for the polling threshold — actual
  delivered will be slightly less due to swap-back slippage + Bybit fees).
- `attestor_contract_address` comes from the chain_writer's contract
  instance — same address used for `confirmWithdraw`, so consistency
  guaranteed by construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from decimal import Decimal

from .bybit_client import BybitClient, BybitOrderError
from .chain_writer import ChainSendError, ChainWriter, poll_mantle_usdc_credit
from .events import WithdrawRequested
from .hedge import HedgeTrigger, NullHedgeTrigger
from .product_picker import NoProductAvailable, ProductPicker
from .redeem_swap import RedeemSwapExecutor, WithdrawSource
from .state import (
    WITHDRAW_CONFIRMED,
    WITHDRAW_FAILED,
    WITHDRAW_HEDGE_CLOSE_SKIPPED,
    WITHDRAW_HEDGE_CLOSED,
    WITHDRAW_ON_MANTLE,
    WITHDRAW_RECEIVED,
    WITHDRAW_SWAP_SKIPPED,
    WITHDRAW_SWAPPED_TO_USDC,
    advance_withdraw_status,
    increment_withdraw_retry,
    upsert_withdraw_request,
)
from .structured_log import get_logger

log = get_logger(__name__)


_USDC_DECIMALS = Decimal(10) ** 6


def _micro_to_decimal(micro: int) -> Decimal:
    return Decimal(micro) / _USDC_DECIMALS


def _decimal_to_micro(value: Decimal) -> int:
    """Decimal USDC → uint256 micro. Truncates fractional micro-USDC."""
    return int(value * _USDC_DECIMALS)


def _row_status(conn: sqlite3.Connection, tx_id: int) -> str | None:
    row = conn.execute(
        "SELECT status FROM withdraw_requests WHERE tx_id = ?", (tx_id,)
    ).fetchone()
    return row["status"] if row else None


class WithdrawOrchestrator:
    def __init__(
        self,
        chain_writer: ChainWriter,
        bybit_client: BybitClient,
        picker: ProductPicker,
        redeem_swap: RedeemSwapExecutor,
        hedge: HedgeTrigger | None = None,
    ) -> None:
        self._chain = chain_writer
        self._bybit = bybit_client
        self._picker = picker
        self._redeem_swap = redeem_swap
        self._hedge: HedgeTrigger = hedge or NullHedgeTrigger()

    async def handle(
        self, conn: sqlite3.Connection, event: WithdrawRequested
    ) -> None:
        if _row_status(conn, event.tx_id) is None:
            upsert_withdraw_request(
                conn, event.tx_id, event.amount, status=WITHDRAW_RECEIVED
            )

        try:
            await self._phase_close_hedge(conn, event)
            await self._phase_redeem_swap(conn, event)
            await self._phase_mantle_withdraw(conn, event)
            await self._phase_confirm(conn, event)
        except (
            ChainSendError,
            BybitOrderError,
            NoProductAvailable,
            TimeoutError,
            RuntimeError,
        ) as exc:
            self._mark_failed(conn, event.tx_id, exc)
            raise

    # --- Phase 1: hedge close ----------------------------------------------

    async def _phase_close_hedge(
        self, conn: sqlite3.Connection, event: WithdrawRequested
    ) -> None:
        if _row_status(conn, event.tx_id) != WITHDRAW_RECEIVED:
            return

        # Pick the position the deposit cycle staked into. MVP picker is
        # deterministic so re-pick returns the same product — no need to
        # persist picker state across deposit→withdraw boundary.
        picked = await self._picker.pick(self._bybit)
        self._last_picked = picked

        amount = _micro_to_decimal(event.amount)
        outcome = await self._hedge.maybe_close(coin=picked.target_coin, amount=amount)
        next_status = (
            WITHDRAW_HEDGE_CLOSED if outcome == "closed"
            else WITHDRAW_HEDGE_CLOSE_SKIPPED
        )
        advance_withdraw_status(
            conn, event.tx_id, WITHDRAW_RECEIVED, next_status
        )
        log.info(
            "phase_hedge_close_done",
            extra={
                "tx_id": event.tx_id,
                "coin": picked.target_coin,
                "outcome": outcome,
            },
        )

    # --- Phase 2: redeem + swap-back --------------------------------------

    async def _phase_redeem_swap(
        self, conn: sqlite3.Connection, event: WithdrawRequested
    ) -> None:
        status = _row_status(conn, event.tx_id)
        if status not in (WITHDRAW_HEDGE_CLOSED, WITHDRAW_HEDGE_CLOSE_SKIPPED):
            return

        picked = getattr(self, "_last_picked", None)
        if picked is None:
            picked = await self._picker.pick(self._bybit)
            self._last_picked = picked

        source = WithdrawSource(
            product_id=picked.product_id,
            staked_coin=picked.target_coin,
            redeem_amount=_micro_to_decimal(event.amount),
        )
        delivered = await self._redeem_swap.execute(conn, event.tx_id, source)
        self._last_delivered = delivered
        log.info(
            "phase_redeem_swap_done",
            extra={"tx_id": event.tx_id, "delivered_usdc": str(delivered)},
        )

    # --- Phase 3: Bybit withdraw + Mantle credit poll ----------------------

    async def _phase_mantle_withdraw(
        self, conn: sqlite3.Connection, event: WithdrawRequested
    ) -> None:
        status = _row_status(conn, event.tx_id)
        if status not in (WITHDRAW_SWAP_SKIPPED, WITHDRAW_SWAPPED_TO_USDC):
            return

        delivered_usdc = getattr(self, "_last_delivered", None)
        if delivered_usdc is None:
            # Process restart between redeem_swap finish and this phase.
            # event.amount is a conservative upper bound — for USDC path it's
            # exact; for volatile it's slightly high (we'd never get more
            # delivered USDC than the event amount). Polling threshold below
            # halves it so the >= check still triggers.
            delivered_usdc = _micro_to_decimal(event.amount)
            log.warning(
                "mantle_withdraw_lost_delivered_state",
                extra={"tx_id": event.tx_id, "fallback_to_event_amount": True},
            )

        attestor_addr = self._chain.address

        # Snapshot baseline BEFORE the Bybit call so polling sees a real delta.
        baseline_micro = await asyncio.to_thread(
            self._chain.read_usdc_balance, attestor_addr
        )

        withdraw_result = await self._bybit.withdraw_to_mantle(
            coin="USDC",
            amount=str(delivered_usdc),
            address=attestor_addr,
        )
        bybit_withdraw_id = withdraw_result.id
        log.info(
            "phase_bybit_withdraw_initiated",
            extra={
                "tx_id": event.tx_id,
                "bybit_withdraw_id": bybit_withdraw_id,
                "amount": str(delivered_usdc),
                "baseline_micro": baseline_micro,
            },
        )

        # Accept credit >= 50% of delivered — Bybit withdrawal fee can take
        # a noticeable cut on small amounts. Contract-side floor in
        # `confirmWithdraw` is also amount >= expected/2, so anything that
        # makes it through here will satisfy the contract.
        delivered_micro_target = _decimal_to_micro(delivered_usdc)
        credited_micro = await poll_mantle_usdc_credit(
            writer=self._chain,
            address=attestor_addr,
            baseline=baseline_micro,
            min_credit=max(1, delivered_micro_target // 2),
        )

        advance_withdraw_status(
            conn,
            event.tx_id,
            status,
            WITHDRAW_ON_MANTLE,
            bybit_withdraw_id=bybit_withdraw_id,
            delivered_amount=credited_micro,
        )
        log.info(
            "phase_mantle_credited",
            extra={"tx_id": event.tx_id, "credited_micro": credited_micro},
        )

    # --- Phase 4: approve + confirmWithdraw on-chain -----------------------

    async def _phase_confirm(
        self, conn: sqlite3.Connection, event: WithdrawRequested
    ) -> None:
        if _row_status(conn, event.tx_id) != WITHDRAW_ON_MANTLE:
            return

        row = conn.execute(
            "SELECT delivered_amount FROM withdraw_requests WHERE tx_id = ?",
            (event.tx_id,),
        ).fetchone()
        delivered_micro = row["delivered_amount"] if row else None
        if not delivered_micro or delivered_micro <= 0:
            raise RuntimeError(
                f"phase_confirm: no delivered_amount recorded for tx_id={event.tx_id}"
            )

        contract_addr = self._chain.attestor_contract_address
        approve_hash = await asyncio.to_thread(
            self._chain.approve_usdc, contract_addr, delivered_micro
        )
        log.info(
            "phase_confirm_approved",
            extra={
                "tx_id": event.tx_id,
                "approved_micro": delivered_micro,
                "approve_tx": approve_hash,
            },
        )

        confirm_hash = await asyncio.to_thread(
            self._chain.push_confirm_withdraw, event.tx_id, delivered_micro
        )
        advance_withdraw_status(
            conn,
            event.tx_id,
            WITHDRAW_ON_MANTLE,
            WITHDRAW_CONFIRMED,
            mantle_tx_hash=confirm_hash,
        )
        log.info(
            "withdraw_cycle_done",
            extra={
                "tx_id": event.tx_id,
                "delivered_micro": delivered_micro,
                "confirm_tx": confirm_hash,
            },
        )

    # --- Failure handling --------------------------------------------------

    def _mark_failed(
        self, conn: sqlite3.Connection, tx_id: int, exc: BaseException
    ) -> None:
        current = _row_status(conn, tx_id)
        if current is None or current in (WITHDRAW_FAILED, WITHDRAW_CONFIRMED):
            if current is not None:
                with contextlib.suppress(LookupError):
                    increment_withdraw_retry(conn, tx_id, repr(exc))
            return

        with contextlib.suppress(LookupError):
            increment_withdraw_retry(conn, tx_id, repr(exc))
        advance_withdraw_status(
            conn, tx_id, current, WITHDRAW_FAILED, last_error=repr(exc)
        )
        log.error(
            "withdraw_cycle_failed",
            extra={"tx_id": tx_id, "at_status": current, "err": repr(exc)},
        )
