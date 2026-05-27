"""Tests for sandbox.on_chain — Aave V3 USDC read-only fetch (`.37a`).

web3.py is mocked via unittest.mock so tests run without a live RPC.
The mock exercises the same code path as production (contract objects,
function calls, address checksums) — only the network layer is fake.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from agent.sandbox.on_chain import (
    AAVE_V3_POOL_ADDRESS,
    AUSDC_ADDRESS,
    USDC_ADDRESS,
    AaveV3UsdcState,
    fetch_aave_v3_usdc_state,
    micro_to_usd,
)


VAULT = "0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037"  # Safe vault address


def _reserve_data_tuple(current_liquidity_rate_ray: int) -> tuple:
    """Build the 15-field ReserveData tuple Aave V3 returns. Only
    `currentLiquidityRate` (index 2) carries meaning for the test;
    others are zero placeholders."""
    return (
        (0,),  # configuration (struct with one uint256)
        0,  # liquidityIndex
        current_liquidity_rate_ray,  # currentLiquidityRate
        0,  # variableBorrowIndex
        0,  # currentVariableBorrowRate
        0,  # currentStableBorrowRate
        0,  # lastUpdateTimestamp
        0,  # id
        AUSDC_ADDRESS,  # aTokenAddress
        "0x0000000000000000000000000000000000000000",  # stableDebtToken
        "0x0000000000000000000000000000000000000000",  # variableDebtToken
        "0x0000000000000000000000000000000000000000",  # interestRateStrategy
        0,  # accruedToTreasury
        0,  # unbacked
        0,  # isolationModeTotalDebt
    )


def _make_mock_w3(
    *,
    current_liquidity_rate_ray: int,
    vault_usdc_micro: int,
    vault_ausdc_micro: int,
    block_number: int = 12345678,
) -> MagicMock:
    """Build a MagicMock Web3 that responds to the three calls
    `fetch_aave_v3_usdc_state` makes. Each `contract(address=..., abi=...)`
    call returns a fresh stub whose `functions.<name>(...).call()` chain
    returns the prepared value."""
    w3 = MagicMock(name="Web3")
    w3.eth.block_number = block_number

    pool_stub = MagicMock(name="pool_contract")
    pool_stub.functions.getReserveData.return_value.call.return_value = (
        _reserve_data_tuple(current_liquidity_rate_ray)
    )

    usdc_stub = MagicMock(name="usdc_contract")
    usdc_stub.functions.balanceOf.return_value.call.return_value = vault_usdc_micro

    ausdc_stub = MagicMock(name="ausdc_contract")
    ausdc_stub.functions.balanceOf.return_value.call.return_value = vault_ausdc_micro

    # Route w3.eth.contract(address=..., abi=...) to the right stub by
    # the address argument. Production fetcher calls them in this order:
    # pool, usdc, ausdc.
    def _make_contract(address: str, abi: list) -> MagicMock:
        a = address.lower()
        if a == AAVE_V3_POOL_ADDRESS.lower():
            return pool_stub
        if a == USDC_ADDRESS.lower():
            return usdc_stub
        if a == AUSDC_ADDRESS.lower():
            return ausdc_stub
        raise AssertionError(f"unexpected contract address {address}")

    w3.eth.contract.side_effect = _make_contract
    return w3


# ─── micro_to_usd ──────────────────────────────────────────────────────────


def test_micro_to_usd_converts_six_decimal_units() -> None:
    assert micro_to_usd(0) == Decimal(0)
    assert micro_to_usd(1_000_000) == Decimal(1)
    assert micro_to_usd(50_500_000) == Decimal("50.5")
    # Sub-cent precision survives.
    assert micro_to_usd(1) == Decimal("0.000001")


# ─── fetch_aave_v3_usdc_state ──────────────────────────────────────────────


def test_fetch_returns_state_with_supply_apr_from_ray() -> None:
    # Aave Ray = 1e27. 3.45% APR encoded as 3.45e25.
    rate = 34_500_000_000_000_000_000_000_000  # 0.0345 * 1e27
    w3 = _make_mock_w3(
        current_liquidity_rate_ray=rate,
        vault_usdc_micro=10_000_000,  # $10
        vault_ausdc_micro=50_000_000,  # $50
    )

    state = fetch_aave_v3_usdc_state(w3, VAULT)

    assert isinstance(state, AaveV3UsdcState)
    assert state.supply_apr == Decimal("0.0345")
    assert state.vault_usdc_micro == 10_000_000
    assert state.vault_ausdc_micro == 50_000_000
    assert state.pool_address == AAVE_V3_POOL_ADDRESS
    assert state.block_number == 12345678


def test_fetch_handles_zero_supply_apr_and_zero_balances() -> None:
    """Fresh vault with nothing supplied. APR=0 is degenerate but legal."""
    w3 = _make_mock_w3(
        current_liquidity_rate_ray=0,
        vault_usdc_micro=0,
        vault_ausdc_micro=0,
    )
    state = fetch_aave_v3_usdc_state(w3, VAULT)
    assert state.supply_apr == Decimal(0)
    assert state.vault_usdc_micro == 0
    assert state.vault_ausdc_micro == 0


def test_fetch_calls_balance_of_with_checksummed_vault_address() -> None:
    """Bare-lowercase vault address must be checksummed before going
    into balanceOf — otherwise web3 raises InvalidAddress."""
    w3 = _make_mock_w3(
        current_liquidity_rate_ray=0,
        vault_usdc_micro=100,
        vault_ausdc_micro=200,
    )
    fetch_aave_v3_usdc_state(w3, VAULT.lower())

    # Both balanceOf calls receive the checksummed form.
    usdc_stub_call = w3.eth.contract.side_effect(USDC_ADDRESS, [])
    ausdc_stub_call = w3.eth.contract.side_effect(AUSDC_ADDRESS, [])
    # MagicMock records all calls — find the balanceOf invocations.
    # The mock above returns the SAME stub each time for a given address,
    # so we read from the recorded call args.
    usdc_args = usdc_stub_call.functions.balanceOf.call_args
    ausdc_args = ausdc_stub_call.functions.balanceOf.call_args
    # Checksummed Mantle vault: mixed-case per EIP-55.
    assert usdc_args.args[0] == "0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037"
    assert ausdc_args.args[0] == "0x4dc4a70Ae02d7ca2F3A06b1231b3A9312d82a037"


def test_fetch_propagates_rpc_exception() -> None:
    """If the pool call blows up, propagate. The snapshot collector
    wraps the call in `_safe_fetch_aave_v3` for fail-soft."""
    w3 = MagicMock(name="Web3")
    w3.eth.block_number = 0
    pool_stub = MagicMock(name="pool_contract")
    pool_stub.functions.getReserveData.return_value.call.side_effect = RuntimeError(
        "rpc down"
    )
    w3.eth.contract.return_value = pool_stub

    with pytest.raises(RuntimeError, match="rpc down"):
        fetch_aave_v3_usdc_state(w3, VAULT)
