from agent.backtest.models import PortfolioState
from agent.backtest.models import LegacyTargetAllocation
from agent.gather.models import MarketData

# Slippage assumptions per asset class (in bps; 1 bp = 0.01%)
SLIPPAGE_BPS = {
    "meth":      10,  # 0.10% — deep pool
    "cmeth":     30,  # 0.30% — thinner
    "susde":     50,  # 0.50% — instant exit via Curve (no cooldown modelled in Week 1)
    "aave_usdc":  5,  # 0.05% — supply/withdraw near-zero slip
    "cash":       0,
}

REBALANCE_THRESHOLD = 0.02  # skip rebalance if total absolute drift < 2× this


def accrue_apy(portfolio: PortfolioState, market: MarketData) -> PortfolioState:
    """Apply one day of compound yield to each position."""
    def daily(apy: float) -> float:
        return (1 + apy) ** (1 / 365) - 1

    return portfolio.model_copy(update={
        "meth_usd":      portfolio.meth_usd      * (1 + daily(market.meth_apy)),
        "cmeth_usd":     portfolio.cmeth_usd     * (1 + daily(market.cmeth_apy)),
        "susde_usd":     portfolio.susde_usd     * (1 + daily(market.susde_apy)),
        "aave_usdc_usd": portfolio.aave_usdc_usd * (1 + daily(market.aave_usdc_apy)),
        "cash_usd":      portfolio.cash_usd,
    })


def rebalance(
    portfolio: PortfolioState,
    target: LegacyTargetAllocation,
    market: MarketData,
) -> tuple[PortfolioState, float]:
    """
    Rebalance portfolio toward target allocation.
    Returns (new_portfolio, slippage_cost_usd).
    Returns (original_portfolio, 0.0) if drift is below threshold.
    """
    total = portfolio.total_usd
    if total == 0:
        return portfolio, 0.0

    target_dict = {
        "meth":      target.mETH_staked,
        "cmeth":     target.cmETH,
        "susde":     target.sUSDe,
        "aave_usdc": target.lendle_usdc,
        "cash":      target.cash,
    }
    current_dict = portfolio.to_allocation()

    total_delta = sum(abs(target_dict[k] - current_dict[k]) for k in target_dict) * total

    if total_delta / total < REBALANCE_THRESHOLD * 2:
        return portfolio, 0.0

    slippage = sum(
        abs(target_dict[k] - current_dict[k]) * total * SLIPPAGE_BPS[k] / 10_000
        for k in target_dict
    )
    net = total - slippage

    return portfolio.model_copy(update={
        "meth_usd":      target_dict["meth"]      * net,
        "cmeth_usd":     target_dict["cmeth"]     * net,
        "susde_usd":     target_dict["susde"]     * net,
        "aave_usdc_usd": target_dict["aave_usdc"] * net,
        "cash_usd":      target_dict["cash"]      * net,
    }), slippage
