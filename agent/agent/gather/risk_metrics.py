from pydantic import BaseModel


class RiskMetrics(BaseModel):
    depeg_risk_mETH: float = 0.0
    depeg_risk_sUSDe: float = 0.0
    liquidity_score: float = 1.0
    smart_contract_risk_score: float = 0.0


async def get_risk_metrics() -> RiskMetrics:
    raise NotImplementedError
