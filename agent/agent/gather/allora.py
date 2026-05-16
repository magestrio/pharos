from pydantic import BaseModel


class AlloraSignals(BaseModel):
    eth_price_prediction_24h: float = 0.0
    mETH_yield_signal: float = 0.0
    risk_score: float = 0.0


async def get_allora_signals() -> AlloraSignals:
    raise NotImplementedError
