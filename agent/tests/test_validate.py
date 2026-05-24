import pytest

from agent.reason.schema import Decision, TargetAllocation
from agent.validate import RiskContext, validate
from agent.validate.rules import (
    check_sum,
    check_cash_usdc,
    check_max_position,
    check_bybit_attestor_cap,
    check_confidence,
    check_risk_flags,
    check_bybit_lag,
    check_usdc_peg,
    check_aave_utilization,
)


def _alloc(**kwargs) -> TargetAllocation:
    defaults = dict(cash_usdc=0.05, aave_v3_usdc=0.25, aave_v3_weth=0.30, bybit_attestor=0.40)
    defaults.update(kwargs)
    return TargetAllocation(**defaults)


def _decision(**kwargs) -> Decision:
    alloc = kwargs.pop("target_allocation", _alloc())
    return Decision(
        thesis="Balanced allocation with sufficient cash buffer and no red flags",
        target_allocation=alloc,
        confidence=kwargs.pop("confidence", 0.8),
        **kwargs,
    )


def _safe_ctx() -> RiskContext:
    """Risk context with no rule triggered."""
    return RiskContext(
        bybit_attestor_lag_minutes=10,
        usdc_peg_deviation_bps=20,
        aave_v3_usdc_utilization=0.70,
        aave_v3_weth_utilization=0.70,
    )


# --- check_sum ---

def test_sum_valid():
    ok, err = check_sum(_alloc())
    assert ok and err is None


def test_sum_invalid():
    bad = _alloc(cash_usdc=0.10, aave_v3_usdc=0.30, aave_v3_weth=0.30, bybit_attestor=0.40)
    ok, err = check_sum(bad)
    assert not ok
    assert "1.10" in err


def test_sum_tolerance():
    alloc = _alloc(cash_usdc=0.0501)  # sum = 1.0001, within ±0.001
    ok, _ = check_sum(alloc)
    assert ok


# --- check_cash_usdc ---

def test_cash_valid():
    ok, _ = check_cash_usdc(_alloc(cash_usdc=0.03, aave_v3_usdc=0.27, aave_v3_weth=0.30, bybit_attestor=0.40))
    assert ok


def test_cash_below_minimum_blocked_at_construction():
    """Field(ge=0.03) on cash_usdc enforces the floor at pydantic level."""
    with pytest.raises(Exception):
        _alloc(cash_usdc=0.02, aave_v3_usdc=0.28, aave_v3_weth=0.30, bybit_attestor=0.40)


# --- check_max_position ---

def test_max_position_valid():
    ok, _ = check_max_position(_alloc())
    assert ok


def test_max_position_at_cap():
    """70% exactly is allowed."""
    ok, _ = check_max_position(_alloc(cash_usdc=0.05, aave_v3_usdc=0.70, aave_v3_weth=0.00, bybit_attestor=0.25))
    assert ok


def test_max_position_violated():
    ok, err = check_max_position(_alloc(cash_usdc=0.05, aave_v3_usdc=0.71, aave_v3_weth=0.00, bybit_attestor=0.24))
    assert not ok
    assert "aave_v3_usdc" in err


def test_max_position_bybit_caught_by_general_cap():
    """A position above 70% in bybit_attestor trips this rule (concentration cap separately)."""
    ok, err = check_max_position(_alloc(cash_usdc=0.05, aave_v3_usdc=0.10, aave_v3_weth=0.10, bybit_attestor=0.75))
    assert not ok
    assert "bybit_attestor" in err


# --- check_bybit_attestor_cap ---

def test_bybit_cap_at_limit():
    ok, _ = check_bybit_attestor_cap(_alloc(cash_usdc=0.05, aave_v3_usdc=0.25, aave_v3_weth=0.20, bybit_attestor=0.50))
    assert ok


def test_bybit_cap_exceeded():
    ok, err = check_bybit_attestor_cap(_alloc(cash_usdc=0.05, aave_v3_usdc=0.20, aave_v3_weth=0.24, bybit_attestor=0.51))
    assert not ok
    assert "bybit_attestor" in err


# --- check_confidence ---

def test_confidence_at_limit():
    ok, _ = check_confidence(_decision(confidence=0.4))
    assert ok


def test_confidence_too_low():
    ok, _ = check_confidence(_decision(confidence=0.39))
    assert not ok


# --- check_risk_flags ---

def test_no_risk_flags():
    ok, _ = check_risk_flags(_decision())
    assert ok


def test_with_risk_flags():
    ok, _ = check_risk_flags(_decision(risk_flags=["usdc_depeg"]))
    assert not ok


# --- check_bybit_lag (conditional) ---

def test_bybit_lag_under_threshold_passes():
    ok, _ = check_bybit_lag(_decision(), RiskContext(bybit_attestor_lag_minutes=10))
    assert ok


def test_bybit_lag_at_threshold_passes():
    """Trigger is strictly > 60min."""
    ok, _ = check_bybit_lag(_decision(), RiskContext(bybit_attestor_lag_minutes=60))
    assert ok


def test_bybit_lag_over_threshold_requires_exit():
    alloc = _alloc(cash_usdc=0.05, aave_v3_usdc=0.25, aave_v3_weth=0.30, bybit_attestor=0.40)
    ok, err = check_bybit_lag(_decision(target_allocation=alloc), RiskContext(bybit_attestor_lag_minutes=61))
    assert not ok
    assert "forced exit" in err


def test_bybit_lag_over_threshold_with_zero_bybit_passes():
    alloc = _alloc(cash_usdc=0.05, aave_v3_usdc=0.45, aave_v3_weth=0.50, bybit_attestor=0.00)
    ok, _ = check_bybit_lag(_decision(target_allocation=alloc), RiskContext(bybit_attestor_lag_minutes=120))
    assert ok


def test_bybit_lag_none_fails_closed():
    """Missing metric is treated as triggered."""
    alloc = _alloc(cash_usdc=0.05, aave_v3_usdc=0.25, aave_v3_weth=0.30, bybit_attestor=0.40)
    ok, err = check_bybit_lag(_decision(target_allocation=alloc), RiskContext(bybit_attestor_lag_minutes=None))
    assert not ok
    assert "unavailable" in err


# --- check_usdc_peg (conditional) ---

def test_peg_under_threshold_passes():
    ok, _ = check_usdc_peg(_decision(), RiskContext(usdc_peg_deviation_bps=50))
    assert ok


def test_peg_at_threshold_passes():
    """Trigger is strictly > 100bps."""
    ok, _ = check_usdc_peg(_decision(), RiskContext(usdc_peg_deviation_bps=100))
    assert ok


def test_peg_over_threshold_with_high_stable_exposure_fails():
    """cash_usdc + aave_v3_usdc > 30% blocks when peg deviated."""
    alloc = _alloc(cash_usdc=0.10, aave_v3_usdc=0.25, aave_v3_weth=0.25, bybit_attestor=0.40)
    ok, err = check_usdc_peg(_decision(target_allocation=alloc), RiskContext(usdc_peg_deviation_bps=150))
    assert not ok
    assert "stablecoin exposure" in err


def test_peg_over_threshold_with_reduced_stable_exposure_passes():
    """cash_usdc + aave_v3_usdc ≤ 30% allowed even with peg deviation."""
    alloc = _alloc(cash_usdc=0.05, aave_v3_usdc=0.20, aave_v3_weth=0.35, bybit_attestor=0.40)
    ok, _ = check_usdc_peg(_decision(target_allocation=alloc), RiskContext(usdc_peg_deviation_bps=200))
    assert ok


def test_peg_none_fails_closed():
    alloc = _alloc(cash_usdc=0.10, aave_v3_usdc=0.25, aave_v3_weth=0.25, bybit_attestor=0.40)
    ok, err = check_usdc_peg(_decision(target_allocation=alloc), RiskContext(usdc_peg_deviation_bps=None))
    assert not ok
    assert "unavailable" in err


# --- check_aave_utilization (conditional) ---

def test_util_under_threshold_passes():
    ctx = RiskContext(aave_v3_usdc_utilization=0.90, aave_v3_weth_utilization=0.80)
    ok, _ = check_aave_utilization(_decision(), ctx)
    assert ok


def test_util_at_threshold_passes():
    ctx = RiskContext(aave_v3_usdc_utilization=0.95, aave_v3_weth_utilization=0.95)
    ok, _ = check_aave_utilization(_decision(), ctx)
    assert ok


def test_util_usdc_over_threshold_requires_exit():
    alloc = _alloc(cash_usdc=0.05, aave_v3_usdc=0.25, aave_v3_weth=0.30, bybit_attestor=0.40)
    ctx = RiskContext(aave_v3_usdc_utilization=0.96, aave_v3_weth_utilization=0.80)
    ok, err = check_aave_utilization(_decision(target_allocation=alloc), ctx)
    assert not ok
    assert "aave_v3_usdc" in err
    assert "aave_v3_weth" not in err


def test_util_both_pools_over_threshold_reports_both():
    alloc = _alloc(cash_usdc=0.05, aave_v3_usdc=0.25, aave_v3_weth=0.30, bybit_attestor=0.40)
    ctx = RiskContext(aave_v3_usdc_utilization=0.97, aave_v3_weth_utilization=0.98)
    ok, err = check_aave_utilization(_decision(target_allocation=alloc), ctx)
    assert not ok
    assert "aave_v3_usdc" in err and "aave_v3_weth" in err


def test_util_over_threshold_with_zero_target_passes():
    alloc = _alloc(cash_usdc=0.30, aave_v3_usdc=0.00, aave_v3_weth=0.30, bybit_attestor=0.40)
    ctx = RiskContext(aave_v3_usdc_utilization=0.99, aave_v3_weth_utilization=0.80)
    ok, _ = check_aave_utilization(_decision(target_allocation=alloc), ctx)
    assert ok


def test_util_none_fails_closed():
    alloc = _alloc(cash_usdc=0.05, aave_v3_usdc=0.25, aave_v3_weth=0.30, bybit_attestor=0.40)
    ctx = RiskContext(aave_v3_usdc_utilization=None, aave_v3_weth_utilization=None)
    ok, err = check_aave_utilization(_decision(target_allocation=alloc), ctx)
    assert not ok
    assert "unavailable" in err


# --- aggregate validate() ---

def test_validate_passes():
    ok, errors = validate(_decision(), _safe_ctx())
    assert ok
    assert errors == []


def test_validate_fails_multiple():
    bad = _alloc(cash_usdc=0.05, aave_v3_usdc=0.05, aave_v3_weth=0.15, bybit_attestor=0.75)  # bybit > both caps
    d = Decision(
        thesis="Aggressive allocation ignoring hard caps completely",
        target_allocation=bad,
        confidence=0.3,
        risk_flags=["usdc_depeg"],
    )
    ok, errors = validate(d, _safe_ctx())
    assert not ok
    # bybit > 70% (max_position) + bybit > 50% (concentration) + confidence + risk_flags = at least 4
    assert len(errors) >= 4


def test_validate_fails_on_missing_risk_metrics():
    """Default RiskContext (all None) fails closed on every conditional rule."""
    alloc = _alloc(cash_usdc=0.10, aave_v3_usdc=0.25, aave_v3_weth=0.25, bybit_attestor=0.40)
    ok, errors = validate(_decision(target_allocation=alloc), RiskContext())
    assert not ok
    # bybit_lag + usdc_peg + aave_utilization (both pools rolled into one error string)
    assert len(errors) == 3
