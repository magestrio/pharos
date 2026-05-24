from pydantic import BaseModel, Field, model_validator


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


class BybitSubAllocation(BaseModel):
    flexible_usdc: float = Field(ge=0, le=1)
    sol_basis_trade: float = Field(ge=0, le=1)
    eth_basis_trade: float = Field(ge=0, le=1)
    buffer_cash: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> "BybitSubAllocation":
        total = self.flexible_usdc + self.sol_basis_trade + self.eth_basis_trade + self.buffer_cash
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"bybit_sub_allocation sums to {total:.4f}, expected 1.0 ± 0.001")
        return self


class Decision(BaseModel):
    thesis: str = Field(min_length=20)
    target_allocation: TargetAllocation
    bybit_sub_allocation: BybitSubAllocation | None = None
    confidence: float = Field(ge=0, le=1)
    risk_flags: list[str] = []
    expected_blended_apr_pct: float = Field(ge=0)

    @model_validator(mode="after")
    def _bybit_sub_required_when_active(self) -> "Decision":
        if self.target_allocation.bybit_attestor > 0 and self.bybit_sub_allocation is None:
            raise ValueError(
                "bybit_sub_allocation is required when target_allocation.bybit_attestor > 0"
            )
        return self
