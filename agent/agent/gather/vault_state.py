from web3 import Web3
from agent.config import settings
from agent.gather.models import VaultState, AdapterBalance


_CM_ABI = [
    {"name": "totalAssetsUsdc", "outputs": [{"type": "uint256"}], "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "usdc", "outputs": [{"type": "address"}], "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "vusdc", "outputs": [{"type": "address"}], "inputs": [], "stateMutability": "view", "type": "function"},
]

_ERC20_ABI = [
    {"name": "balanceOf", "outputs": [{"type": "uint256"}], "inputs": [{"name": "account", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"name": "totalSupply", "outputs": [{"type": "uint256"}], "inputs": [], "stateMutability": "view", "type": "function"},
]

_ADAPTER_ABI = [
    {"name": "valueInUsdc", "outputs": [{"type": "uint256"}], "inputs": [], "stateMutability": "view", "type": "function"},
]

_ZERO_ADDR = "0x0000000000000000000000000000000000000000"

# Adapter venue names must match TargetAllocation field names in
# agent.reason.schema so the Execute layer can match target_pct → adapter
# address by string lookup.
ADAPTER_VENUES = ("aave_v3_usdc", "aave_v3_weth", "bybit_attestor")


def _adapter_addresses() -> dict[str, str]:
    return {
        "aave_v3_usdc": settings.AAVE_V3_USDC_ADAPTER,
        "aave_v3_weth": settings.AAVE_V3_WETH_ADAPTER,
        "bybit_attestor": settings.BYBIT_ATTESTOR_ADAPTER,
    }


async def get_vault_state() -> VaultState:
    """Reads on-chain state of CapitalManager + whitelisted adapters.

    All USDC amounts are returned as floats in USDC units (6-decimal base
    divided out). `allocations` carries one entry per whitelisted venue
    plus a `cash` entry; entry names match `TargetAllocation` field names
    so the Execute builder can join on them.
    """
    w3 = Web3(Web3.HTTPProvider(settings.MANTLE_RPC_URL))
    cm = w3.eth.contract(address=settings.CAPITAL_MANAGER_ADDRESS, abi=_CM_ABI)

    total_assets_raw = cm.functions.totalAssetsUsdc().call()
    usdc_addr = cm.functions.usdc().call()
    vusdc_addr = cm.functions.vusdc().call()

    usdc = w3.eth.contract(address=usdc_addr, abi=_ERC20_ABI)
    cash_raw = usdc.functions.balanceOf(settings.CAPITAL_MANAGER_ADDRESS).call()

    if vusdc_addr != _ZERO_ADDR:
        vusdc = w3.eth.contract(address=vusdc_addr, abi=_ERC20_ABI)
        total_supply_raw = vusdc.functions.totalSupply().call()
    else:
        total_supply_raw = 0

    allocations: list[AdapterBalance] = []
    for name, addr in _adapter_addresses().items():
        if not addr or addr == _ZERO_ADDR:
            value_raw = 0
            addr_out = None
        else:
            adapter = w3.eth.contract(address=addr, abi=_ADAPTER_ABI)
            value_raw = adapter.functions.valueInUsdc().call()
            addr_out = addr
        allocations.append(AdapterBalance(
            name=name,
            address=addr_out,
            balance_assets=value_raw / 1e6,
            pct_of_total=value_raw / total_assets_raw if total_assets_raw > 0 else 0.0,
        ))

    allocations.append(AdapterBalance(
        name="cash",
        address=None,
        balance_assets=cash_raw / 1e6,
        pct_of_total=cash_raw / total_assets_raw if total_assets_raw > 0 else 1.0,
    ))

    share_price = total_assets_raw / total_supply_raw if total_supply_raw > 0 else 1.0

    return VaultState(
        total_assets_usd=total_assets_raw / 1e6,
        total_supply=total_supply_raw / 1e6,
        share_price=share_price,
        allocations=allocations,
        cash_pct=cash_raw / total_assets_raw if total_assets_raw > 0 else 1.0,
    )
