import asyncio

from web3 import Web3

from .abi import load_bybit_attestor_abi
from .bybit_client import BybitClient
from .chain_writer import ChainWriter
from .config import settings
from .listener import make_contract, run_listener
from .orchestrator import DepositOrchestrator
from .product_picker import FlexibleUsdcPicker
from .state import open_db
from .structured_log import get_logger, setup_logging
from .swap_stake import SwapStakeExecutor


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
    orchestrator = DepositOrchestrator(
        chain_writer=chain_writer,
        bybit_client=bybit_client,
        picker=picker,
        swap_stake=swap_stake,
    )
    log.info("orchestrator_ready", extra={"attestor_addr": chain_writer.address})

    try:
        await run_listener(conn, contract, deposit_handler=orchestrator.handle)
    finally:
        await bybit_client.aclose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
