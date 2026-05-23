import hashlib
import hmac
import json

import httpx
import pytest

from agent.bybit_oracle.bybit_client import (
    BybitAPIError,
    BybitClient,
    EarnProduct,
)

API_KEY = "test-key"
API_SECRET = "test-secret"
RECV_WINDOW = 5000


def _expected_signature(timestamp: str, payload: str) -> str:
    msg = (timestamp + API_KEY + str(RECV_WINDOW) + payload).encode()
    return hmac.new(API_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def _ok(result: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"retCode": 0, "retMsg": "OK", "result": result or {}}
    )


@pytest.fixture
def captured() -> list[httpx.Request]:
    return []


def _client(captured: list[httpx.Request], responder) -> BybitClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responder(request)

    return BybitClient(
        api_key=API_KEY,
        api_secret=API_SECRET,
        base_url="https://api.bybit.com",
        recv_window=RECV_WINDOW,
        transport=httpx.MockTransport(_handler),
    )


@pytest.mark.asyncio
async def test_get_signs_query_string_correctly(captured):
    fixture = {"list": [{"productId": "p1", "coin": "USDC", "category": "FlexibleSaving"}]}
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        products = await c.list_earn_products(category="FlexibleSaving", coin="USDC")

    assert len(products) == 1
    assert isinstance(products[0], EarnProduct)
    assert products[0].productId == "p1"

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/v5/earn/product"
    assert dict(req.url.params) == {"category": "FlexibleSaving", "coin": "USDC"}

    ts = req.headers["X-BAPI-TIMESTAMP"]
    assert req.headers["X-BAPI-API-KEY"] == API_KEY
    assert req.headers["X-BAPI-RECV-WINDOW"] == str(RECV_WINDOW)
    expected = _expected_signature(ts, "category=FlexibleSaving&coin=USDC")
    assert req.headers["X-BAPI-SIGN"] == expected


@pytest.mark.asyncio
async def test_get_drops_none_params_from_signature(captured):
    async with _client(captured, lambda _r: _ok({"list": []})) as c:
        await c.list_earn_products()

    req = captured[0]
    assert str(req.url.query, "utf-8") == "" if req.url.query else True
    ts = req.headers["X-BAPI-TIMESTAMP"]
    assert req.headers["X-BAPI-SIGN"] == _expected_signature(ts, "")


@pytest.mark.asyncio
async def test_post_signs_raw_json_body(captured):
    async with _client(
        captured, lambda _r: _ok({"orderId": "ord-1"})
    ) as c:
        await c.place_earn_order(product_id="p1", amount="100", side="Stake")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v5/earn/place-order"
    assert req.headers["Content-Type"] == "application/json"

    body_text = req.content.decode()
    assert json.loads(body_text) == {
        "productId": "p1",
        "amount": "100",
        "orderType": "Stake",
    }
    ts = req.headers["X-BAPI-TIMESTAMP"]
    assert req.headers["X-BAPI-SIGN"] == _expected_signature(ts, body_text)


@pytest.mark.asyncio
async def test_post_uses_compact_json_for_signature(captured):
    """Sign string must match the raw bytes sent on the wire — bybit recomputes
    the HMAC against the exact body it receives, so spaces would break it.
    """
    async with _client(captured, lambda _r: _ok({"id": "wd-1"})) as c:
        await c.withdraw_to_mantle(coin="USDC", amount="10", address="0xabc")

    body_text = captured[0].content.decode()
    assert " " not in body_text


@pytest.mark.asyncio
async def test_ret_code_nonzero_raises(captured):
    def _err(_r):
        return httpx.Response(
            200,
            json={"retCode": 10003, "retMsg": "Invalid API key", "result": {}},
        )

    async with _client(captured, _err) as c:
        with pytest.raises(BybitAPIError) as exc:
            await c.get_wallet_balance()

    assert exc.value.ret_code == 10003
    assert "10003" in str(exc.value)


@pytest.mark.asyncio
async def test_get_wallet_balance_parses_nested_coins(captured):
    payload = {
        "list": [
            {
                "accountType": "UNIFIED",
                "totalEquity": "1234.5",
                "coin": [
                    {"coin": "USDC", "walletBalance": "1000.5", "availableToWithdraw": "1000.5"},
                    {"coin": "USDT", "walletBalance": "234.0"},
                ],
            }
        ]
    }
    async with _client(captured, lambda _r: _ok(payload)) as c:
        accounts = await c.get_wallet_balance(coin="USDC,USDT")

    assert len(accounts) == 1
    assert {c.coin for c in accounts[0].coin} == {"USDC", "USDT"}
    req = captured[0]
    assert dict(req.url.params) == {"accountType": "UNIFIED", "coin": "USDC,USDT"}


@pytest.mark.asyncio
async def test_withdraw_targets_mantle_chain(captured):
    async with _client(captured, lambda _r: _ok({"id": "wd-1"})) as c:
        result = await c.withdraw_to_mantle(
            coin="USDC", amount="50", address="0xdead"
        )

    assert result.id == "wd-1"
    body = json.loads(captured[0].content.decode())
    assert body["chain"] == "MANTLE"
    assert body["accountType"] == "FUND"
    assert body["forceChain"] == 1
    assert body["address"] == "0xdead"


@pytest.mark.asyncio
async def test_place_spot_order_market(captured):
    async with _client(captured, lambda _r: _ok({"orderId": "ord-9"})) as c:
        result = await c.place_spot_order(symbol="ETHUSDT", side="Buy", qty="0.01")

    assert result.orderId == "ord-9"
    body = json.loads(captured[0].content.decode())
    assert body == {
        "category": "spot",
        "symbol": "ETHUSDT",
        "side": "Buy",
        "orderType": "Market",
        "qty": "0.01",
    }


@pytest.mark.asyncio
async def test_place_spot_order_limit_includes_price(captured):
    async with _client(captured, lambda _r: _ok({"orderId": "ord-10"})) as c:
        await c.place_spot_order(
            symbol="ETHUSDT", side="Sell", qty="0.5", order_type="Limit", price="3500"
        )

    body = json.loads(captured[0].content.decode())
    assert body["orderType"] == "Limit"
    assert body["price"] == "3500"


@pytest.mark.asyncio
async def test_get_earn_positions_empty(captured):
    async with _client(captured, lambda _r: _ok({"list": []})) as c:
        positions = await c.get_earn_positions()
    assert positions == []


@pytest.mark.asyncio
async def test_from_settings_requires_credentials():
    from agent.bybit_oracle.config import OracleSettings

    cfg = OracleSettings(_env_file=None)
    with pytest.raises(RuntimeError, match="BYBIT_API_KEY"):
        BybitClient.from_settings(cfg=cfg)
