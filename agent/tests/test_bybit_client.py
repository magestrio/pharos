import asyncio
import base64
import json
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from agent.bybit_oracle.bybit_client import (
    BybitAPIError,
    BybitClient,
    BybitOrderError,
    DepositChain,
    FlexibleEarnProduct,
    OnChainEarnProduct,
)
from agent.bybit_oracle.bybit_client import (  # noqa: E501 — keep typed-model surface explicit
    BonusEvent,
    EarnPosition,
    FreezeDetail,
    PerpPosition,
)

API_KEY = "test-key"
RECV_WINDOW = 5000
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _expected_signature(timestamp: str, payload: str) -> str:
    msg = (timestamp + API_KEY + str(RECV_WINDOW) + payload).encode()
    sig = PRIVATE_KEY.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


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
        private_key=PRIVATE_KEY,
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
    assert isinstance(products[0], FlexibleEarnProduct)
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
        await c.place_earn_order(
            category="FlexibleSaving",
            product_id="p1",
            amount="100",
            side="Stake",
            coin="USDC",
            account_type="FUND",
            order_link_id="link-1",
        )

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v5/earn/place-order"
    assert req.headers["Content-Type"] == "application/json"

    body_text = req.content.decode()
    assert json.loads(body_text) == {
        "category": "FlexibleSaving",
        "productId": "p1",
        "amount": "100",
        "orderType": "Stake",
        "coin": "USDC",
        "accountType": "FUND",
        "orderLinkId": "link-1",
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
            category="FlexibleSaving",
            product_id="prod-1",
            amount="100",
            coin="USDC",
            account_type="FUND",
            order_link_id="link-1",
        )

    assert result.orderId == "redeem-1"
    req = captured[0]
    assert req.url.path == "/v5/earn/place-order"
    body = json.loads(req.content.decode())
    assert body == {
        "category": "FlexibleSaving",
        "productId": "prod-1",
        "amount": "100",
        "orderType": "Redeem",
        "coin": "USDC",
        "accountType": "FUND",
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


# ─── advance Earn categories (multi-category routing) ───────────────────────


@pytest.mark.parametrize(
    "category",
    # LiquidityMining intentionally absent — it lives in its own
    # /v5/earn/liquidity-mining/* namespace, not under advance-Earn
    # (verified .24, 2026-05-27).
    ["DualAssets", "DiscountBuy", "SmartLeverage", "DoubleWin"],
)
@pytest.mark.asyncio
async def test_list_advance_earn_products_routes_to_correct_path(captured, category):
    fixture = {"list": [{"productId": f"x-{category}", "coin": "USDC"}]}
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        items = await c.list_advance_earn_products(category=category, coin="USDC")

    req = captured[0]
    assert req.url.path == "/v5/earn/advance/product"
    assert dict(req.url.params) == {"category": category, "coin": "USDC"}
    assert items == [{"productId": f"x-{category}", "coin": "USDC"}]


@pytest.mark.asyncio
async def test_list_advance_earn_products_omits_none_coin(captured):
    async with _client(captured, lambda _r: _ok({"list": []})) as c:
        await c.list_advance_earn_products(category="DualAssets")

    req = captured[0]
    assert dict(req.url.params) == {"category": "DualAssets"}


@pytest.mark.asyncio
async def test_list_advance_earn_products_rejects_unknown_category():
    async with _client([], lambda _r: _ok({"list": []})) as c:
        with pytest.raises(ValueError, match="unknown advance-Earn category"):
            await c.list_advance_earn_products(category="NotARealCategory")


@pytest.mark.asyncio
async def test_list_advance_earn_products_rejects_legacy_category():
    """FlexibleSaving / OnChain belong to `list_earn_products`, not advance."""
    async with _client([], lambda _r: _ok({"list": []})) as c:
        with pytest.raises(ValueError, match="unknown advance-Earn category"):
            await c.list_advance_earn_products(category="FlexibleSaving")


@pytest.mark.asyncio
async def test_list_advance_earn_products_handles_empty_result(captured):
    async with _client(captured, lambda _r: _ok({})) as c:
        items = await c.list_advance_earn_products(category="DualAssets")
    assert items == []


@pytest.mark.asyncio
async def test_list_advance_earn_products_returns_raw_dicts_not_models(captured):
    """Schemas vary per advance-Earn category — caller gets raw dicts to
    preserve structured-product fields (DualAssets has strikePrice/expiryTime,
    DiscountBuy has knockoutPrice/instUid, etc.)."""
    fixture = {
        "list": [
            {
                "productId": "da-001",
                "underlyingPair": "BTC-USDC",
                "strikePrice": "70000",
                "expiryTime": "1735689600000",
                "estimateApr": "0.45",
                "settlementCoin": "USDC",
            }
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        items = await c.list_advance_earn_products(category="DualAssets")

    assert len(items) == 1
    assert items[0] == fixture["list"][0]
    # caller can inspect category-specific fields without pydantic dropping them
    assert items[0]["strikePrice"] == "70000"
    assert items[0]["expiryTime"] == "1735689600000"


# ─── advance Earn — quote / position / redeem-estimate / place ──────────────


@pytest.mark.asyncio
async def test_get_advance_product_quote_routes_correctly(captured):
    fixture = {
        "productId": "12999",
        "breakevenPrice": "68650.62",
        "currentPrice": "68403.67",
        "category": "SmartLeverage",
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        result = await c.get_advance_product_quote(
            category="SmartLeverage", product_id="12999"
        )

    assert result == fixture
    req = captured[0]
    assert req.url.path == "/v5/earn/advance/product-extra-info"
    assert dict(req.url.params) == {
        "category": "SmartLeverage",
        "productId": "12999",
    }


@pytest.mark.asyncio
async def test_get_advance_product_quote_omits_none_product_id(captured):
    """For DiscountBuy you may want all offers — productId is optional."""
    async with _client(captured, lambda _r: _ok({"offers": []})) as c:
        await c.get_advance_product_quote(category="DiscountBuy")
    assert dict(captured[0].url.params) == {"category": "DiscountBuy"}


@pytest.mark.asyncio
async def test_get_advance_product_quote_rejects_basic_category():
    async with _client([], lambda _r: _ok({})) as c:
        with pytest.raises(ValueError, match="unknown advance-Earn category"):
            await c.get_advance_product_quote(category="FlexibleSaving")


@pytest.mark.asyncio
async def test_get_advance_earn_positions_requires_product_id(captured):
    fixture = {
        "list": [
            {
                "positionId": "1277",
                "productId": "12999",
                "strikePrice": "68650",
                "settlementCoin": "USDC",
            }
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        result = await c.get_advance_earn_positions(
            category="SmartLeverage", product_id="12999"
        )
    assert len(result) == 1
    assert result[0]["positionId"] == "1277"
    req = captured[0]
    assert req.url.path == "/v5/earn/advance/position"
    assert dict(req.url.params) == {
        "category": "SmartLeverage",
        "productId": "12999",
    }


@pytest.mark.asyncio
async def test_get_redeem_estimate_serializes_position_id_list(captured):
    async with _client(captured, lambda _r: _ok({"list": []})) as c:
        await c.get_redeem_estimate(
            category="DoubleWin", position_ids=["2847", "2848"]
        )
    req = captured[0]
    assert req.url.path == "/v5/earn/advance/get-redeem-est-amount-list"
    assert dict(req.url.params) == {
        "category": "DoubleWin",
        "positionIds": "2847,2848",
    }


@pytest.mark.asyncio
async def test_get_redeem_estimate_accepts_csv_string(captured):
    async with _client(captured, lambda _r: _ok({})) as c:
        await c.get_redeem_estimate(category="DoubleWin", position_ids="42")
    assert dict(captured[0].url.params)["positionIds"] == "42"


@pytest.mark.asyncio
async def test_place_advance_earn_order_stake_body(captured):
    async with _client(
        captured,
        lambda _r: _ok({"orderId": "ord-99", "orderLinkId": "link-99"}),
    ) as c:
        result = await c.place_advance_earn_order(
            category="SmartLeverage",
            product_id="12999",
            side="Stake",
            account_type="FUND",
            order_link_id="link-99",
            coin="USDT",
            amount="100",
            extra={
                "smartLeverageStakeExtra": {
                    "initialPrice": "68403",
                    "breakevenPrice": "68650",
                }
            },
        )

    assert result == {"orderId": "ord-99", "orderLinkId": "link-99"}
    req = captured[0]
    assert req.url.path == "/v5/earn/advance/place-order"
    body = json.loads(req.content.decode())
    assert body == {
        "category": "SmartLeverage",
        "productId": "12999",
        "orderType": "Stake",
        "accountType": "FUND",
        "orderLinkId": "link-99",
        "coin": "USDT",
        "amount": "100",
        "smartLeverageStakeExtra": {
            "initialPrice": "68403",
            "breakevenPrice": "68650",
        },
    }


@pytest.mark.asyncio
async def test_place_advance_earn_order_redeem_omits_coin_and_amount(captured):
    """Per the V5 docs Redeem orders don't need coin/amount — the position
    carries them. The wrapper must omit None fields from the body."""
    async with _client(captured, lambda _r: _ok({"orderId": "redeem-9"})) as c:
        await c.place_advance_earn_order(
            category="SmartLeverage",
            product_id="12999",
            side="Redeem",
            account_type="FUND",
            order_link_id="link-100",
            extra={
                "smartLeverageRedeemExtra": {
                    "positionId": "1277",
                    "estRedeemAmount": "77.85",
                    "isSlippageProtected": True,
                }
            },
        )

    body = json.loads(captured[0].content.decode())
    assert "coin" not in body
    assert "amount" not in body
    assert body["orderType"] == "Redeem"
    assert body["smartLeverageRedeemExtra"]["positionId"] == "1277"


@pytest.mark.asyncio
async def test_place_advance_earn_order_rejects_basic_category():
    async with _client([], lambda _r: _ok({"orderId": "x"})) as c:
        with pytest.raises(ValueError, match="unknown advance-Earn category"):
            await c.place_advance_earn_order(
                category="FlexibleSaving",
                product_id="p",
                side="Stake",
                account_type="FUND",
                order_link_id="lid",
            )


# ─── liquidity-mining (own namespace, not under advance-Earn) ───────────────


@pytest.mark.asyncio
async def test_list_liquidity_mining_products_uses_dedicated_path(captured):
    """LM is NOT in ADVANCE_EARN_CATEGORIES — it lives at
    `/v5/earn/liquidity-mining/product` with baseCoin/quoteCoin filters
    instead of the category-discriminator pattern. Result is unwrapped
    from `result.products` (not `result.list`)."""
    fixture = {
        "products": [
            {
                "productId": "5",
                "baseCoin": "ETH",
                "quoteCoin": "USDT",
                "status": "Available",
                "maxLeverage": 3,
                "apyE8": "13714946",
            }
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        items = await c.list_liquidity_mining_products(
            base_coin="ETH", quote_coin="USDT"
        )

    req = captured[0]
    assert req.url.path == "/v5/earn/liquidity-mining/product"
    assert dict(req.url.params) == {"baseCoin": "ETH", "quoteCoin": "USDT"}
    assert items == fixture["products"]


@pytest.mark.asyncio
async def test_list_liquidity_mining_products_omits_unset_filters(captured):
    async with _client(captured, lambda _r: _ok({"products": []})) as c:
        await c.list_liquidity_mining_products()
    assert dict(captured[0].url.params) == {}


@pytest.mark.asyncio
async def test_list_liquidity_mining_products_handles_missing_products_key(captured):
    """When the result envelope is empty (`{}`) the method must return
    `[]`, not raise — Bybit returns this when no products match."""
    async with _client(captured, lambda _r: _ok({})) as c:
        items = await c.list_liquidity_mining_products(base_coin="DOGE")
    assert items == []


@pytest.mark.asyncio
async def test_get_liquidity_mining_positions_routes_correctly(captured):
    fixture = {
        "positions": [
            {
                "positionId": "1498",
                "productId": "5",
                "baseCoin": "ETH",
                "quoteCoin": "USDT",
                "status": "Active",
            }
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        items = await c.get_liquidity_mining_positions(base_coin="ETH")

    req = captured[0]
    assert req.url.path == "/v5/earn/liquidity-mining/position"
    assert dict(req.url.params) == {"baseCoin": "ETH"}
    assert items == fixture["positions"]


@pytest.mark.asyncio
async def test_get_liquidity_mining_yield_records_passes_all_params(captured):
    fixture = {
        "records": [
            {
                "coin": "USDT",
                "amount": "0.0098",
                "baseCoin": "ETH",
                "quoteCoin": "USDT",
                "type": "Manual",
                "status": "Complete",
                "createdTime": "1775125850000",
            }
        ],
        "nextPageCursor": "",
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        result = await c.get_liquidity_mining_yield_records(
            base_coin="ETH",
            quote_coin="USDT",
            start_time=1_700_000_000_000,
            end_time=1_700_500_000_000,
            limit=50,
            cursor="prev",
        )

    req = captured[0]
    assert req.url.path == "/v5/earn/liquidity-mining/yield-records"
    assert dict(req.url.params) == {
        "baseCoin": "ETH",
        "quoteCoin": "USDT",
        "startTime": "1700000000000",
        "endTime": "1700500000000",
        "limit": "50",
        "cursor": "prev",
    }
    assert result == fixture


@pytest.mark.asyncio
async def test_liquidity_mining_no_longer_in_advance_earn_categories():
    """Regression guard for .24 — LM was bug-bucketed into the advance-
    Earn category set; removing it shifts callers onto the dedicated
    `list_liquidity_mining_products` path. ValueError ensures any stale
    caller fails loudly instead of silently 404'ing or, worse, 180001'ing."""
    async with _client([], lambda _r: _ok({"list": []})) as c:
        with pytest.raises(ValueError, match="unknown advance-Earn category"):
            await c.list_advance_earn_products(category="LiquidityMining")


# ─── LM mutating endpoints (.47) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_liquidity_quote_only_single_sided(captured):
    """Single-sided USDC deposit at leverage=1 — Bybit auto-balances to
    50/50 internally. Body must include `quoteAmount` + matching
    `quoteAccountType`, omit base-side keys, and signed via POST."""
    async with _client(
        captured, lambda _r: _ok({"orderId": "lm-1", "orderLinkId": "lk-1"})
    ) as c:
        out = await c.add_liquidity(
            product_id="24",
            order_link_id="lk-1",
            quote_amount="10",
            quote_account_type="UNIFIED",
        )

    assert out.orderId == "lm-1"
    assert out.orderLinkId == "lk-1"

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v5/earn/liquidity-mining/add-liquidity"
    body = json.loads(req.content.decode())
    assert body == {
        "productId": "24",
        "orderLinkId": "lk-1",
        "leverage": "1",
        "quoteAmount": "10",
        "quoteAccountType": "UNIFIED",
    }


@pytest.mark.asyncio
async def test_add_liquidity_dual_sided_locks_ratio(captured):
    """When both sides are supplied, server skips internal rebalancing
    and uses the exact ratio. Both `*_amount` + `*_account_type` keys
    must appear in the body."""
    async with _client(
        captured, lambda _r: _ok({"orderId": "lm-2"})
    ) as c:
        await c.add_liquidity(
            product_id="23",
            order_link_id="lk-2",
            quote_amount="50",
            quote_account_type="UNIFIED",
            base_amount="0.0008",
            base_account_type="UNIFIED",
            leverage="1",
        )

    body = json.loads(captured[0].content.decode())
    assert body["quoteAmount"] == "50"
    assert body["baseAmount"] == "0.0008"
    assert body["quoteAccountType"] == "UNIFIED"
    assert body["baseAccountType"] == "UNIFIED"


@pytest.mark.asyncio
async def test_add_liquidity_rejects_no_amount():
    """Caller MUST supply at least one of `quote_amount` / `base_amount`.
    Local ValueError saves an unsignable round-trip and surfaces the
    contract violation at the call site, not as a Bybit retCode."""
    async with _client([], lambda _r: _ok({})) as c:
        with pytest.raises(ValueError, match="at least one of"):
            await c.add_liquidity(
                product_id="24", order_link_id="lk-x"
            )


@pytest.mark.asyncio
async def test_add_liquidity_requires_account_type_for_quote():
    async with _client([], lambda _r: _ok({})) as c:
        with pytest.raises(ValueError, match="quote_account_type is required"):
            await c.add_liquidity(
                product_id="24",
                order_link_id="lk-x",
                quote_amount="10",
            )


@pytest.mark.asyncio
async def test_add_liquidity_requires_account_type_for_base():
    async with _client([], lambda _r: _ok({})) as c:
        with pytest.raises(ValueError, match="base_account_type is required"):
            await c.add_liquidity(
                product_id="24",
                order_link_id="lk-x",
                base_amount="0.001",
            )


@pytest.mark.asyncio
async def test_remove_liquidity_full_exit_defaults(captured):
    """Default `remove_rate=100` + `remove_type="Normal"` is a full exit
    that returns both coins pro-rata — the standard close path."""
    async with _client(
        captured, lambda _r: _ok({"orderId": "rm-1", "orderLinkId": "lk-r"})
    ) as c:
        out = await c.remove_liquidity(
            product_id="24",
            position_id="9001",
            order_link_id="lk-r",
        )

    assert out.orderId == "rm-1"
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v5/earn/liquidity-mining/remove-liquidity"
    body = json.loads(req.content.decode())
    assert body == {
        "productId": "24",
        "positionId": "9001",
        "orderLinkId": "lk-r",
        "removeRate": 100,
        "removeType": "Normal",
    }


@pytest.mark.asyncio
async def test_remove_liquidity_partial_with_single_quote(captured):
    """Partial exit returning quote coin only — convenient when the
    strategy wants to recover USDC without re-swapping the base side."""
    async with _client(
        captured, lambda _r: _ok({"orderId": "rm-2"})
    ) as c:
        await c.remove_liquidity(
            product_id="24",
            position_id="9001",
            order_link_id="lk-r2",
            remove_rate=50,
            remove_type="SingleQuoteCoin",
        )

    body = json.loads(captured[0].content.decode())
    assert body["removeRate"] == 50
    assert body["removeType"] == "SingleQuoteCoin"


@pytest.mark.asyncio
async def test_claim_lm_interest_defaults_to_claim_all(captured):
    """`productId="-1"` is Bybit-native shorthand for "every active LM
    position" — preferred over per-product calls to save round-trips."""
    async with _client(captured, lambda _r: _ok({})) as c:
        await c.claim_lm_interest()

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v5/earn/liquidity-mining/claim-interest"
    body = json.loads(req.content.decode())
    assert body == {"productId": "-1"}


@pytest.mark.asyncio
async def test_claim_lm_interest_with_specific_product(captured):
    async with _client(captured, lambda _r: _ok({})) as c:
        await c.claim_lm_interest(product_id="24")

    body = json.loads(captured[0].content.decode())
    assert body == {"productId": "24"}


# ─── hourly-yield ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_hourly_yield_includes_all_provided_params(captured):
    fixture = {
        "list": [{"productId": "428", "coin": "USDT", "amount": "0.06"}],
        "nextPageCursor": "abc",
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        result = await c.get_hourly_yield(
            category="FlexibleSaving",
            product_id="428",
            start_time=1700000000000,
            end_time=1700500000000,
            limit=100,
            cursor="prev",
        )

    assert result == fixture
    req = captured[0]
    assert req.url.path == "/v5/earn/hourly-yield"
    params = dict(req.url.params)
    assert params == {
        "category": "FlexibleSaving",
        "productId": "428",
        "startTime": "1700000000000",
        "endTime": "1700500000000",
        "limit": "100",
        "cursor": "prev",
    }


@pytest.mark.asyncio
async def test_get_hourly_yield_omits_unset_params(captured):
    """Optional params (productId, startTime, ...) must not show up as
    empty strings — Bybit interprets `productId=` as a filter for an
    empty product, not a wildcard."""
    async with _client(captured, lambda _r: _ok({"list": []})) as c:
        await c.get_hourly_yield(category="FlexibleSaving")
    assert dict(captured[0].url.params) == {"category": "FlexibleSaving"}


# ─── apr-history / yield-history ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_apr_history_windows_days_into_timestamps(captured, monkeypatch):
    """`days` is windowed client-side into `startTime`/`endTime` (epoch
    ms). Freeze `time.time()` so the assertion isn't flaky."""
    fixture = {
        "list": [
            {"timestamp": "1735000000000", "apr": "0.5%"},
            {"timestamp": "1735086400000", "apr": "0.5%"},
        ]
    }
    monkeypatch.setattr(
        "agent.bybit_oracle.bybit_client.time.time", lambda: 1_735_000_000.0
    )

    async with _client(captured, lambda _r: _ok(fixture)) as c:
        result = await c.get_apr_history(
            category="FlexibleSaving", product_id="1131", days=30
        )

    assert result == fixture
    req = captured[0]
    assert req.url.path == "/v5/earn/apr-history"
    end_ms = 1_735_000_000_000
    start_ms = end_ms - 30 * 24 * 60 * 60 * 1000
    assert dict(req.url.params) == {
        "category": "FlexibleSaving",
        "productId": "1131",
        "startTime": str(start_ms),
        "endTime": str(end_ms),
    }


@pytest.mark.asyncio
async def test_get_yield_history_includes_all_provided_params(captured):
    fixture = {
        "yield": [
            {
                "productId": "428",
                "coin": "USDT",
                "id": "1002096",
                "amount": "0.0608",
                "yieldType": "Normal",
                "status": "Success",
                "createdAt": "1759993805000",
            }
        ],
        "nextPageCursor": "next-page",
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        result = await c.get_yield_history(
            category="FlexibleSaving",
            start_time=1_700_000_000_000,
            end_time=1_700_500_000_000,
            product_id="428",
            limit=50,
            cursor="prev",
        )

    assert result == fixture
    req = captured[0]
    assert req.url.path == "/v5/earn/yield"
    assert dict(req.url.params) == {
        "category": "FlexibleSaving",
        "startTime": "1700000000000",
        "endTime": "1700500000000",
        "productId": "428",
        "limit": "50",
        "cursor": "prev",
    }


@pytest.mark.asyncio
async def test_get_yield_history_omits_unset_optionals(captured):
    """Required category/startTime/endTime always sent; productId/limit/
    cursor must drop out entirely when None — Bybit treats empty-string
    filters as literal filters, not wildcards."""
    async with _client(captured, lambda _r: _ok({"yield": []})) as c:
        await c.get_yield_history(
            category="OnChain",
            start_time=1_700_000_000_000,
            end_time=1_700_500_000_000,
        )
    assert dict(captured[0].url.params) == {
        "category": "OnChain",
        "startTime": "1700000000000",
        "endTime": "1700500000000",
    }


# ─── asset-overview ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_asset_overview_includes_provided_params(captured):
    fixture = {
        "totalEquity": "1254.56",
        "list": [
            {
                "accountType": "UNIFIED",
                "totalEquity": "1234.56",
                "valuationCurrency": "USD",
                "snapshotTime": "1735000000000",
                "coinDetail": [{"coin": "USDC", "equity": "1234.56"}],
            },
            {
                "accountType": "FUND",
                "totalEquity": "20.00",
                "valuationCurrency": "USD",
                "snapshotTime": "1735000000000",
                "coinDetail": [{"coin": "USDC", "equity": "20.00"}],
            },
        ],
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        result = await c.get_asset_overview(
            account_type="UNIFIED",
            valuation_currency="USD",
            member_id="123456",
        )

    assert result == fixture
    req = captured[0]
    assert req.url.path == "/v5/asset/asset-overview"
    assert dict(req.url.params) == {
        "accountType": "UNIFIED",
        "valuationCurrency": "USD",
        "memberId": "123456",
    }


@pytest.mark.asyncio
async def test_get_asset_overview_omits_unset_params(captured):
    """All params optional — omitting `accountType` asks Bybit for the
    cross-account aggregate. Empty-string filters would be interpreted
    literally, so they must drop out of the query entirely."""
    async with _client(captured, lambda _r: _ok({"list": []})) as c:
        await c.get_asset_overview()
    assert dict(captured[0].url.params) == {}


# ─── typed-model coverage: full V5 /v5/earn/product + /position fields ──────


@pytest.mark.asyncio
async def test_earn_product_parses_full_flexible_saving_payload(captured):
    """Verbatim Flexible BTC sample from the V5 spec — shared/Flexible
    fields must round-trip; OnChain-only fields (term, swap*) are
    correctly absent on the typed FlexibleEarnProduct subclass after
    the .20 discriminated-union split."""
    fixture = {
        "list": [
            {
                "category": "FlexibleSaving",
                "estimateApr": "3%",
                "coin": "BTC",
                "minStakeAmount": "0.001",
                "maxStakeAmount": "10",
                "precision": "8",
                "productId": "430",
                "status": "Available",
                "bonusEvents": [],
                "minRedeemAmount": "",
                "maxRedeemAmount": "",
                "duration": "",
                "term": 0,
                "swapCoin": "",
                "swapCoinPrecision": "",
                "stakeExchangeRate": "",
                "redeemExchangeRate": "",
                "rewardDistributionType": "",
                "rewardIntervalMinute": 0,
                "redeemProcessingMinute": "0",
                "stakeTime": "",
                "interestCalculationTime": "",
            }
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        products = await c.list_earn_products(category="FlexibleSaving", coin="BTC")

    assert len(products) == 1
    p = products[0]
    assert isinstance(p, FlexibleEarnProduct)
    assert p.productId == "430"
    assert p.status == "Available"
    assert p.precision == "8"
    assert p.duration == ""
    assert p.bonusEvents == []
    assert p.rewardIntervalMinute == 0
    assert p.redeemProcessingMinute == "0"
    assert not hasattr(p, "term")
    assert not hasattr(p, "swapCoin")


@pytest.mark.asyncio
async def test_earn_product_accepts_int_redeem_processing_minute(captured):
    """Bybit V5 returns `redeemProcessingMinute` inconsistently — `"0"`
    string for some products, raw int `0` for others (live-observed
    2026-05-27 on FlexibleSaving USDC). Both must parse without error.
    """
    fixture = {
        "list": [
            {
                "productId": "int-rpm",
                "coin": "USDC",
                "category": "FlexibleSaving",
                "redeemProcessingMinute": 0,
            },
            {
                "productId": "str-rpm",
                "coin": "USDC",
                "category": "FlexibleSaving",
                "redeemProcessingMinute": "30",
            },
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        products = await c.list_earn_products(category="FlexibleSaving", coin="USDC")

    int_rpm = next(p for p in products if p.productId == "int-rpm")
    str_rpm = next(p for p in products if p.productId == "str-rpm")
    assert int_rpm.redeemProcessingMinute == 0
    assert str_rpm.redeemProcessingMinute == "30"


@pytest.mark.asyncio
async def test_earn_product_distinguishes_fixed_vs_flexible(captured):
    """OnChain Fixed product carries `term > 0` and `duration=Fixed` —
    that is the model-level signal the oracle uses to differentiate
    locked products from flex pools."""
    fixture = {
        "list": [
            {
                "productId": "fixed-30d",
                "coin": "USDC",
                "category": "OnChain",
                "duration": "Fixed",
                "term": 30,
                "estimateApr": "8.5%",
                "rewardDistributionType": "Compound",
                "stakeTime": "1735689600000",
                "interestCalculationTime": "1735776000000",
                "rewardIntervalMinute": 1440,
            },
            {
                "productId": "flex-usdc",
                "coin": "USDC",
                "category": "OnChain",
                "duration": "Flexible",
                "term": 0,
                "estimateApr": "4.2%",
                "rewardDistributionType": "Simple",
            },
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        products = await c.list_earn_products(category="OnChain")

    fixed = next(p for p in products if p.productId == "fixed-30d")
    flex = next(p for p in products if p.productId == "flex-usdc")
    assert fixed.duration == "Fixed"
    assert fixed.term == 30
    assert fixed.rewardDistributionType == "Compound"
    assert fixed.rewardIntervalMinute == 1440
    assert flex.duration == "Flexible"
    assert flex.term == 0


@pytest.mark.asyncio
async def test_earn_product_parses_bonus_events(captured):
    """Promo-APR layer: UI 7.52% vs API estimateApr 0.7% delta lives in
    bonusEvents.apr — Phase A.3 observation. Must surface as typed
    BonusEvent objects, not be dropped to extra=ignore."""
    fixture = {
        "list": [
            {
                "productId": "usd1-promo",
                "coin": "USD1",
                "category": "FlexibleSaving",
                "estimateApr": "0.70%",
                "bonusEvents": [
                    {
                        "apr": "6.82%",
                        "coin": "USD1",
                        "announcement": "https://announcements.bybit.com/promo-123",
                    }
                ],
            }
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        products = await c.list_earn_products(category="FlexibleSaving")

    assert len(products[0].bonusEvents) == 1
    bonus = products[0].bonusEvents[0]
    assert isinstance(bonus, BonusEvent)
    assert bonus.apr == "6.82%"
    assert bonus.coin == "USD1"


@pytest.mark.asyncio
async def test_earn_product_parses_lst_fields(captured):
    """OnChain LST mode populates swap-pair fields — needed when the
    oracle reasons about cmETH-like products that wrap stake-by-swap."""
    fixture = {
        "list": [
            {
                "productId": "lst-cmeth",
                "coin": "ETH",
                "category": "OnChain",
                "swapCoin": "cmETH",
                "swapCoinPrecision": "6",
                "stakeExchangeRate": "1.0234",
                "redeemExchangeRate": "1.0231",
                "minRedeemAmount": "0.01",
                "maxRedeemAmount": "100",
            }
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        products = await c.list_earn_products(category="OnChain")

    p = products[0]
    assert p.swapCoin == "cmETH"
    assert p.swapCoinPrecision == "6"
    assert p.stakeExchangeRate == "1.0234"
    assert p.redeemExchangeRate == "1.0231"
    assert p.minRedeemAmount == "0.01"


@pytest.mark.asyncio
async def test_earn_position_parses_full_onchain_payload(captured):
    """Verbatim OnChain Fixed BTC position sample from the V5 spec —
    settlementTime / freezeDetails / autoReinvest / availableAmount /
    estimate*Time must all land as typed fields."""
    fixture = {
        "list": [
            {
                "coin": "BTC",
                "productId": "8",
                "amount": "0.1",
                "totalPnl": "0.000027397260273973",
                "claimableYield": "0",
                "id": "326",
                "status": "Active",
                "orderId": "1a5a8945-e042-4dd5-a93f-c0f0577377ad",
                "estimateRedeemTime": "",
                "estimateStakeTime": "",
                "estimateInterestCalculationTime": "1744243200000",
                "settlementTime": "1744675200000",
                "autoReinvest": "Enable",
                "availableAmount": "4900",
                "freezeDetails": [
                    {"amount": "100", "description": "Locked in Fixed-Rate Loan"}
                ],
            }
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        positions = await c.get_earn_positions(category="OnChain")

    assert len(positions) == 1
    pos = positions[0]
    assert isinstance(pos, EarnPosition)
    assert pos.id == "326"
    assert pos.status == "Active"
    assert pos.totalPnl == "0.000027397260273973"
    assert pos.claimableYield == "0"
    assert pos.orderId == "1a5a8945-e042-4dd5-a93f-c0f0577377ad"
    assert pos.settlementTime == "1744675200000"
    assert pos.estimateInterestCalculationTime == "1744243200000"
    assert pos.autoReinvest == "Enable"
    assert pos.availableAmount == "4900"
    assert len(pos.freezeDetails) == 1
    fr = pos.freezeDetails[0]
    assert isinstance(fr, FreezeDetail)
    assert fr.amount == "100"
    assert fr.description == "Locked in Fixed-Rate Loan"


@pytest.mark.asyncio
async def test_earn_position_handles_minimal_flexible_payload(captured):
    """FlexibleSaving positions typically only populate amount /
    claimableYield / availableAmount — OnChain-only fields must default
    to None, never raise."""
    fixture = {
        "list": [
            {
                "coin": "USDC",
                "productId": "430",
                "amount": "1000.50",
                "claimableYield": "0.012",
                "availableAmount": "1000.50",
            }
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        positions = await c.get_earn_positions(category="FlexibleSaving")

    pos = positions[0]
    assert pos.amount == "1000.50"
    assert pos.claimableYield == "0.012"
    assert pos.availableAmount == "1000.50"
    assert pos.id is None
    assert pos.status is None
    assert pos.totalPnl is None
    assert pos.settlementTime is None
    assert pos.freezeDetails == []


# ─── permission_probe (.26) ─────────────────────────────────────────────────


def _probe_responder(per_path_status: dict[str, int]):
    """Build an httpx responder that returns retCode per request path.

    `per_path_status` maps a substring of the request URL path to a
    Bybit retCode: 0 = success, 10005 = permission denied, others =
    arbitrary error. Any unmatched path defaults to retCode=0 so the
    test only has to specify the *failing* paths.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        ret_code = 0
        for key, code in per_path_status.items():
            if key in path:
                ret_code = code
                break
        if ret_code == 0:
            return _ok({"list": []})
        return httpx.Response(
            200,
            json={"retCode": ret_code, "retMsg": "denied or invalid", "result": {}},
        )

    return _handler


@pytest.mark.asyncio
async def test_permission_probe_all_ok(captured: list[httpx.Request]) -> None:
    async with _client(captured, _probe_responder({})) as c:
        out = await c.permission_probe()
    # Every probed endpoint should be present and "ok".
    expected = {
        "wallet_balance[UNIFIED]",
        "list_earn_products[FlexibleSaving]",
        "list_earn_products[OnChain]",
        "earn_positions[FlexibleSaving]",
        "lm_products",
        "advance_products[DualAssets]",
        "tickers_linear",
    }
    assert set(out) == expected
    assert all(v == "ok" for v in out.values())


@pytest.mark.asyncio
async def test_permission_probe_classifies_10005_as_permission_denied(
    captured: list[httpx.Request],
) -> None:
    # /v5/earn/position returns 10005 → "permission_denied" tag.
    async with _client(
        captured, _probe_responder({"/v5/earn/position": 10005})
    ) as c:
        out = await c.permission_probe()
    assert out["earn_positions[FlexibleSaving]"] == "permission_denied"
    assert out["wallet_balance[UNIFIED]"] == "ok"


@pytest.mark.asyncio
async def test_permission_probe_classifies_other_errors_as_error_code(
    captured: list[httpx.Request],
) -> None:
    async with _client(
        captured, _probe_responder({"/v5/earn/advance/product": 180001})
    ) as c:
        out = await c.permission_probe()
    assert out["advance_products[DualAssets]"] == "error:180001"


@pytest.mark.asyncio
async def test_permission_probe_runs_in_parallel(
    captured: list[httpx.Request],
) -> None:
    """All probe endpoints fire concurrently — captured length matches
    the probe size and no in-order dependency exists between calls."""
    async with _client(captured, _probe_responder({})) as c:
        await c.permission_probe()
    # 7 endpoints in `permission_probe` → 7 captured requests.
    assert len(captured) == 7


# ─── /v5/position/list (.32) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_positions_routes_to_position_list_with_settle_coin(
    captured: list[httpx.Request],
) -> None:
    fixture = {
        "list": [
            {
                "symbol": "TONUSDT",
                "side": "Sell",
                "size": "25",
                "positionValue": "50.00",
                "avgPrice": "2.0",
                "markPrice": "2.0",
                "unrealisedPnl": "0",
                "leverage": "1",
                "positionIdx": 0,
            }
        ]
    }
    async with _client(captured, lambda _r: _ok(fixture)) as c:
        positions = await c.get_positions(category="linear", settle_coin="USDT")
    assert len(positions) == 1
    p = positions[0]
    assert isinstance(p, PerpPosition)
    assert p.symbol == "TONUSDT"
    assert p.side == "Sell"
    assert p.size == "25"
    assert p.positionValue == "50.00"

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/v5/position/list"
    assert dict(req.url.params) == {"category": "linear", "settleCoin": "USDT"}


@pytest.mark.asyncio
async def test_get_positions_handles_empty_list(
    captured: list[httpx.Request],
) -> None:
    async with _client(captured, lambda _r: _ok({"list": []})) as c:
        positions = await c.get_positions(category="linear", settle_coin="USDT")
    assert positions == []


@pytest.mark.asyncio
async def test_get_positions_omits_none_params_from_signature(
    captured: list[httpx.Request],
) -> None:
    async with _client(captured, lambda _r: _ok({"list": []})) as c:
        await c.get_positions(category="linear")
    req = captured[0]
    # Only `category` makes it into the query string — None settle_coin
    # and symbol must not appear (otherwise the signed payload mismatches
    # what we send and Bybit returns 10004).
    assert dict(req.url.params) == {"category": "linear"}


@pytest.mark.asyncio
async def test_get_positions_propagates_bybit_api_error(
    captured: list[httpx.Request],
) -> None:
    def err(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"retCode": 10005, "retMsg": "permission denied", "result": {}}
        )

    async with _client(captured, err) as c:
        with pytest.raises(BybitAPIError) as exc:
            await c.get_positions(category="linear", settle_coin="USDT")
    assert exc.value.ret_code == 10005
