from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class AdapterBalance(BaseModel):
    name: str
    address: Optional[str] = None
    balance_assets: float
    pct_of_total: float


class VaultState(BaseModel):
    total_assets_usd: float
    total_supply: float
    share_price: float
    allocations: list[AdapterBalance]
    cash_pct: float
    last_decision_id: Optional[str] = None
    last_decision_timestamp: Optional[datetime] = None


class MarketData(BaseModel):
    eth_price_usd: float
    meth_price_usd: float
    meth_eth_ratio: float
    meth_exchange_rate: float
    meth_apy: float
    cmeth_apy: float
    susde_apy: float
    aave_usdc_apy: float
    funding_rate_8h: float
    funding_rate_7d_avg: float
    mantle_tvl_usd: float
    timestamp: datetime


class AlloraSignal(BaseModel):
    topic_id: int
    topic_name: str
    inference: Optional[float] = None
    confidence_low: Optional[float] = None
    confidence_high: Optional[float] = None
    is_available: bool


class AlloraSignals(BaseModel):
    eth_24h: AlloraSignal
    eth_7d: AlloraSignal
    funding_forecast: AlloraSignal
    timestamp: datetime


class RiskMetrics(BaseModel):
    meth_depeg_bps: float
    susde_funding_7d_avg: float
    susde_funding_is_positive: bool
    aave_usdc_utilization: float
    cmeth_cooldown_active: bool
    oracle_max_staleness_sec: int
    red_flags: list[str]
    timestamp: datetime
