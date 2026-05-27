from pydantic import BaseModel, Field, computed_field
from datetime import datetime
from typing import Optional


class LegacyTargetAllocation(BaseModel):
    """Pre-vUSDC-pivot venue set kept for backtest baselines whose asset
    universe (mETH/cmETH/sUSDe/Lendle USDC) lives only in the historical
    parquet data under `data/processed/`. Not used by the live agent —
    the production path uses `agent.reason.schema.Decision` instead.
    """

    mETH_staked: float = Field(ge=0, le=1)
    cmETH: float = Field(ge=0, le=1)
    sUSDe: float = Field(ge=0, le=1)
    lendle_usdc: float = Field(ge=0, le=1)
    cash: float = Field(ge=0.03, le=1)


class PortfolioState(BaseModel):
    date: datetime
    meth_usd: float
    cmeth_usd: float
    susde_usd: float
    aave_usdc_usd: float
    cash_usd: float

    @computed_field
    @property
    def total_usd(self) -> float:
        return self.meth_usd + self.cmeth_usd + self.susde_usd + self.aave_usdc_usd + self.cash_usd

    def to_allocation(self) -> dict[str, float]:
        """Fractional allocations summing to 1.0."""
        total = self.total_usd
        if total == 0:
            return {k: 0.0 for k in ["meth", "cmeth", "susde", "aave_usdc", "cash"]}
        return {
            "meth":      self.meth_usd / total,
            "cmeth":     self.cmeth_usd / total,
            "susde":     self.susde_usd / total,
            "aave_usdc": self.aave_usdc_usd / total,
            "cash":      self.cash_usd / total,
        }


class DayResult(BaseModel):
    date: datetime
    portfolio: PortfolioState
    target_allocation: dict[str, float]
    rebalanced: bool
    rebalance_cost_usd: float
    skipped: bool
    skip_reason: Optional[str] = None


class BacktestResult(BaseModel):
    policy_name: str
    initial_capital_usd: float
    final_capital_usd: float
    total_return_pct: float
    annualized_apr_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate_vs_benchmark: Optional[float] = None
    rebalance_count: int
    skip_count: int
    days: list[DayResult]
