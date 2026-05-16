from pydantic import BaseModel, Field


class TargetAllocation(BaseModel):
    mETH_staked: float = Field(ge=0, le=1)
    cmETH: float = Field(ge=0, le=1)
    sUSDe: float = Field(ge=0, le=1)
    lendle_usdc: float = Field(ge=0, le=1)
    cash: float = Field(ge=0.03, le=1)


class Decision(BaseModel):
    thesis: str = Field(min_length=20)
    target_allocation: TargetAllocation
    confidence: float = Field(ge=0, le=1)
    risk_flags: list[str] = []
