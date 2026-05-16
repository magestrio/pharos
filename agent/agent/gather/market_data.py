from pydantic import BaseModel


class MarketData(BaseModel):
    mETH_apy: float = 0.0
    cmETH_apy: float = 0.0
    susde_funding_7d_avg: float = 0.0
    lendle_usdc_supply_apy: float = 0.0
    mantle_gas_price_gwei: float = 0.0


async def get_market_data() -> MarketData:
    raise NotImplementedError
