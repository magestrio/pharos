import httpx
import pytest

from agent.bybit_oracle.bybit_client import BybitClient
from agent.gather import bybit as bybit_gather
from agent.gather.bybit import (
    _orderbook_depth_usd_within_band,
    get_bybit_earn_products,
    get_bybit_positions,
    get_perp_market_data,
)


API_KEY = "test-key"
API_SECRET = "test-secret"


def _ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


def _err(code: int = 10001, msg: str = "fail") -> httpx.Response:
    return httpx.Response(200, json={"retCode": code, "retMsg": msg, "result": {}})


def _make_client(responder) -> BybitClient:
    return BybitClient(
        api_key=API_KEY,
        api_secret=API_SECRET,
        base_url="https://api.bybit.com",
        recv_window=5000,
        transport=httpx.MockTransport(responder),
    )


@pytest.fixture
def patch_open_client(monkeypatch):
    """Force `_open_client` to return a specific BybitClient (or None)."""
    def _set(client_or_none):
        monkeypatch.setattr(bybit_gather, "_open_client", lambda: client_or_none)
    return _set


# ─── earn products ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_earn_products_filtered_sorted_and_marked_available(patch_open_client):
    def handler(request):
        coin = request.url.params.get("coin")
        if coin == "USDC":
            return _ok({"list": [
                {"productId": "u1", "coin": "USDC", "category": "FlexibleSaving", "estimateApr": "5.5"},
                {"productId": "u2", "coin": "USDC", "category": "FlexibleSaving", "estimateApr": "8.0"},
            ]})
        if coin == "USDT":
            return _ok({"list": [
                {"productId": "t1", "coin": "USDT", "category": "FlexibleSaving", "estimateApr": "6.0"},
            ]})
        return _ok({"list": []})

    patch_open_client(_make_client(handler))
    snap = await get_bybit_earn_products()
    assert snap.is_available is True
    aprs = [p.estimateApr for p in snap.products]
    assert aprs == [8.0, 6.0, 5.5]


@pytest.mark.asyncio
async def test_earn_products_fails_soft_when_creds_missing(patch_open_client):
    patch_open_client(None)
    snap = await get_bybit_earn_products()
    assert snap.is_available is False
    assert snap.products == []


@pytest.mark.asyncio
async def test_earn_products_fails_soft_on_api_error(patch_open_client):
    patch_open_client(_make_client(lambda r: _err()))
    snap = await get_bybit_earn_products()
    assert snap.is_available is False
    assert snap.products == []


@pytest.mark.asyncio
async def test_earn_products_caps_to_max(patch_open_client, monkeypatch):
    monkeypatch.setattr(bybit_gather, "MAX_EARN_PRODUCTS", 2)
    def handler(request):
        if request.url.params.get("coin") == "USDC":
            return _ok({"list": [
                {"productId": f"u{i}", "coin": "USDC", "category": "FlexibleSaving", "estimateApr": str(i)}
                for i in range(1, 10)
            ]})
        return _ok({"list": []})
    patch_open_client(_make_client(handler))
    snap = await get_bybit_earn_products()
    assert len(snap.products) == 2
    assert snap.products[0].estimateApr == 9.0  # top APR first
    assert snap.products[1].estimateApr == 8.0


# ─── positions ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_positions_passthrough(patch_open_client):
    handler = lambda r: _ok({"list": [
        {"productId": "p1", "coin": "USDC", "amount": "1000.50", "category": "FlexibleSaving"},
    ]})
    patch_open_client(_make_client(handler))
    snap = await get_bybit_positions()
    assert snap.is_available is True
    assert len(snap.positions) == 1
    assert snap.positions[0].amount == "1000.50"


@pytest.mark.asyncio
async def test_positions_fails_soft(patch_open_client):
    patch_open_client(None)
    snap = await get_bybit_positions()
    assert snap.is_available is False
    assert snap.positions == []


# ─── orderbook depth arithmetic (pure) ───────────────────────────────────────

def test_depth_sums_only_levels_inside_band():
    bids = [["100.00", "10"], ["99.90", "5"], ["99.00", "100"]]  # last outside ±50bps
    asks = [["100.10", "20"], ["101.00", "30"]]                  # last outside ±50bps
    # Band [99.5, 100.5]: bids 100*10 + 99.9*5 = 1499.5; asks 100.1*20 = 2002
    total = _orderbook_depth_usd_within_band(bids, asks, mark_price=100.0, band_pct=0.005)
    assert total == pytest.approx(3501.5)


def test_depth_ignores_malformed_levels():
    bids = [["100", "10"], ["bad", "20"], ["99.95"]]  # bad: not numeric; last: no size
    total = _orderbook_depth_usd_within_band(bids, [], mark_price=100.0, band_pct=0.005)
    assert total == 1000.0


def test_depth_zero_when_nothing_in_band():
    bids = [["50.00", "100"]]
    asks = [["150.00", "100"]]
    total = _orderbook_depth_usd_within_band(bids, asks, mark_price=100.0, band_pct=0.005)
    assert total == 0.0


# ─── perp market data ────────────────────────────────────────────────────────

def _perp_handler(symbol_to_data: dict, default_error: bool = False):
    def handler(request):
        symbol = request.url.params.get("symbol")
        if default_error and symbol not in symbol_to_data:
            return _err()
        data = symbol_to_data.get(symbol, {})
        path = request.url.path
        if "tickers" in path:
            return _ok({"list": [{
                "symbol": symbol,
                "lastPrice": data.get("price", "100"),
                "markPrice": data.get("price", "100"),
                "fundingRate": data.get("funding", "0.0001"),
            }]})
        if "orderbook" in path:
            return _ok({
                "s": symbol,
                "b": data.get("bids", [["100", "5"]]),
                "a": data.get("asks", [["100.1", "5"]]),
            })
        if "instruments-info" in path:
            return _ok({"list": [{
                "symbol": symbol,
                "leverageFilter": {"maxLeverage": data.get("leverage", "50"), "minLeverage": "1"},
            }]})
        return _ok({})
    return handler


@pytest.mark.asyncio
async def test_perp_market_happy_path(patch_open_client):
    patch_open_client(_make_client(_perp_handler({
        "SOLUSDT": {"price": "100", "funding": "0.0001", "leverage": "50"},
        "ETHUSDT": {"price": "3000", "funding": "0.0002", "leverage": "100"},
    })))
    snap = await get_perp_market_data()
    assert snap.is_available is True
    assert len(snap.venues) == 2
    sol = next(v for v in snap.venues if v.symbol == "SOLUSDT")
    eth = next(v for v in snap.venues if v.symbol == "ETHUSDT")
    assert sol.mark_price == 100.0
    assert sol.funding_rate_8h == 0.0001
    assert sol.max_leverage == 50.0
    assert sol.orderbook_depth_usd_50bps > 0
    assert eth.mark_price == 3000.0
    assert eth.max_leverage == 100.0


@pytest.mark.asyncio
async def test_perp_market_per_symbol_failure_is_isolated(patch_open_client):
    """When SOL request fails, ETH still lands in the snapshot."""
    handler = _perp_handler(
        symbol_to_data={"ETHUSDT": {"price": "3000", "funding": "0.0002", "leverage": "100"}},
        default_error=True,
    )
    patch_open_client(_make_client(handler))
    snap = await get_perp_market_data()
    assert snap.is_available is True
    assert len(snap.venues) == 2
    sol = next(v for v in snap.venues if v.symbol == "SOLUSDT")
    eth = next(v for v in snap.venues if v.symbol == "ETHUSDT")
    assert sol.mark_price is None
    assert sol.funding_rate_8h is None
    assert eth.mark_price == 3000.0


@pytest.mark.asyncio
async def test_perp_market_fails_soft_when_creds_missing(patch_open_client):
    patch_open_client(None)
    snap = await get_perp_market_data()
    assert snap.is_available is False
    assert snap.venues == []
