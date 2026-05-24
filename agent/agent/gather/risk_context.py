from web3 import Web3

from agent.config import settings
from agent.validate.risk_context import RiskContext


_ZERO = "0x0000000000000000000000000000000000000000"


_ADAPTER_GETTERS_ABI = [
    {"name": "aavePool", "outputs": [{"type": "address"}], "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "aaveOracle", "outputs": [{"type": "address"}], "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "usdc", "outputs": [{"type": "address"}], "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "weth", "outputs": [{"type": "address"}], "inputs": [], "stateMutability": "view", "type": "function"},
    {"name": "lastAttestationTime", "outputs": [{"type": "uint256"}], "inputs": [], "stateMutability": "view", "type": "function"},
]

_AAVE_POOL_ABI = [{
    "name": "getReserveData",
    "type": "function",
    "stateMutability": "view",
    "inputs": [{"name": "asset", "type": "address"}],
    "outputs": [{
        "type": "tuple",
        "components": [
            {"name": "configuration", "type": "tuple", "components": [{"name": "data", "type": "uint256"}]},
            {"name": "liquidityIndex", "type": "uint128"},
            {"name": "currentLiquidityRate", "type": "uint128"},
            {"name": "variableBorrowIndex", "type": "uint128"},
            {"name": "currentVariableBorrowRate", "type": "uint128"},
            {"name": "currentStableBorrowRate", "type": "uint128"},
            {"name": "lastUpdateTimestamp", "type": "uint40"},
            {"name": "id", "type": "uint16"},
            {"name": "aTokenAddress", "type": "address"},
            {"name": "stableDebtTokenAddress", "type": "address"},
            {"name": "variableDebtTokenAddress", "type": "address"},
            {"name": "interestRateStrategyAddress", "type": "address"},
            {"name": "accruedToTreasury", "type": "uint128"},
            {"name": "unbacked", "type": "uint128"},
            {"name": "isolationModeTotalDebt", "type": "uint128"},
        ],
    }],
}]

_ERC20_SUPPLY_ABI = [
    {"name": "totalSupply", "outputs": [{"type": "uint256"}], "inputs": [], "stateMutability": "view", "type": "function"},
]

_ORACLE_ABI = [
    {"name": "getAssetPrice", "outputs": [{"type": "uint256"}], "inputs": [{"name": "asset", "type": "address"}], "stateMutability": "view", "type": "function"},
]

_PEG_E8 = 100_000_000  # $1.00 in 1e8 fixed-point (Aave Oracle convention)


def _has_addr(addr: str) -> bool:
    return bool(addr) and addr != _ZERO


def _bybit_lag_minutes(w3: Web3) -> float | None:
    addr = settings.BYBIT_ATTESTOR_ADAPTER
    if not _has_addr(addr):
        return None
    try:
        attestor = w3.eth.contract(address=addr, abi=_ADAPTER_GETTERS_ABI)
        last = attestor.functions.lastAttestationTime().call()
        if last == 0:
            return None  # no attestation ever → fail-closed
        now = w3.eth.get_block("latest")["timestamp"]
        return max(0.0, (now - last) / 60.0)
    except Exception:
        return None


def _usdc_peg_deviation_bps(w3: Web3) -> float | None:
    """USDC price from Aave Oracle (single verified on-chain source on
    Mantle). Oracle ref is taken from AaveV3WethAdapter, USDC token ref
    from AaveV3UsdcAdapter — both already configured for the Execute
    layer, no new env vars needed."""
    weth_addr = settings.AAVE_V3_WETH_ADAPTER
    usdc_addr = settings.AAVE_V3_USDC_ADAPTER
    if not (_has_addr(weth_addr) and _has_addr(usdc_addr)):
        return None
    try:
        weth_adapter = w3.eth.contract(address=weth_addr, abi=_ADAPTER_GETTERS_ABI)
        usdc_adapter = w3.eth.contract(address=usdc_addr, abi=_ADAPTER_GETTERS_ABI)
        oracle_addr = weth_adapter.functions.aaveOracle().call()
        usdc_token = usdc_adapter.functions.usdc().call()
        oracle = w3.eth.contract(address=oracle_addr, abi=_ORACLE_ABI)
        price_e8 = oracle.functions.getAssetPrice(usdc_token).call()
        return abs(price_e8 - _PEG_E8) / _PEG_E8 * 10_000
    except Exception:
        return None


def _aave_utilization(w3: Web3, adapter_addr: str, asset_attr: str) -> float | None:
    """Utilization = (variableDebt + stableDebt).totalSupply / aToken.totalSupply.
    `asset_attr` is the adapter's getter for the underlying token
    address (`usdc` or `weth`)."""
    if not _has_addr(adapter_addr):
        return None
    try:
        adapter = w3.eth.contract(address=adapter_addr, abi=_ADAPTER_GETTERS_ABI)
        pool_addr = adapter.functions.aavePool().call()
        asset_addr = getattr(adapter.functions, asset_attr)().call()
        pool = w3.eth.contract(address=pool_addr, abi=_AAVE_POOL_ABI)
        data = pool.functions.getReserveData(asset_addr).call()
        a_token_addr = data[8]
        stable_debt_addr = data[9]
        variable_debt_addr = data[10]

        a_token = w3.eth.contract(address=a_token_addr, abi=_ERC20_SUPPLY_ABI)
        stable_debt = w3.eth.contract(address=stable_debt_addr, abi=_ERC20_SUPPLY_ABI)
        var_debt = w3.eth.contract(address=variable_debt_addr, abi=_ERC20_SUPPLY_ABI)

        supplied = a_token.functions.totalSupply().call()
        if supplied == 0:
            return 0.0
        borrowed = (
            stable_debt.functions.totalSupply().call()
            + var_debt.functions.totalSupply().call()
        )
        return borrowed / supplied
    except Exception:
        return None


async def get_risk_context() -> RiskContext:
    """Snapshot live risk metrics for the deterministic validator.

    Each field fails closed: missing config, RPC error, or impossible
    value leaves the field as `None`, which conditional rules treat as
    triggered.

    `weth_funding_available` is hardcoded False until the
    weth-funding-gap (USDC<->WETH swap rail) lands."""
    w3 = Web3(Web3.HTTPProvider(settings.MANTLE_RPC_URL))

    return RiskContext(
        bybit_attestor_lag_minutes=_bybit_lag_minutes(w3),
        usdc_peg_deviation_bps=_usdc_peg_deviation_bps(w3),
        aave_v3_usdc_utilization=_aave_utilization(w3, settings.AAVE_V3_USDC_ADAPTER, "usdc"),
        aave_v3_weth_utilization=_aave_utilization(w3, settings.AAVE_V3_WETH_ADAPTER, "weth"),
        weth_funding_available=False,
    )
