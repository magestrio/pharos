from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# RSA private key default path. Sandbox creds live outside the repo;
# CI/tests can override via env or by patching OracleSettings.
_DEFAULT_RSA_KEY_PATH = Path.home() / ".config" / "vault8004" / "bybit-sandbox-rsa.pem"


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

    # Bybit V5 REST. RSA auth only (sign-type=1). Empty defaults so the
    # listener-only process can boot without credentials; the client raises
    # if they're missing at call time. Switch BYBIT_BASE_URL to
    # https://api-testnet.bybit.com for tests.
    BYBIT_API_KEY: SecretStr = SecretStr("")
    BYBIT_PRIVATE_KEY_PATH: Path = _DEFAULT_RSA_KEY_PATH
    BYBIT_BASE_URL: str = "https://api.bybit.com"
    BYBIT_RECV_WINDOW: int = 5000

    # Mantle on-chain signer (BybitAttestor.confirmDeposit / confirmWithdraw /
    # updateBalance). Empty key by default so listener boots in observe-only
    # mode; ChainWriter.from_settings raises if absent at call time.
    ATTESTOR_PRIVATE_KEY: SecretStr = SecretStr("")
    MANTLE_CHAIN_ID: int = 5000
    MANTLE_TX_RECEIPT_TIMEOUT: int = 120
    MANTLE_GAS_BUFFER: float = 1.2

    # USDC on Mantle (used by the orchestrator to bridge escrow-released
    # USDC from attestor wallet to the Bybit deposit address).
    USDC_ADDRESS: str = "0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9"

    # Vault SAFE address — holds USDC + aUSDC on Mantle. Read-only by the
    # sandbox snapshot collector (`.37a`) to surface the on-chain USDC
    # position alongside Bybit balances. Empty string disables the
    # on-chain leg of the snapshot (collector logs a warning and
    # `Snapshot.on_chain_state` stays None).
    MANTLE_VAULT_ADDRESS: str = ""

    # Balance-update cron (.14). Poll Bybit positions, push `updateBalance`
    # when computed USDC equivalent drifts >threshold from on-chain attested,
    # or when more than `max_age` seconds since the last push.
    BALANCE_POLL_INTERVAL_SECONDS: float = 60.0
    BALANCE_THRESHOLD_BPS: int = 10  # 0.1% (10 basis points)
    BALANCE_MAX_AGE_SECONDS: float = 300.0  # force push every 5 min


settings = OracleSettings()
