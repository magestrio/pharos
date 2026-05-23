from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class OracleSettings(BaseSettings):
    """Bybit oracle settings, loaded from .env.

    Intentionally separate from `agent.config.Settings` — the oracle is a
    standalone process and shouldn't depend on the rebalancer's env shape.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    MANTLE_RPC_URL: str = "https://rpc.mantle.xyz"
    BYBIT_ATTESTOR_ADDRESS: str = "0x0000000000000000000000000000000000000000"

    # Polling interval for new event entries. Mantle block time is ~2s, so 12s
    # picks up a fresh batch every few blocks without hammering the RPC.
    POLL_INTERVAL_SECONDS: float = 12.0

    # SQLite file path. Relative paths resolve against the process CWD.
    ORACLE_DB_PATH: Path = Path("bybit_oracle.sqlite")

    # First block to scan on a cold start. Set to the contract deploy block
    # to avoid replaying the entire chain. `0` means "latest" — fine for
    # tests but in production must be pinned.
    ORACLE_FROM_BLOCK: int = 0

    LOG_LEVEL: str = "INFO"


settings = OracleSettings()
