import asyncio
import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest

from agent.bybit_oracle.bybit_client import (
    BybitAPIError,
    BybitClient,
    BybitOrderError,
    DepositChain,
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


# --- .12c: deposit address + bridge wait -----------------------------------


_DEPOSIT_PAYLOAD = {
    "coin": "USDC",
    "chains": [
        {
            "chain": "ETH",
            "chainType": "ERC20",
            "addressDeposit": "0xeth-address",
            "tagDeposit": "",
        },
        {
            "chain": "MANTLE",
            "chainType": "Mantle",
            "addressDeposit": "0xmantle-address",
            "tagDeposit": "",
        },
    ],
}


@pytest.mark.asyncio
async def test_get_deposit_address_picks_requested_chain(captured):
    async with _client(captured, lambda _r: _ok(_DEPOSIT_PAYLOAD)) as c:
        entry = await c.get_deposit_address(coin="USDC", chain="MANTLE")

    assert isinstance(entry, DepositChain)
    assert entry.chain == "MANTLE"
    assert entry.addressDeposit == "0xmantle-address"

    req = captured[0]
    assert req.url.path == "/v5/asset/deposit/query-address"
    assert dict(req.url.params) == {"coin": "USDC"}


@pytest.mark.asyncio
async def test_get_deposit_address_chain_case_insensitive(captured):
    async with _client(captured, lambda _r: _ok(_DEPOSIT_PAYLOAD)) as c:
        entry = await c.get_deposit_address(coin="USDC", chain="mantle")
    assert entry.chain == "MANTLE"


@pytest.mark.asyncio
async def test_get_deposit_address_missing_chain_raises(captured):
    payload = {"coin": "USDC", "chains": [_DEPOSIT_PAYLOAD["chains"][0]]}  # ETH only
    async with _client(captured, lambda _r: _ok(payload)) as c:
        with pytest.raises(ValueError, match="no deposit address.*MANTLE"):
            await c.get_deposit_address(coin="USDC", chain="MANTLE")


def _wallet_response(unified_balance: str) -> dict:
    return {
        "list": [
            {
                "accountType": "UNIFIED",
                "coin": [{"coin": "USDC", "walletBalance": unified_balance}],
            }
        ]
    }


@pytest.mark.asyncio
async def test_poll_deposit_credited_returns_delta(captured):
    """Baseline 100.0, then 100.0 again (not yet credited), then 150.5
    (credit landed). Caller asked for min_credit=50 → must return 50.5.
    """
    balances = iter([
        _wallet_response("100.0"),  # baseline
        _wallet_response("100.0"),  # not yet
        _wallet_response("150.5"),  # credited
    ])
    async with _client(captured, lambda _r: _ok(next(balances))) as c:
        delta = await c.poll_deposit_credited(
            coin="USDC", min_credit="50", interval_seconds=0
        )

    assert delta == Decimal("50.5")
    # baseline + 2 polls = 3 wallet calls total
    assert sum(1 for r in captured if r.url.path.endswith("wallet-balance")) == 3


@pytest.mark.asyncio
async def test_poll_deposit_credited_timeout(captured):
    """Balance never increases past baseline — raise TimeoutError."""
    async with _client(captured, lambda _r: _ok(_wallet_response("100.0"))) as c:
        with pytest.raises(asyncio.TimeoutError, match="not credited"):
            await c.poll_deposit_credited(
                coin="USDC",
                min_credit="10",
                timeout_seconds=0.05,
                interval_seconds=0.01,
            )


@pytest.mark.asyncio
async def test_poll_deposit_credited_sums_across_accounts(captured):
    """USDC sitting in both UNIFIED and FUND must be summed."""
    def payload(unified: str, fund: str) -> dict:
        return {
            "list": [
                {
                    "accountType": "UNIFIED",
                    "coin": [{"coin": "USDC", "walletBalance": unified}],
                },
                {
                    "accountType": "FUND",
                    "coin": [{"coin": "USDC", "walletBalance": fund}],
                },
            ]
        }

    balances = iter([
        payload("100.0", "0"),    # baseline 100
        payload("100.0", "50.5"), # credited landed in FUND → total 150.5
    ])
    async with _client(captured, lambda _r: _ok(next(balances))) as c:
        delta = await c.poll_deposit_credited(
            coin="USDC", min_credit="50", interval_seconds=0
        )
    assert delta == Decimal("50.5")


@pytest.mark.asyncio
async def test_poll_deposit_credited_zero_baseline_ok(captured):
    """First-ever deposit (baseline 0) credits the full amount."""
    balances = iter([
        _wallet_response("0"),
        _wallet_response("25"),
    ])
    async with _client(captured, lambda _r: _ok(next(balances))) as c:
        delta = await c.poll_deposit_credited(
            coin="USDC", min_credit="20", interval_seconds=0
        )
    assert delta == Decimal("25")


@pytest.mark.asyncio
async def test_poll_deposit_credited_immediate_credit(captured):
    """Credit visible on the very first poll (no waiting needed) — verifies
    we don't sleep an extra interval after success.
    """
    balances = iter([
        _wallet_response("100.0"),  # baseline
        _wallet_response("200.0"),  # already there on first poll
    ])
    async with _client(captured, lambda _r: _ok(next(balances))) as c:
        delta = await c.poll_deposit_credited(
            coin="USDC", min_credit="50", interval_seconds=0
        )
    assert delta == Decimal("100")


# --- .12e: spot order polling ----------------------------------------------


def _order_status(status: str, qty: str = "0", reject: str | None = None) -> dict:
    payload: dict = {
        "list": [
            {"orderId": "ord-1", "orderStatus": status, "cumExecQty": qty}
        ]
    }
    if reject is not None:
        payload["list"][0]["rejectReason"] = reject
    return payload


@pytest.mark.asyncio
async def test_get_spot_order_status_calls_realtime_with_category(captured):
    async with _client(captured, lambda _r: _ok(_order_status("Filled", "0.5"))) as c:
        status = await c.get_spot_order_status("ord-1")

    assert status.orderStatus == "Filled"
    assert status.cumExecQty == "0.5"
    req = captured[0]
    assert req.url.path == "/v5/order/realtime"
    assert dict(req.url.params) == {"category": "spot", "orderId": "ord-1"}


@pytest.mark.asyncio
async def test_get_spot_order_status_empty_list_raises(captured):
    """Bybit removes orders from realtime shortly after finalization.
    Absence is signal, not noise — caller must handle.
    """
    async with _client(captured, lambda _r: _ok({"list": []})) as c:
        with pytest.raises(BybitOrderError, match="not found in realtime"):
            await c.get_spot_order_status("ord-1")


@pytest.mark.asyncio
async def test_poll_spot_order_filled_returns_qty(captured):
    responses = iter([
        _order_status("New"),
        _order_status("PartiallyFilled", "0.3"),
        _order_status("Filled", "1.25"),
    ])
    async with _client(captured, lambda _r: _ok(next(responses))) as c:
        qty = await c.poll_spot_order_filled(
            order_id="ord-1", interval_seconds=0
        )
    assert qty == Decimal("1.25")


@pytest.mark.asyncio
async def test_poll_spot_order_filled_cancelled_raises(captured):
    """Cancelled is terminal — must NOT timeout-wait, must raise immediately."""
    async with _client(
        captured,
        lambda _r: _ok(_order_status("Cancelled", reject="insufficient balance")),
    ) as c:
        with pytest.raises(BybitOrderError, match="Cancelled"):
            await c.poll_spot_order_filled(order_id="ord-1", interval_seconds=0)


@pytest.mark.asyncio
async def test_poll_spot_order_filled_rejected_raises(captured):
    async with _client(captured, lambda _r: _ok(_order_status("Rejected"))) as c:
        with pytest.raises(BybitOrderError, match="Rejected"):
            await c.poll_spot_order_filled(order_id="ord-1", interval_seconds=0)


@pytest.mark.asyncio
async def test_poll_spot_order_filled_timeout(captured):
    """Stuck in `New` — TimeoutError after deadline."""
    async with _client(captured, lambda _r: _ok(_order_status("New"))) as c:
        with pytest.raises(TimeoutError, match="not filled"):
            await c.poll_spot_order_filled(
                order_id="ord-1", timeout_seconds=0.05, interval_seconds=0.01
            )


# --- .13b: Earn redeem helpers --------------------------------------------


@pytest.mark.asyncio
async def test_redeem_from_earn_sends_redeem_side(captured):
    async with _client(captured, lambda _r: _ok({"orderId": "redeem-1"})) as c:
        result = await c.redeem_from_earn(
            product_id="prod-1", amount="100", order_link_id="link-1"
        )

    assert result.orderId == "redeem-1"
    req = captured[0]
    assert req.url.path == "/v5/earn/place-order"
    body = json.loads(req.content.decode())
    assert body == {
        "productId": "prod-1",
        "amount": "100",
        "orderType": "Redeem",
        "orderLinkId": "link-1",
    }


@pytest.mark.asyncio
async def test_poll_redemption_credited_delegates_to_deposit_poller(captured):
    """Semantically identical to poll_deposit_credited — verify by checking
    that the same wallet-balance polling happens and the returned delta
    matches.
    """
    balances = iter([
        {"list": [{"accountType": "UNIFIED", "coin": [{"coin": "USDC", "walletBalance": "0"}]}]},
        {"list": [{"accountType": "UNIFIED", "coin": [{"coin": "USDC", "walletBalance": "50"}]}]},
    ])
    async with _client(captured, lambda _r: _ok(next(balances))) as c:
        delta = await c.poll_redemption_credited(
            coin="USDC", min_credit="50", interval_seconds=0
        )
    assert delta == Decimal("50")
    # 1 baseline + 1 poll
    assert sum(1 for r in captured if r.url.path.endswith("wallet-balance")) == 2
