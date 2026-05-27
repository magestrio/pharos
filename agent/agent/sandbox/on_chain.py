"""Read-only Mantle on-chain context for the sandbox snapshot (`.37a`).

Pulls Aave V3 USDC pool state + vault balances so the LLM sees on-chain
yield rates next to Bybit Earn rates. Execute path (supply/withdraw) is
NOT wired here — that lands with `.37b` once CapitalManager deploys.

All RPC calls are synchronous web3.py; `collect_snapshot` wraps the
fetch in `asyncio.to_thread` so the snapshot remains async-friendly.

Mantle addresses are pinned to the values verified in
`notes/addresses.md` (Aave V3 mainnet on Mantle). Verifiable via:

    cast call <POOL> 'getReserveData(address)(...)' <USDC> --rpc-url ...
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from web3 import Web3
from web3.contract import Contract

# ─── Mantle mainnet addresses (verified per notes/addresses.md) ─────────────

USDC_ADDRESS = "0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9"
AUSDC_ADDRESS = "0xcb8164415274515867ec43CbD284ab5d6d2b304F"
AAVE_V3_POOL_ADDRESS = "0x458F293454fE0d67EC0655f3672301301DD51422"

# USDC is 6-decimal on Mantle.
_USDC_DECIMALS = 6

# Aave returns rates in Ray units (1e27). Liquidity rate is the
# annualized supply APR — fraction of 1.0 (not %).
_RAY = Decimal(10) ** 27


# ─── Minimal ABIs ──────────────────────────────────────────────────────────

# Only the slice of `getReserveData(address)` we read from. The struct
# below is positional — web3.py returns a tuple, we index `currentLiquidityRate`
# at position 2 (after configuration and liquidityIndex).
_AAVE_POOL_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
        "name": "getReserveData",
        "outputs": [
            {
                "components": [
                    {
                        "components": [
                            {"internalType": "uint256", "name": "data", "type": "uint256"}
                        ],
                        "internalType": "struct DataTypes.ReserveConfigurationMap",
                        "name": "configuration",
                        "type": "tuple",
                    },
                    {"internalType": "uint128", "name": "liquidityIndex", "type": "uint128"},
                    {"internalType": "uint128", "name": "currentLiquidityRate", "type": "uint128"},
                    {"internalType": "uint128", "name": "variableBorrowIndex", "type": "uint128"},
                    {"internalType": "uint128", "name": "currentVariableBorrowRate", "type": "uint128"},
                    {"internalType": "uint128", "name": "currentStableBorrowRate", "type": "uint128"},
                    {"internalType": "uint40", "name": "lastUpdateTimestamp", "type": "uint40"},
                    {"internalType": "uint16", "name": "id", "type": "uint16"},
                    {"internalType": "address", "name": "aTokenAddress", "type": "address"},
                    {"internalType": "address", "name": "stableDebtTokenAddress", "type": "address"},
                    {"internalType": "address", "name": "variableDebtTokenAddress", "type": "address"},
                    {"internalType": "address", "name": "interestRateStrategyAddress", "type": "address"},
                    {"internalType": "uint128", "name": "accruedToTreasury", "type": "uint128"},
                    {"internalType": "uint128", "name": "unbacked", "type": "uint128"},
                    {"internalType": "uint128", "name": "isolationModeTotalDebt", "type": "uint128"},
                ],
                "internalType": "struct DataTypes.ReserveData",
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

_ERC20_BALANCE_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


# ─── Data shape ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AaveV3UsdcState:
    """One-shot read of the Aave V3 USDC pool + vault balances on Mantle.

    `supply_apr` is fractional (0.0345 = 3.45% APY), derived from the
    pool's `currentLiquidityRate` divided by 1e27 (Ray precision).
    `vault_usdc_micro` / `vault_ausdc_micro` are raw 6-decimal integers
    (1_000_000 = $1.00). USD-equivalent conversion happens at the
    snapshot model boundary so the on_chain layer stays unit-honest.
    """

    block_number: int
    fetched_at: datetime
    pool_address: str
    supply_apr: Decimal
    vault_usdc_micro: int
    vault_ausdc_micro: int


# ─── RPC fetch ──────────────────────────────────────────────────────────────


def make_mantle_client(rpc_url: str) -> Web3:
    """Construct a Web3 client pointed at Mantle. Thin wrapper to keep
    the snapshot caller free of web3 import noise."""
    return Web3(Web3.HTTPProvider(rpc_url))


def fetch_aave_v3_usdc_state(
    w3: Web3,
    vault_address: str,
    *,
    pool_address: str = AAVE_V3_POOL_ADDRESS,
    usdc_address: str = USDC_ADDRESS,
    ausdc_address: str = AUSDC_ADDRESS,
) -> AaveV3UsdcState:
    """Read pool APR + vault USDC + aUSDC balances in one fetch.

    Three eth_calls total — pool.getReserveData, usdc.balanceOf, aUSDC.balanceOf.
    Caller is responsible for handling RPC errors; the snapshot layer
    wraps this in a try/except so a Mantle outage doesn't break the
    whole snapshot (Bybit side still works).
    """
    pool = _contract(w3, pool_address, _AAVE_POOL_ABI)
    usdc = _contract(w3, usdc_address, _ERC20_BALANCE_ABI)
    ausdc = _contract(w3, ausdc_address, _ERC20_BALANCE_ABI)

    reserve = pool.functions.getReserveData(
        Web3.to_checksum_address(usdc_address)
    ).call()
    # web3 returns the struct as a tuple. Index 2 is currentLiquidityRate
    # per the ABI ordering — comment kept here so this isn't a magic number.
    current_liquidity_rate = reserve[2]
    supply_apr = Decimal(current_liquidity_rate) / _RAY

    vault_checksum = Web3.to_checksum_address(vault_address)
    vault_usdc_micro = usdc.functions.balanceOf(vault_checksum).call()
    vault_ausdc_micro = ausdc.functions.balanceOf(vault_checksum).call()

    return AaveV3UsdcState(
        block_number=w3.eth.block_number,
        fetched_at=datetime.now(UTC),
        pool_address=pool_address,
        supply_apr=supply_apr,
        vault_usdc_micro=int(vault_usdc_micro),
        vault_ausdc_micro=int(vault_ausdc_micro),
    )


def micro_to_usd(micro: int) -> Decimal:
    """Convert 6-decimal USDC micro-units to a USD Decimal."""
    return Decimal(micro) / (Decimal(10) ** _USDC_DECIMALS)


def _contract(w3: Web3, address: str, abi: list[dict[str, Any]]) -> Contract:
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
