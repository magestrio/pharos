from pydantic import BaseModel, Field


class LegacyTargetAllocation(BaseModel):
    """Pre-vUSDC-pivot venue set. Retained for the backtest baselines whose
    asset universe (mETH/cmETH/sUSDe/Lendle USDC) does not exist in the new
    on-chain Execute path. Not used by the live agent."""

    mETH_staked: float = Field(ge=0, le=1)
    cmETH: float = Field(ge=0, le=1)
    sUSDe: float = Field(ge=0, le=1)
    lendle_usdc: float = Field(ge=0, le=1)
    cash: float = Field(ge=0.03, le=1)


class TargetAllocation(BaseModel):
    cash_usdc: float = Field(ge=0.03, le=1)
    aave_v3_usdc: float = Field(ge=0, le=1)
    aave_v3_weth: float = Field(ge=0, le=1)
    bybit_attestor: float = Field(ge=0, le=1)


class Decision(BaseModel):
    thesis: str = Field(min_length=20)
    target_allocation: TargetAllocation
    confidence: float = Field(ge=0, le=1)
    risk_flags: list[str] = []
