import numpy as np
from agent.backtest.models import DayResult


def compute_metrics(days: list[DayResult], initial_capital: float) -> dict:
    """Return performance metrics dict for BacktestResult construction."""
    if not days:
        return {
            "total_return_pct": 0.0, "annualized_apr_pct": 0.0,
            "sharpe_ratio": 0.0, "max_drawdown_pct": 0.0,
            "rebalance_count": 0, "skip_count": 0,
        }

    equity = np.array([d.portfolio.total_usd for d in days])
    final = equity[-1]
    n_days = len(days)

    total_return = (final / initial_capital - 1) * 100
    annualized_apr = ((final / initial_capital) ** (365 / n_days) - 1) * 100

    returns = np.diff(equity) / equity[:-1]
    std = float(np.std(returns))
    sharpe = float(np.mean(returns) / std * np.sqrt(365)) if len(returns) > 1 and std > 0 else 0.0

    running_max = np.maximum.accumulate(equity)
    drawdowns = (equity - running_max) / running_max
    max_dd = float(drawdowns.min()) * 100

    return {
        "total_return_pct":  float(total_return),
        "annualized_apr_pct": float(annualized_apr),
        "sharpe_ratio":       sharpe,
        "max_drawdown_pct":   max_dd,
        "rebalance_count":    sum(1 for d in days if d.rebalanced),
        "skip_count":         sum(1 for d in days if d.skipped),
    }
