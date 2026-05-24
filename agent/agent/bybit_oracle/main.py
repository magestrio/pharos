import asyncio

from web3 import Web3

from .abi import load_bybit_attestor_abi
from .balance_updater import BalanceUpdater
from .bybit_client import BybitClient
from .chain_writer import ChainWriter
from .config import settings
from .hedge import NullHedgeTrigger
from .listener import make_contract, run_listener
from .orchestrator import DepositOrchestrator
from .product_picker import FlexibleUsdcPicker
from .redeem_swap import RedeemSwapExecutor
from .state import open_db
from .structured_log import get_logger, setup_logging
from .swap_stake import SwapStakeExecutor
from .withdraw_orchestrator import WithdrawOrchestrator


async def _main() -> None:
    setup_logging(settings.LOG_LEVEL)
    log = get_logger(__name__)

    conn = open_db(settings.ORACLE_DB_PATH)
    log.info("db_open", extra={"path": str(settings.ORACLE_DB_PATH)})

    w3 = Web3(Web3.HTTPProvider(settings.MANTLE_RPC_URL))
    abi = load_bybit_attestor_abi()
    contract = make_contract(w3, settings.BYBIT_ATTESTOR_ADDRESS, abi)

    # Compose orchestrator stack. Each `from_settings` raises if its required
    # creds are missing — listener-only "observe mode" (no Bybit key, no
    # private key) falls back to handler skeletons via the default args of
    # `run_listener`.
    bybit_client = BybitClient.from_settings(cfg=settings)
    chain_writer = ChainWriter.from_settings(cfg=settings, w3=w3)
    picker = FlexibleUsdcPicker()
    swap_stake = SwapStakeExecutor(bybit_client)
    redeem_swap = RedeemSwapExecutor(bybit_client)
    hedge = NullHedgeTrigger()  # blocked-by:hedge-engine
    deposit_orchestrator = DepositOrchestrator(
        chain_writer=chain_writer,
        bybit_client=bybit_client,
        picker=picker,
        swap_stake=swap_stake,
        hedge=hedge,
    )
    withdraw_orchestrator = WithdrawOrchestrator(
        chain_writer=chain_writer,
        bybit_client=bybit_client,
        picker=picker,
        redeem_swap=redeem_swap,
        hedge=hedge,
    )
    balance_updater = BalanceUpdater(
        chain_writer=chain_writer, bybit_client=bybit_client, cfg=settings,
    )
    log.info("orchestrator_ready", extra={"attestor_addr": chain_writer.address})

    listener_task = asyncio.create_task(
        run_listener(
            conn,
            contract,
            deposit_handler=deposit_orchestrator.handle,
            withdraw_handler=withdraw_orchestrator.handle,
        ),
        name="listener",
    )
    balance_task = asyncio.create_task(balance_updater.run_loop(), name="balance_updater")

    try:
        # If either coroutine exits (it shouldn't — both are infinite loops),
        # cancel the other and surface the result.
        done, pending = await asyncio.wait(
            {listener_task, balance_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()  # re-raises if the task errored
    finally:
        await bybit_client.aclose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
