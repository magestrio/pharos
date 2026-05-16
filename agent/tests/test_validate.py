import pytest

from agent.reason.schema import Decision, TargetAllocation
from agent.validate.rules import validate, check_sum, check_cash, check_max_position, check_susde, check_confidence, check_risk_flags


def _alloc(**kwargs) -> TargetAllocation:
    defaults = dict(mETH_staked=0.30, cmETH=0.20, sUSDe=0.20, lendle_usdc=0.25, cash=0.05)
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


# --- check_sum ---

def test_sum_valid():
    ok, err = check_sum(_alloc())
    assert ok and err is None


def test_sum_invalid():
    ok, err = check_sum(_alloc(mETH_staked=0.50, cmETH=0.20, sUSDe=0.20, lendle_usdc=0.20, cash=0.05))
    assert not ok
    assert err is not None


def test_sum_tolerance():
    alloc = _alloc(cash=0.0501)  # tiny rounding, still within 0.001
    total = alloc.mETH_staked + alloc.cmETH + alloc.sUSDe + alloc.lendle_usdc + alloc.cash
    ok, _ = check_sum(alloc)
    assert ok == (abs(total - 1.0) <= 0.001)


# --- check_cash ---

def test_cash_valid():
    ok, err = check_cash(_alloc(cash=0.03))
    assert ok


def test_cash_below_minimum():
    with pytest.raises(Exception):
        _alloc(mETH_staked=0.33, cmETH=0.22, sUSDe=0.22, lendle_usdc=0.22, cash=0.01)


def test_cash_exact_minimum():
    ok, err = check_cash(_alloc(mETH_staked=0.32, cmETH=0.20, sUSDe=0.20, lendle_usdc=0.25, cash=0.03))
    assert ok


# --- check_max_position ---

def test_max_position_valid():
    ok, err = check_max_position(_alloc())
    assert ok


def test_max_position_violated():
    ok, err = check_max_position(_alloc(mETH_staked=0.61, cmETH=0.10, sUSDe=0.10, lendle_usdc=0.10, cash=0.09))
    assert not ok
    assert "mETH_staked" in err


# --- check_susde ---

def test_susde_valid():
    ok, err = check_susde(_alloc(sUSDe=0.50))
    assert ok


def test_susde_exceeded():
    ok, err = check_susde(_alloc(mETH_staked=0.10, cmETH=0.10, sUSDe=0.51, lendle_usdc=0.24, cash=0.05))
    assert not ok


# --- check_confidence ---

def test_confidence_valid():
    ok, err = check_confidence(_decision(confidence=0.4))
    assert ok


def test_confidence_too_low():
    ok, err = check_confidence(_decision(confidence=0.39))
    assert not ok


# --- check_risk_flags ---

def test_no_risk_flags():
    ok, err = check_risk_flags(_decision())
    assert ok


def test_with_risk_flags():
    ok, err = check_risk_flags(_decision(risk_flags=["sUSDe_depeg"]))
    assert not ok


# --- aggregate validate() ---

def test_validate_passes():
    ok, errors = validate(_decision())
    assert ok
    assert errors == []


def test_validate_fails_multiple():
    bad_alloc = _alloc(mETH_staked=0.61, cmETH=0.10, sUSDe=0.10, lendle_usdc=0.10, cash=0.09)
    d = Decision(
        thesis="Aggressive allocation ignoring hard caps completely",
        target_allocation=bad_alloc,
        confidence=0.3,
        risk_flags=["high_vol"],
    )
    ok, errors = validate(d)
    assert not ok
    assert len(errors) >= 2
