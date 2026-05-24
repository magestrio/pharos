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


class BybitEarnProductView(BaseModel):
    """A filtered, prompt-friendly view of a Bybit Earn product."""

    productId: str
    coin: str
    category: str
    estimateApr: Optional[float] = None  # parsed from string, percent
    minStakeAmount: Optional[str] = None


class BybitEarnSnapshot(BaseModel):
    """Filtered list of FlexibleSaving USDC/USDT products. `is_available`
    is False when the API was unreachable or credentials missing — Reason
    phase must treat `products=[]` plus `is_available=False` as 'no Earn
    data this cycle', NOT 'no products exist'."""

    products: list[BybitEarnProductView] = []
    is_available: bool
    timestamp: datetime


class BybitPositionView(BaseModel):
    productId: str
    coin: str
    amount: str  # decimal-string, native Bybit convention
    category: Optional[str] = None


class BybitPositionsSnapshot(BaseModel):
    positions: list[BybitPositionView] = []
    is_available: bool
    timestamp: datetime


class PerpVenueData(BaseModel):
    """Per-symbol perp market snapshot."""

    symbol: str
    mark_price: Optional[float] = None
    funding_rate_8h: Optional[float] = None  # signed decimal (e.g. 0.0001 = 1bps/8h)
    orderbook_depth_usd_50bps: Optional[float] = None  # bid+ask USD volume within ±50bps of mark
    max_leverage: Optional[float] = None


class PerpMarketData(BaseModel):
    """Hedge-feasibility snapshot for the perps the agent can use."""

    venues: list[PerpVenueData] = []
    is_available: bool
    timestamp: datetime
