import sqlite3

from .events import DepositRequested, WithdrawRequested
from .state import upsert_deposit_request, upsert_withdraw_request
from .structured_log import get_logger

log = get_logger(__name__)


async def handle_deposit_requested(conn: sqlite3.Connection, event: DepositRequested) -> None:
    """Skeleton handler — records the request in SQLite and logs.

    Real implementation (subtask .12): withdraw USDC from contract escrow
    to attestor address, transfer to Bybit, LLM picks Earn product, swap
    if needed, stake, then call `confirmDeposit` on-chain.
    """
    upsert_deposit_request(conn, event.tx_id, event.amount, status="received")
    log.info(
        "deposit_requested",
        extra={
            "tx_id": event.tx_id,
            "amount": event.amount,
            "tx_hash": event.tx_hash,
            "block": event.block_number,
        },
    )


async def handle_withdraw_requested(conn: sqlite3.Connection, event: WithdrawRequested) -> None:
    """Skeleton handler — records the request in SQLite and logs.

    Real implementation (subtask .13): close hedges, redeem Earn positions,
    swap back to USDC, withdraw from Bybit to Mantle, then call
    `confirmWithdraw` on-chain with the actual delivered amount.
    """
    upsert_withdraw_request(conn, event.tx_id, event.amount, status="received")
    log.info(
        "withdraw_requested",
        extra={
            "tx_id": event.tx_id,
            "amount": event.amount,
            "tx_hash": event.tx_hash,
            "block": event.block_number,
        },
    )
