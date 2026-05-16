from web3 import Web3
from agent.config import settings
from agent.gather.models import VaultState, AdapterBalance

# Week 1 single-slot version. Week 2 расширится до multi-strategy с per-adapter балансами.

_VAULT_ABI = [
    {"name": "totalAssets", "outputs": [{"type": "uint256"}], "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "totalSupply", "outputs": [{"type": "uint256"}], "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "currentStrategy", "outputs": [{"type": "address"}], "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "totalAllocated", "outputs": [{"type": "uint256"}], "inputs": [], "stateMutability": "view", "type": "function"},
]

_ZERO_ADDR = "0x0000000000000000000000000000000000000000"


async def get_vault_state() -> VaultState:
    """Reads on-chain state of Vault8004 + adapters."""
    w3 = Web3(Web3.HTTPProvider(settings.MANTLE_RPC_URL))
    vault = w3.eth.contract(address=settings.VAULT_ADDRESS, abi=_VAULT_ABI)

    total_assets = vault.functions.totalAssets().call()
    total_supply = vault.functions.totalSupply().call()
    total_allocated = vault.functions.totalAllocated().call()
    current_strategy = vault.functions.currentStrategy().call()

    cash = total_assets - total_allocated

    allocations = [
        AdapterBalance(
            name="strategy",
            address=current_strategy if current_strategy != _ZERO_ADDR else None,
            balance_assets=total_allocated / 1e18,
            pct_of_total=total_allocated / total_assets if total_assets > 0 else 0.0,
        ),
        AdapterBalance(
            name="cash",
            address=None,
            balance_assets=cash / 1e18,
            pct_of_total=cash / total_assets if total_assets > 0 else 1.0,
        ),
    ]

    share_price = total_assets / total_supply if total_supply > 0 else 1.0

    return VaultState(
        total_assets_usd=total_assets / 1e18,
        total_supply=total_supply / 1e18,
        share_price=share_price,
        allocations=allocations,
        cash_pct=cash / total_assets if total_assets > 0 else 1.0,
    )
