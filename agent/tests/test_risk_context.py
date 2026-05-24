from unittest.mock import MagicMock

import pytest

from agent.config import settings
from agent.gather import risk_context as rc_mod
from agent.gather.risk_context import (
    _aave_utilization,
    _bybit_lag_minutes,
    _usdc_peg_deviation_bps,
    get_risk_context,
)


ATTESTOR = "0x000000000000000000000000000000000000B001"
USDC_ADAPTER = "0x000000000000000000000000000000000000A001"
WETH_ADAPTER = "0x000000000000000000000000000000000000A002"
AAVE_POOL = "0x000000000000000000000000000000000000C001"
AAVE_ORACLE = "0x000000000000000000000000000000000000C002"
USDC_TOKEN = "0x000000000000000000000000000000000000D001"
WETH_TOKEN = "0x000000000000000000000000000000000000D002"
A_USDC = "0x000000000000000000000000000000000000E001"
STABLE_DEBT_USDC = "0x000000000000000000000000000000000000E002"
VAR_DEBT_USDC = "0x000000000000000000000000000000000000E003"


@pytest.fixture(autouse=True)
def _addrs(monkeypatch):
    monkeypatch.setattr(settings, "BYBIT_ATTESTOR_ADAPTER", ATTESTOR)
    monkeypatch.setattr(settings, "AAVE_V3_USDC_ADAPTER", USDC_ADAPTER)
    monkeypatch.setattr(settings, "AAVE_V3_WETH_ADAPTER", WETH_ADAPTER)


def _w3_with_contracts(contracts: dict, now_ts: int = 1_000_000) -> MagicMock:
    fake = MagicMock()
    fake.eth.contract.side_effect = lambda address, abi: contracts[address]
    fake.eth.get_block.return_value = {"timestamp": now_ts}
    return fake


def _attestor_mock(last_attestation_time: int) -> MagicMock:
    m = MagicMock()
    m.functions.lastAttestationTime.return_value.call.return_value = last_attestation_time
    return m


# ─── bybit lag ───────────────────────────────────────────────────────────────

def test_bybit_lag_returns_minutes():
    # last attestation 5 minutes ago
    now = 1_000_000
    last = now - 300
    w3 = _w3_with_contracts({ATTESTOR: _attestor_mock(last)}, now_ts=now)
    assert _bybit_lag_minutes(w3) == pytest.approx(5.0)


def test_bybit_lag_none_when_never_attested():
    w3 = _w3_with_contracts({ATTESTOR: _attestor_mock(0)})
    assert _bybit_lag_minutes(w3) is None


def test_bybit_lag_none_on_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "BYBIT_ATTESTOR_ADAPTER", "")
    w3 = _w3_with_contracts({})
    assert _bybit_lag_minutes(w3) is None


def test_bybit_lag_none_on_rpc_error():
    m = MagicMock()
    m.functions.lastAttestationTime.return_value.call.side_effect = Exception("RPC down")
    w3 = _w3_with_contracts({ATTESTOR: m})
    assert _bybit_lag_minutes(w3) is None


# ─── usdc peg ────────────────────────────────────────────────────────────────

def _adapter_with_getters(**getters) -> MagicMock:
    m = MagicMock()
    for name, val in getters.items():
        getattr(m.functions, name).return_value.call.return_value = val
    return m


def _oracle_mock(price_for: dict[str, int]) -> MagicMock:
    m = MagicMock()
    m.functions.getAssetPrice.side_effect = (
        lambda asset: MagicMock(call=MagicMock(return_value=price_for[asset]))
    )
    return m


def test_usdc_peg_at_dollar_is_zero_bps():
    w3 = _w3_with_contracts({
        WETH_ADAPTER: _adapter_with_getters(aaveOracle=AAVE_ORACLE),
        USDC_ADAPTER: _adapter_with_getters(usdc=USDC_TOKEN),
        AAVE_ORACLE: _oracle_mock({USDC_TOKEN: 100_000_000}),  # $1.00 exact
    })
    assert _usdc_peg_deviation_bps(w3) == pytest.approx(0.0)


def test_usdc_peg_99_cents_is_100_bps():
    w3 = _w3_with_contracts({
        WETH_ADAPTER: _adapter_with_getters(aaveOracle=AAVE_ORACLE),
        USDC_ADAPTER: _adapter_with_getters(usdc=USDC_TOKEN),
        AAVE_ORACLE: _oracle_mock({USDC_TOKEN: 99_000_000}),  # $0.99
    })
    assert _usdc_peg_deviation_bps(w3) == pytest.approx(100.0)


def test_usdc_peg_above_dollar_uses_abs():
    w3 = _w3_with_contracts({
        WETH_ADAPTER: _adapter_with_getters(aaveOracle=AAVE_ORACLE),
        USDC_ADAPTER: _adapter_with_getters(usdc=USDC_TOKEN),
        AAVE_ORACLE: _oracle_mock({USDC_TOKEN: 101_500_000}),  # $1.015
    })
    assert _usdc_peg_deviation_bps(w3) == pytest.approx(150.0)


def test_usdc_peg_none_on_error():
    w3 = _w3_with_contracts({
        WETH_ADAPTER: _adapter_with_getters(aaveOracle=AAVE_ORACLE),
        USDC_ADAPTER: _adapter_with_getters(usdc=USDC_TOKEN),
        AAVE_ORACLE: MagicMock(),  # missing getAssetPrice setup → side_effect raises
    })
    w3.eth.contract.side_effect = lambda address, abi: {
        WETH_ADAPTER: _adapter_with_getters(aaveOracle=AAVE_ORACLE),
        USDC_ADAPTER: _adapter_with_getters(usdc=USDC_TOKEN),
        AAVE_ORACLE: MagicMock(functions=MagicMock(getAssetPrice=MagicMock(side_effect=Exception("oracle down")))),
    }[address]
    assert _usdc_peg_deviation_bps(w3) is None


# ─── aave utilization ────────────────────────────────────────────────────────

def _reserve_data(a_token, stable_debt, var_debt) -> list:
    """Build the 15-tuple returned by Pool.getReserveData with the three
    address fields at indices 8/9/10 and zeros elsewhere."""
    out = [0] * 15
    out[0] = (0,)  # configuration tuple
    out[8] = a_token
    out[9] = stable_debt
    out[10] = var_debt
    return out


def _pool_mock(reserve_by_asset: dict) -> MagicMock:
    m = MagicMock()
    m.functions.getReserveData.side_effect = (
        lambda asset: MagicMock(call=MagicMock(return_value=reserve_by_asset[asset]))
    )
    return m


def _totalSupply_mock(value: int) -> MagicMock:
    m = MagicMock()
    m.functions.totalSupply.return_value.call.return_value = value
    return m


def test_aave_utilization_partial():
    # Supplied 1_000_000 USDC, borrowed 700_000 → utilization 0.70
    w3 = _w3_with_contracts({
        USDC_ADAPTER: _adapter_with_getters(aavePool=AAVE_POOL, usdc=USDC_TOKEN),
        AAVE_POOL: _pool_mock({USDC_TOKEN: _reserve_data(A_USDC, STABLE_DEBT_USDC, VAR_DEBT_USDC)}),
        A_USDC: _totalSupply_mock(1_000_000),
        STABLE_DEBT_USDC: _totalSupply_mock(100_000),
        VAR_DEBT_USDC: _totalSupply_mock(600_000),
    })
    assert _aave_utilization(w3, USDC_ADAPTER, "usdc") == pytest.approx(0.70)


def test_aave_utilization_zero_supplied_returns_zero():
    w3 = _w3_with_contracts({
        USDC_ADAPTER: _adapter_with_getters(aavePool=AAVE_POOL, usdc=USDC_TOKEN),
        AAVE_POOL: _pool_mock({USDC_TOKEN: _reserve_data(A_USDC, STABLE_DEBT_USDC, VAR_DEBT_USDC)}),
        A_USDC: _totalSupply_mock(0),
        STABLE_DEBT_USDC: _totalSupply_mock(0),
        VAR_DEBT_USDC: _totalSupply_mock(0),
    })
    assert _aave_utilization(w3, USDC_ADAPTER, "usdc") == 0.0


def test_aave_utilization_none_on_unconfigured():
    w3 = _w3_with_contracts({})
    assert _aave_utilization(w3, "", "usdc") is None


def test_aave_utilization_none_on_rpc_error():
    bad_adapter = MagicMock()
    bad_adapter.functions.aavePool.return_value.call.side_effect = Exception("RPC down")
    w3 = _w3_with_contracts({USDC_ADAPTER: bad_adapter})
    assert _aave_utilization(w3, USDC_ADAPTER, "usdc") is None


# ─── integration: get_risk_context ────────────────────────────────────────────

def _stub_web3_class() -> MagicMock:
    """MagicMock standing in for the Web3 class. Has HTTPProvider as an
    attribute so `Web3.HTTPProvider(url)` works, and calling it (as
    constructor) returns a MagicMock instance."""
    cls = MagicMock()
    cls.HTTPProvider = MagicMock(return_value=None)
    return cls


@pytest.mark.asyncio
async def test_get_risk_context_all_none_when_addresses_unset(monkeypatch):
    monkeypatch.setattr(settings, "BYBIT_ATTESTOR_ADAPTER", "")
    monkeypatch.setattr(settings, "AAVE_V3_USDC_ADAPTER", "")
    monkeypatch.setattr(settings, "AAVE_V3_WETH_ADAPTER", "")
    monkeypatch.setattr(rc_mod, "Web3", _stub_web3_class())
    ctx = await get_risk_context()
    assert ctx.bybit_attestor_lag_minutes is None
    assert ctx.usdc_peg_deviation_bps is None
    assert ctx.aave_v3_usdc_utilization is None
    assert ctx.aave_v3_weth_utilization is None
    assert ctx.weth_funding_available is False


@pytest.mark.asyncio
async def test_get_risk_context_weth_funding_always_false(monkeypatch):
    """Until weth-funding-gap closes, weth_funding_available is hardcoded False."""
    monkeypatch.setattr(rc_mod, "Web3", _stub_web3_class())
    ctx = await get_risk_context()
    assert ctx.weth_funding_available is False
