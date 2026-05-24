from datetime import datetime, timezone

from agent.bybit_oracle.bybit_client import BybitClient
from agent.gather.models import (
    BybitEarnProductView,
    BybitEarnSnapshot,
    BybitPositionView,
    BybitPositionsSnapshot,
    PerpMarketData,
    PerpVenueData,
)


# Symbols matching `bybit_sub_allocation.{sol|eth}_basis_trade`.
PERP_SYMBOLS = ("SOLUSDT", "ETHUSDT")

# USD-pegged coins the agent can realistically Stake from a USDC-base vault.
EARN_COINS = ("USDC", "USDT")
EARN_CATEGORY = "FlexibleSaving"

# Context-budget guard: even after coin/category filter Bybit lists many
# tenor variants. Top N by APR is sufficient for the Reason phase.
MAX_EARN_PRODUCTS = 30

# Depth band for hedge-feasibility check: ±50bps around mark price.
DEPTH_BAND = 0.005


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_float(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _open_client() -> BybitClient | None:
    """Returns a configured BybitClient or None if credentials are absent.
    Caller treats None as 'fail-soft, return empty snapshot'."""
    try:
        return BybitClient.from_settings()
    except Exception:
        return None


async def get_bybit_earn_products() -> BybitEarnSnapshot:
    client = _open_client()
    if client is None:
        return BybitEarnSnapshot(is_available=False, timestamp=_now())

    try:
        async with client:
            collected: list[BybitEarnProductView] = []
            for coin in EARN_COINS:
                products = await client.list_earn_products(
                    category=EARN_CATEGORY, coin=coin
                )
                for p in products:
                    collected.append(BybitEarnProductView(
                        productId=p.productId,
                        coin=p.coin,
                        category=p.category,
                        estimateApr=_parse_float(p.estimateApr),
                        minStakeAmount=p.minStakeAmount,
                    ))
        collected.sort(key=lambda v: v.estimateApr or 0.0, reverse=True)
        return BybitEarnSnapshot(
            products=collected[:MAX_EARN_PRODUCTS],
            is_available=True,
            timestamp=_now(),
        )
    except Exception:
        return BybitEarnSnapshot(is_available=False, timestamp=_now())


async def get_bybit_positions() -> BybitPositionsSnapshot:
    client = _open_client()
    if client is None:
        return BybitPositionsSnapshot(is_available=False, timestamp=_now())

    try:
        async with client:
            positions = await client.get_earn_positions()
        return BybitPositionsSnapshot(
            positions=[BybitPositionView(
                productId=p.productId,
                coin=p.coin,
                amount=p.amount,
                category=p.category,
            ) for p in positions],
            is_available=True,
            timestamp=_now(),
        )
    except Exception:
        return BybitPositionsSnapshot(is_available=False, timestamp=_now())


def _orderbook_depth_usd_within_band(
    bids: list[list[str]],
    asks: list[list[str]],
    mark_price: float,
    band_pct: float,
) -> float:
    low = mark_price * (1 - band_pct)
    high = mark_price * (1 + band_pct)
    total = 0.0
    for side in (bids, asks):
        for level in side:
            try:
                price = float(level[0])
                size = float(level[1])
            except (ValueError, IndexError, TypeError):
                continue
            if low <= price <= high:
                total += price * size
    return total


async def get_perp_market_data() -> PerpMarketData:
    """For each PERP_SYMBOL fetch funding/mark, orderbook depth in ±50bps,
    and max leverage. Per-symbol fail-soft: a single symbol's failure
    yields a stubbed PerpVenueData with all-None fields rather than
    losing the whole snapshot."""
    client = _open_client()
    if client is None:
        return PerpMarketData(is_available=False, timestamp=_now())

    venues: list[PerpVenueData] = []
    try:
        async with client:
            for symbol in PERP_SYMBOLS:
                try:
                    tickers = await client.get_tickers(category="linear", symbol=symbol)
                    ticker = tickers[0] if tickers else None
                    book = await client.get_orderbook(symbol=symbol, category="linear", limit=50)
                    instruments = await client.get_instruments_info(category="linear", symbol=symbol)
                    instrument = instruments[0] if instruments else None

                    mark = _parse_float(ticker.markPrice) if ticker else None
                    funding = _parse_float(ticker.fundingRate) if ticker else None
                    depth = None
                    if mark is not None and book is not None:
                        depth = _orderbook_depth_usd_within_band(book.b, book.a, mark, DEPTH_BAND)
                    max_lev = None
                    if instrument and instrument.leverageFilter:
                        max_lev = _parse_float(instrument.leverageFilter.maxLeverage)

                    venues.append(PerpVenueData(
                        symbol=symbol,
                        mark_price=mark,
                        funding_rate_8h=funding,
                        orderbook_depth_usd_50bps=depth,
                        max_leverage=max_lev,
                    ))
                except Exception:
                    venues.append(PerpVenueData(symbol=symbol))
        return PerpMarketData(venues=venues, is_available=True, timestamp=_now())
    except Exception:
        return PerpMarketData(is_available=False, timestamp=_now())
