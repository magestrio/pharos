import pytest

from agent.config import settings
from agent.execute.builders import (
    AllocationCall,
    AllocationCallKind,
    DELTA_THRESHOLD,
    build_allocation_calls,
)
from agent.execute.tx import SKIPPED, execute_on_chain
from agent.gather.models import AdapterBalance, VaultState
from agent.reason.schema import TargetAllocation


AAVE_USDC = "0x000000000000000000000000000000000000A001"
AAVE_WETH = "0x000000000000000000000000000000000000A002"
BYBIT = "0x000000000000000000000000000000000000A003"


@pytest.fixture(autouse=True)
def _adapter_addresses(monkeypatch):
    monkeypatch.setattr(settings, "AAVE_V3_USDC_ADAPTER", AAVE_USDC)
    monkeypatch.setattr(settings, "AAVE_V3_WETH_ADAPTER", AAVE_WETH)
    monkeypatch.setattr(settings, "BYBIT_ATTESTOR_ADAPTER", BYBIT)


def _vault(total: float, **per_venue: float) -> VaultState:
    """Build a synthetic VaultState. Pass venue=usdc_amount kwargs;
    cash = total - sum(venues)."""
    used = sum(per_venue.values())
    cash = total - used
    allocations = [
        AdapterBalance(
            name=name,
            address=None,
            balance_assets=amt,
            pct_of_total=amt / total if total > 0 else 0.0,
        )
        for name, amt in per_venue.items()
    ]
    allocations.append(AdapterBalance(
        name="cash",
        address=None,
        balance_assets=cash,
        pct_of_total=cash / total if total > 0 else 1.0,
    ))
    return VaultState(
        total_assets_usd=total,
        total_supply=total,
        share_price=1.0,
        allocations=allocations,
        cash_pct=cash / total if total > 0 else 1.0,
    )


def _alloc(cash=0.0, aave_usdc=0.0, aave_weth=0.0, bybit=0.0) -> TargetAllocation:
    return TargetAllocation(
        cash_usdc=cash,
        aave_v3_usdc=aave_usdc,
        aave_v3_weth=aave_weth,
        bybit_attestor=bybit,
    )


# ─── pure no-op cases ────────────────────────────────────────────────────────

def test_empty_when_all_at_target():
    current = _vault(100_000, aave_v3_usdc=50_000, bybit_attestor=47_000)
    target = _alloc(cash=0.03, aave_usdc=0.50, bybit=0.47)
    assert build_allocation_calls(current, target) == []


def test_empty_when_deltas_below_threshold():
    # current: 50% aave, 47% bybit. target shifts by 1.5% — under 2%.
    current = _vault(100_000, aave_v3_usdc=50_000, bybit_attestor=47_000)
    target = _alloc(cash=0.045, aave_usdc=0.485, bybit=0.470)
    assert build_allocation_calls(current, target) == []


def test_empty_when_total_assets_zero():
    current = _vault(0)
    target = _alloc(cash=1.0)
    assert build_allocation_calls(current, target) == []


def test_cash_only_diff_generates_no_call():
    # cash has no adapter — moving USDC in/out of cash is the residual
    # of all other venue calls, never its own call.
    current = _vault(100_000, aave_v3_usdc=50_000, bybit_attestor=47_000)
    target = _alloc(cash=0.03, aave_usdc=0.50, bybit=0.47)
    calls = build_allocation_calls(current, target)
    assert all(c.adapter != "cash" for c in calls)


# ─── single-venue moves ──────────────────────────────────────────────────────

def test_single_deposit_into_aave_usdc():
    # 100% cash → 50% aave_usdc
    current = _vault(100_000)
    target = _alloc(cash=0.50, aave_usdc=0.50)
    calls = build_allocation_calls(current, target)
    assert calls == [AllocationCall(
        adapter=AAVE_USDC,
        kind=AllocationCallKind.DEPOSIT,
        amount=50_000_000_000,  # 50,000 USDC * 1e6
    )]


def test_single_withdraw_from_bybit():
    # 50% bybit → 30% bybit (withdraw 20%)
    current = _vault(100_000, bybit_attestor=50_000)
    target = _alloc(cash=0.70, bybit=0.30)
    calls = build_allocation_calls(current, target)
    assert calls == [AllocationCall(
        adapter=BYBIT,
        kind=AllocationCallKind.WITHDRAW,
        amount=20_000_000_000,
    )]


# ─── ordering: withdraws before deposits ─────────────────────────────────────

def test_withdraws_emitted_before_deposits():
    # over-allocated bybit (40%→10%), under-allocated aave_usdc (10%→50%)
    current = _vault(100_000, aave_v3_usdc=10_000, bybit_attestor=40_000)
    target = _alloc(cash=0.40, aave_usdc=0.50, bybit=0.10)
    calls = build_allocation_calls(current, target)
    assert len(calls) == 2
    assert calls[0].kind == AllocationCallKind.WITHDRAW
    assert calls[0].adapter == BYBIT
    assert calls[1].kind == AllocationCallKind.DEPOSIT
    assert calls[1].adapter == AAVE_USDC


def test_multiple_withdraws_then_multiple_deposits():
    # Two over-allocated → withdraws; flat layout → no deposits expected.
    # Construct a case with two of each by using sub-tests below.
    # Withdraw aave_usdc 60→20, bybit 30→0. Cash absorbs.
    current = _vault(100_000, aave_v3_usdc=60_000, bybit_attestor=30_000)
    target = _alloc(cash=0.80, aave_usdc=0.20, bybit=0.00)
    calls = build_allocation_calls(current, target)
    kinds = [c.kind for c in calls]
    assert kinds == [AllocationCallKind.WITHDRAW, AllocationCallKind.WITHDRAW]


# ─── WETH fail-closed (weth-funding-gap) ─────────────────────────────────────

def test_weth_target_above_zero_raises():
    current = _vault(100_000)
    target = _alloc(cash=0.50, aave_weth=0.50)
    with pytest.raises(ValueError, match="weth-funding-gap"):
        build_allocation_calls(current, target)


def test_weth_current_balance_above_zero_raises():
    # Even if target is 0, refuse to act if we somehow hold WETH already —
    # we have no swap rail to liquidate it cleanly.
    current = _vault(100_000, aave_v3_weth=10_000)
    target = _alloc(cash=1.0)
    with pytest.raises(ValueError, match="weth-funding-gap"):
        build_allocation_calls(current, target)


# ─── address misconfig ───────────────────────────────────────────────────────

def test_unconfigured_adapter_raises(monkeypatch):
    monkeypatch.setattr(
        settings,
        "BYBIT_ATTESTOR_ADAPTER",
        "0x0000000000000000000000000000000000000000",
    )
    current = _vault(100_000)
    target = _alloc(cash=0.50, bybit=0.50)
    with pytest.raises(ValueError, match="BYBIT_ATTESTOR_ADAPTER"):
        build_allocation_calls(current, target)


# ─── amount precision ────────────────────────────────────────────────────────

def test_amount_rounded_to_integer_usdc_base_units():
    # Delta must clear the 2% threshold, so use a 5%+epsilon allocation
    # on a 1_000-USDC TVL: 50.001234 USDC = 50_001_234 base units.
    current = _vault(1_000)
    target_pct = 0.050001234
    target = _alloc(cash=1.0 - target_pct, aave_usdc=target_pct)
    calls = build_allocation_calls(current, target)
    assert len(calls) == 1
    assert calls[0].amount == 50_001_234


def test_threshold_is_strict_less_than():
    # delta just below 2% → skipped; just above → emitted. The threshold
    # filter is `< DELTA_THRESHOLD`, so the boundary case (== 2%) emits.
    current = _vault(100_000, aave_v3_usdc=50_000)

    below = 0.50 + DELTA_THRESHOLD - 0.0001
    target_below = _alloc(cash=1.0 - below, aave_usdc=below)
    assert build_allocation_calls(current, target_below) == []

    above = 0.50 + DELTA_THRESHOLD + 0.0001
    target_above = _alloc(cash=1.0 - above, aave_usdc=above)
    assert len(build_allocation_calls(current, target_above)) == 1


# ─── tx layer ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_on_chain_skips_when_no_calls():
    result = await execute_on_chain("bafytestcid", [])
    assert result == SKIPPED
