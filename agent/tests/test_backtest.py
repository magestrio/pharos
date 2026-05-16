import pytest
from datetime import datetime

from agent.backtest.policy import DummyPolicy, HumanPMPolicy
from agent.backtest.engine import run_backtest
from agent.backtest.models import PortfolioState


def test_dummy_policy_runs():
    """Smoke test: full backtest run completes without errors."""
    result = run_backtest(DummyPolicy(), initial_capital_usd=1000.0, dataset_name="daily_clean")
    assert result.policy_name == "Dummy_Static"
    assert len(result.days) > 0
    assert result.final_capital_usd > 0


def test_human_pm_runs():
    result = run_backtest(HumanPMPolicy(), initial_capital_usd=1000.0, dataset_name="daily_clean")
    assert result.policy_name == "Human_PM"
    assert len(result.days) > 0


def test_portfolio_total_invariant():
    """to_allocation() fractions sum to 1.0."""
    p = PortfolioState(
        date=datetime.now(),
        meth_usd=400, cmeth_usd=200, susde_usd=250,
        aave_usdc_usd=100, cash_usd=50,
    )
    alloc = p.to_allocation()
    assert abs(sum(alloc.values()) - 1.0) < 1e-9
