import asyncio

from web3 import Web3

from .abi import load_bybit_attestor_abi
from .config import settings
from .listener import make_contract, run_listener
from .state import open_db
from .structured_log import get_logger, setup_logging


async def _main() -> None:
    setup_logging(settings.LOG_LEVEL)
    log = get_logger(__name__)

    conn = open_db(settings.ORACLE_DB_PATH)
    log.info("db_open", extra={"path": str(settings.ORACLE_DB_PATH)})

    w3 = Web3(Web3.HTTPProvider(settings.MANTLE_RPC_URL))
    abi = load_bybit_attestor_abi()
    contract = make_contract(w3, settings.BYBIT_ATTESTOR_ADDRESS, abi)

    await run_listener(conn, contract)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
