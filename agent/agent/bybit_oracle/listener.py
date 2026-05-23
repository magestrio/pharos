import asyncio
import json
import sqlite3

from web3 import Web3
from web3.contract import Contract

from .config import settings
from .events import DepositRequested, WithdrawRequested
from .handlers import handle_deposit_requested, handle_withdraw_requested
from .state import mark_event_processed
from .structured_log import get_logger

log = get_logger(__name__)


def make_contract(w3: Web3, address: str, abi: list[dict]) -> Contract:
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)


def _event_payload(log_entry) -> str:
    """Serialize a web3 LogReceipt for storage in `processed_events.payload`."""
    return json.dumps(
        {
            "tx_id": log_entry["args"]["txId"],
            "amount": log_entry["args"]["amount"],
            "tx_hash": log_entry["transactionHash"].hex(),
            "log_index": log_entry["logIndex"],
            "block": log_entry["blockNumber"],
        }
    )


async def _dispatch_deposit(conn: sqlite3.Connection, log_entry) -> None:
    if not mark_event_processed(
        conn,
        log_entry["transactionHash"].hex(),
        log_entry["logIndex"],
        "DepositRequested",
        _event_payload(log_entry),
    ):
        return  # already processed
    event = DepositRequested(
        tx_id=log_entry["args"]["txId"],
        amount=log_entry["args"]["amount"],
        tx_hash=log_entry["transactionHash"].hex(),
        log_index=log_entry["logIndex"],
        block_number=log_entry["blockNumber"],
    )
    await handle_deposit_requested(conn, event)


async def _dispatch_withdraw(conn: sqlite3.Connection, log_entry) -> None:
    if not mark_event_processed(
        conn,
        log_entry["transactionHash"].hex(),
        log_entry["logIndex"],
        "WithdrawRequested",
        _event_payload(log_entry),
    ):
        return
    event = WithdrawRequested(
        tx_id=log_entry["args"]["txId"],
        amount=log_entry["args"]["amount"],
        tx_hash=log_entry["transactionHash"].hex(),
        log_index=log_entry["logIndex"],
        block_number=log_entry["blockNumber"],
    )
    await handle_withdraw_requested(conn, event)


async def run_listener(conn: sqlite3.Connection, contract: Contract) -> None:
    from_block = settings.ORACLE_FROM_BLOCK or "latest"
    deposit_filter = contract.events.DepositRequested.create_filter(from_block=from_block)
    withdraw_filter = contract.events.WithdrawRequested.create_filter(from_block=from_block)

    log.info(
        "listener_started",
        extra={
            "contract": contract.address,
            "from_block": from_block,
            "poll_interval": settings.POLL_INTERVAL_SECONDS,
        },
    )

    while True:
        try:
            for entry in deposit_filter.get_new_entries():
                await _dispatch_deposit(conn, entry)
            for entry in withdraw_filter.get_new_entries():
                await _dispatch_withdraw(conn, entry)
        except Exception:
            log.exception("listener_poll_failed")
        await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)
