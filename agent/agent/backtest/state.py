import pandas as pd
from datetime import datetime
from agent.gather.models import MarketData


def _f(val, default: float = 0.0) -> float:
    v = float(val)
    return default if v != v else v  # NaN check: NaN != NaN


def row_to_market_data(row: pd.Series, date: datetime) -> MarketData:
    """Convert a parquet row into MarketData for policy consumption."""
    eth_price = _f(row["eth_price"], 1.0)
    meth_price = _f(row["meth_price"], eth_price)
    return MarketData(
        eth_price_usd=eth_price,
        meth_price_usd=meth_price,
        meth_eth_ratio=meth_price / eth_price if eth_price else 1.0,
        meth_exchange_rate=_f(row.get("meth_exchange_rate", 1.0), 1.0),
        meth_apy=_f(row["meth_apy"]),
        cmeth_apy=_f(row["cmeth_apy"]),
        susde_apy=_f(row["susde_apy"]),
        aave_usdc_apy=_f(row["aave_usdc_apy"]),
        funding_rate_8h=_f(row["funding_rate_8h"]),
        funding_rate_7d_avg=_f(row["funding_rate_7d_avg"]),
        mantle_tvl_usd=_f(row["mantle_tvl"]),
        timestamp=date,
    )
