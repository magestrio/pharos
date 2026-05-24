"""Bybit V5 REST client.

Direct httpx wrapper instead of the official `pybit` SDK — we already depend
on httpx, and the V5 protocol is just REST + HMAC-SHA256, so a thin client
keeps deps frozen and avoids surprises when SDK lags behind new endpoints.

Signing scheme (V5 RECV_WINDOW header style):
    sign_string = timestamp + api_key + recv_window + payload
    signature   = hex(hmac_sha256(api_secret, sign_string))
where `payload` is the URL-encoded query string for GET/DELETE and the raw
JSON body for POST/PUT.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import urllib.parse
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .config import OracleSettings, settings
from .structured_log import get_logger

log = get_logger(__name__)


T = TypeVar("T")


class BybitAPIError(RuntimeError):
    """Raised when Bybit responds with retCode != 0."""

    def __init__(self, ret_code: int, ret_msg: str, path: str) -> None:
        super().__init__(f"bybit {path} failed: retCode={ret_code} retMsg={ret_msg}")
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.path = path


class BybitResponse(BaseModel, Generic[T]):
    """Standard Bybit V5 envelope."""

    model_config = ConfigDict(extra="ignore")

    retCode: int
    retMsg: str
    result: T | None = None


class EarnProduct(BaseModel):
    model_config = ConfigDict(extra="ignore")

    productId: str
    coin: str
    category: str
    status: str | None = None
    estimateApr: str | None = None
    minStakeAmount: str | None = None
    maxStakeAmount: str | None = None


class EarnProductList(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    items: list[EarnProduct] = Field(default_factory=list, alias="list")


class EarnPosition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    productId: str
    coin: str
    amount: str
    category: str | None = None
    status: str | None = None


class EarnPositionList(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    items: list[EarnPosition] = Field(default_factory=list, alias="list")


class EarnOrderResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    orderId: str


class WalletCoin(BaseModel):
    model_config = ConfigDict(extra="ignore")

    coin: str
    walletBalance: str
    availableToWithdraw: str | None = None
    usdValue: str | None = None


class WalletAccount(BaseModel):
    model_config = ConfigDict(extra="ignore")

    accountType: str
    totalEquity: str | None = None
    coin: list[WalletCoin] = Field(default_factory=list)


class WalletBalanceResult(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    items: list[WalletAccount] = Field(default_factory=list, alias="list")


class WithdrawResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str


class SpotOrderResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    orderId: str
    orderLinkId: str | None = None


class SpotOrderStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    orderId: str
    orderStatus: str
    cumExecQty: str = "0"
    cumExecValue: str = "0"
    avgPrice: str | None = None
    rejectReason: str | None = None


class SpotOrderStatusList(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    items: list[SpotOrderStatus] = Field(default_factory=list, alias="list")


class BybitOrderError(RuntimeError):
    """A spot order reached a terminal non-Filled state (Cancelled, Rejected,
    PartiallyFilledCancelled, Deactivated). Caller should advance FSM to
    failed and surface — these are not transient, retry won't help.
    """


class DepositChain(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chain: str
    chainType: str | None = None
    addressDeposit: str
    tagDeposit: str | None = None
    addressType: str | None = None


class DepositAddressResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    coin: str
    chains: list[DepositChain] = Field(default_factory=list)


Side = Literal["Buy", "Sell"]
EarnSide = Literal["Stake", "Redeem"]


class BybitClient:
    """Async Bybit V5 REST wrapper.

    Lifecycle:
        async with BybitClient.from_settings() as client:
            await client.get_wallet_balance()

    The underlying httpx.AsyncClient is opened on context entry; passing a
    pre-built `transport` is the test seam — production callers leave it None.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://api.bybit.com",
        recv_window: int = 5000,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret.encode()
        self._base_url = base_url.rstrip("/")
        self._recv_window = str(recv_window)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            transport=transport,
            timeout=timeout,
        )

    @classmethod
    def from_settings(
        cls,
        cfg: OracleSettings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> BybitClient:
        cfg = cfg or settings
        key = cfg.BYBIT_API_KEY.get_secret_value()
        secret = cfg.BYBIT_API_SECRET.get_secret_value()
        if not key or not secret:
            raise RuntimeError(
                "BYBIT_API_KEY / BYBIT_API_SECRET are required to call private endpoints"
            )
        return cls(
            api_key=key,
            api_secret=secret,
            base_url=cfg.BYBIT_BASE_URL,
            recv_window=cfg.BYBIT_RECV_WINDOW,
            transport=transport,
        )

    async def __aenter__(self) -> BybitClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _now_ms(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, timestamp: str, payload: str) -> str:
        message = (timestamp + self._api_key + self._recv_window + payload).encode()
        return hmac.new(self._api_secret, message, hashlib.sha256).hexdigest()

    def _auth_headers(self, timestamp: str, signature: str) -> dict[str, str]:
        return {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self._recv_window,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
        }

    async def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ts = self._now_ms()
        headers: dict[str, str] = {}
        request_kwargs: dict[str, Any] = {}

        if method == "GET":
            clean = {k: v for k, v in (params or {}).items() if v is not None}
            qs = urllib.parse.urlencode(clean, doseq=True)
            sig = self._sign(ts, qs)
            headers.update(self._auth_headers(ts, sig))
            if qs:
                request_kwargs["params"] = clean
        else:
            raw = json.dumps(body or {}, separators=(",", ":"))
            sig = self._sign(ts, raw)
            headers.update(self._auth_headers(ts, sig))
            headers["Content-Type"] = "application/json"
            request_kwargs["content"] = raw

        resp = await self._client.request(method, path, headers=headers, **request_kwargs)
        resp.raise_for_status()
        data = resp.json()

        ret_code = data.get("retCode")
        if ret_code != 0:
            log.warning(
                "bybit_api_error",
                extra={"path": path, "ret_code": ret_code, "ret_msg": data.get("retMsg")},
            )
            raise BybitAPIError(int(ret_code or -1), str(data.get("retMsg", "")), path)
        return data

    async def list_earn_products(
        self, category: str | None = None, coin: str | None = None
    ) -> list[EarnProduct]:
        """List Earn products. Covers all categories: FlexibleSaving, OnChain,
        FixedSaving, LiquidityMining, DualAsset, DiscountBuy.
        """
        data = await self._request(
            "GET", "/v5/earn/product", params={"category": category, "coin": coin}
        )
        parsed = BybitResponse[EarnProductList].model_validate(data)
        return parsed.result.items if parsed.result else []

    async def place_earn_order(
        self,
        product_id: str,
        amount: str,
        side: EarnSide,
        order_link_id: str | None = None,
    ) -> EarnOrderResult:
        """Stake or Redeem an Earn product. `amount` is decimal-string per
        Bybit convention to avoid float drift.
        """
        body: dict[str, Any] = {
            "productId": product_id,
            "amount": amount,
            "orderType": side,
        }
        if order_link_id is not None:
            body["orderLinkId"] = order_link_id
        data = await self._request("POST", "/v5/earn/place-order", body=body)
        return BybitResponse[EarnOrderResult].model_validate(data).result  # type: ignore[return-value]

    async def redeem_from_earn(
        self,
        product_id: str,
        amount: str,
        order_link_id: str | None = None,
    ) -> EarnOrderResult:
        """Named wrapper over `place_earn_order(..., side="Redeem")`. Same
        endpoint, separate method so withdraw-side callers read clearly
        ("redeem from Earn") instead of `place_earn_order(side="Redeem")`.
        """
        return await self.place_earn_order(
            product_id=product_id,
            amount=amount,
            side="Redeem",
            order_link_id=order_link_id,
        )

    async def poll_redemption_credited(
        self,
        coin: str,
        min_credit: str | Decimal,
        timeout_seconds: float = 900,
        interval_seconds: float = 5,
    ) -> Decimal:
        """Poll Bybit wallet until `coin` balance grows by at least
        `min_credit`. Semantically identical to `poll_deposit_credited` —
        same baseline + delta logic. Separate method gives the withdraw
        orchestrator a clearly-named hook and lets us tune defaults
        independently (Flexible Saving redemption is usually <1min; default
        timeout 15min covers congestion).
        """
        return await self.poll_deposit_credited(
            coin=coin,
            min_credit=min_credit,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )

    async def get_earn_positions(self, category: str | None = None) -> list[EarnPosition]:
        data = await self._request("GET", "/v5/earn/position", params={"category": category})
        parsed = BybitResponse[EarnPositionList].model_validate(data)
        return parsed.result.items if parsed.result else []

    async def get_wallet_balance(
        self, coin: str | None = None, account_type: str = "UNIFIED"
    ) -> list[WalletAccount]:
        data = await self._request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": account_type, "coin": coin},
        )
        parsed = BybitResponse[WalletBalanceResult].model_validate(data)
        return parsed.result.items if parsed.result else []

    async def withdraw_to_mantle(
        self,
        coin: str,
        amount: str,
        address: str,
        chain: str = "MANTLE",
    ) -> WithdrawResult:
        """Withdraw `coin` to `address` on Mantle. The destination address
        must be whitelisted in Bybit account settings — Bybit rejects the
        call with retCode=131228 otherwise.
        """
        body = {
            "coin": coin,
            "chain": chain,
            "address": address,
            "amount": amount,
            "accountType": "FUND",
            "forceChain": 1,
        }
        data = await self._request("POST", "/v5/asset/withdraw/create", body=body)
        return BybitResponse[WithdrawResult].model_validate(data).result  # type: ignore[return-value]

    async def get_deposit_address(self, coin: str, chain: str = "MANTLE") -> DepositChain:
        """Return the deposit address for `coin` on `chain`. Bybit returns
        all enabled chains for the coin; we filter client-side because the
        endpoint's `chainType` param is finicky across asset types.

        Raises ValueError if `coin` exists but isn't enabled on `chain` —
        that's an operator misconfiguration (forgot to enable Mantle in
        Bybit UI), not a runtime condition to retry.
        """
        data = await self._request(
            "GET", "/v5/asset/deposit/query-address", params={"coin": coin}
        )
        parsed = BybitResponse[DepositAddressResult].model_validate(data)
        if parsed.result is None:
            raise ValueError(f"no deposit address payload for {coin}")
        wanted = chain.upper()
        for entry in parsed.result.chains:
            if entry.chain.upper() == wanted:
                return entry
        available = [c.chain for c in parsed.result.chains]
        raise ValueError(
            f"no deposit address for {coin} on chain {chain}; available: {available}"
        )

    async def poll_deposit_credited(
        self,
        coin: str,
        min_credit: str | Decimal,
        timeout_seconds: float = 1800,
        interval_seconds: float = 15,
    ) -> Decimal:
        """Block until `coin` balance grows by at least `min_credit` versus
        the baseline captured at call entry. Returns the actual delta.

        Used after a Mantle USDC transfer to a Bybit deposit address — we
        poll the Bybit wallet, not the chain, because Bybit credit lag (a
        few minutes after on-chain confirmation) is the binding wait.

        Raises TimeoutError if not credited within timeout. Sums
        across all account types (UNIFIED + FUND + ...) so transfers that
        land in either are detected.
        """
        threshold = Decimal(str(min_credit))
        baseline = self._sum_coin_balance(await self.get_wallet_balance(coin=coin), coin)
        log.info(
            "bridge_wait_started",
            extra={"coin": coin, "baseline": str(baseline), "threshold": str(threshold)},
        )
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            current = self._sum_coin_balance(
                await self.get_wallet_balance(coin=coin), coin
            )
            delta = current - baseline
            if delta >= threshold:
                log.info(
                    "bridge_wait_credited",
                    extra={"coin": coin, "delta": str(delta)},
                )
                return delta
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"{coin} not credited within {timeout_seconds}s "
                    f"(delta={delta}, needed={threshold})"
                )
            await asyncio.sleep(interval_seconds)

    async def get_spot_order_status(self, order_id: str) -> SpotOrderStatus:
        """Look up a single spot order via `/v5/order/realtime`. Bybit removes
        orders from the realtime endpoint a few seconds after terminal state —
        absence means "finalized", not "doesn't exist". Caller should fall
        back to order/history if they need post-mortem detail.
        """
        data = await self._request(
            "GET",
            "/v5/order/realtime",
            params={"category": "spot", "orderId": order_id},
        )
        parsed = BybitResponse[SpotOrderStatusList].model_validate(data)
        if parsed.result is None or not parsed.result.items:
            raise BybitOrderError(
                f"order {order_id} not found in realtime (likely already finalized)"
            )
        return parsed.result.items[0]

    async def poll_spot_order_filled(
        self,
        order_id: str,
        timeout_seconds: float = 120,
        interval_seconds: float = 2,
    ) -> Decimal:
        """Poll until the spot order reaches `Filled`. Returns `cumExecQty`
        as Decimal (the base-coin amount actually received).

        Raises BybitOrderError on any terminal non-Filled state — those are
        operator/exchange-side failures (insufficient balance, lot-size
        violation, manual cancel) that the orchestrator must surface, not
        silently retry.
        """
        _TERMINAL_BAD = {
            "Cancelled",
            "Rejected",
            "Deactivated",
            "PartiallyFilledCanceled",  # note Bybit's actual spelling
        }
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            status = await self.get_spot_order_status(order_id)
            if status.orderStatus == "Filled":
                return Decimal(status.cumExecQty)
            if status.orderStatus in _TERMINAL_BAD:
                raise BybitOrderError(
                    f"order {order_id} terminal status={status.orderStatus} "
                    f"reason={status.rejectReason}"
                )
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"order {order_id} not filled within {timeout_seconds}s "
                    f"(last_status={status.orderStatus}, cumExecQty={status.cumExecQty})"
                )
            await asyncio.sleep(interval_seconds)

    @staticmethod
    def _sum_coin_balance(accounts: list[WalletAccount], coin: str) -> Decimal:
        """Sum walletBalance for `coin` across all returned accounts.
        Bybit returns decimal strings; Decimal avoids drift on small credits.
        """
        total = Decimal(0)
        for account in accounts:
            for entry in account.coin:
                if entry.coin == coin:
                    total += Decimal(entry.walletBalance)
        return total

    async def place_spot_order(
        self,
        symbol: str,
        side: Side,
        qty: str,
        order_type: Literal["Market", "Limit"] = "Market",
        price: str | None = None,
        order_link_id: str | None = None,
    ) -> SpotOrderResult:
        """Place a spot order. Caller is responsible for honoring per-symbol
        lot size / min-notional rules — this client is a thin passthrough.
        """
        body: dict[str, Any] = {
            "category": "spot",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": qty,
        }
        if price is not None:
            body["price"] = price
        if order_link_id is not None:
            body["orderLinkId"] = order_link_id
        data = await self._request("POST", "/v5/order/create", body=body)
        return BybitResponse[SpotOrderResult].model_validate(data).result  # type: ignore[return-value]
