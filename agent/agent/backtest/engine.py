import pandas as pd
from datetime import datetime

from agent.backtest.models import PortfolioState, DayResult, BacktestResult
from agent.backtest.state import row_to_market_data
from agent.backtest.simulator import accrue_apy, rebalance
from agent.backtest.metrics import compute_metrics
from agent.backtest.policy import PolicyProtocol
from agent.data.storage import load_parquet


def run_backtest(
    policy: PolicyProtocol,
    initial_capital_usd: float = 1000.0,
    dataset_name: str = "daily_clean",
) -> BacktestResult:
    df = load_parquet(dataset_name, "processed")
    if "date" in df.columns:
        df = df.set_index("date")
    df = df.sort_index().fillna(0.0)

    portfolio = PortfolioState(
        date=pd.to_datetime(df.index[0]).to_pydatetime(),
        meth_usd=0.0, cmeth_usd=0.0, susde_usd=0.0,
        aave_usdc_usd=0.0, cash_usd=initial_capital_usd,
    )

    days: list[DayResult] = []
    for idx, row in df.iterrows():
        date: datetime = pd.to_datetime(idx).to_pydatetime()
        market = row_to_market_data(row, date)
        current_alloc = portfolio.to_allocation()

        target = policy.decide(market, current_alloc)

        portfolio_rebalanced, slip = rebalance(portfolio, target, market)
        portfolio_after = accrue_apy(portfolio_rebalanced, market)
        portfolio_after = portfolio_after.model_copy(update={"date": date})

        days.append(DayResult(
            date=date,
            portfolio=portfolio_after,
            target_allocation={
                "meth":      target.mETH_staked,
                "cmeth":     target.cmETH,
                "susde":     target.sUSDe,
                "aave_usdc": target.lendle_usdc,
                "cash":      target.cash,
            },
            rebalanced=slip > 0,
            rebalance_cost_usd=slip,
            skipped=False,
        ))
        portfolio = portfolio_after

    m = compute_metrics(days, initial_capital_usd)
    return BacktestResult(
        policy_name=policy.name,
        initial_capital_usd=initial_capital_usd,
        final_capital_usd=days[-1].portfolio.total_usd if days else initial_capital_usd,
        days=days,
        **m,
    )
