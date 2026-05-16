import pytest
from agent.gather.market_data import get_market_data
from agent.gather.allora import get_allora_signals


@pytest.mark.asyncio
async def test_market_data_returns_valid():
    """Smoke test: market_data returns real values."""
    m = await get_market_data()
    assert m.eth_price_usd > 100
    assert m.meth_price_usd > 100
    assert 0.5 < m.meth_eth_ratio < 1.5
    assert m.mantle_tvl_usd > 0


@pytest.mark.asyncio
async def test_allora_graceful_without_key(monkeypatch):
    """Without ALLORA_API_KEY all signals are marked is_available=False."""
    monkeypatch.delenv("ALLORA_API_KEY", raising=False)
    signals = await get_allora_signals()
    assert signals.eth_24h.is_available is False
    assert signals.eth_7d.is_available is False
    assert signals.funding_forecast.is_available is False
