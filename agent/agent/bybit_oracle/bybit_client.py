"""Bybit V5 REST client.

Direct httpx wrapper instead of the official `pybit` SDK — we already depend
on httpx, and the V5 protocol is just REST + RSA-SHA256, so a thin client
keeps deps frozen and avoids surprises when SDK lags behind new endpoints.

Signing scheme (V5 RECV_WINDOW header style, RSA / sign-type=1):
    sign_string = timestamp + api_key + recv_window + payload
    signature   = base64(rsa_sha256_pkcs1v15(private_key, sign_string))
where `payload` is the URL-encoded query string for GET/DELETE and the raw
JSON body for POST/PUT. Bybit V5 expects PKCS#1 v1.5 padding (NOT PSS).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.parse
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Generic, Literal, TypeVar, Union

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

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


class BonusEvent(BaseModel):
    """Promotional APR layered on top of `estimateApr` (e.g. "Yesterday's
    Rewards APR"). Bybit returns these for products under active campaigns.
    Discrepancy between UI promo number and API `estimateApr` typically
    lives here — Phase A.3 observation."""

    model_config = ConfigDict(extra="ignore")

    apr: str | None = None
    coin: str | None = None
    announcement: str | None = None


class _BaseEarnProduct(BaseModel):
    """Shared fields across FlexibleSaving + OnChain legacy Earn products.
    Not exported on its own — use the per-category subclasses
    (`FlexibleEarnProduct`, `OnChainEarnProduct`) or parse a raw payload
    via `parse_earn_product()` (or directly the `EarnProduct` union
    annotation in another pydantic model).
    """

    model_config = ConfigDict(extra="ignore")

    productId: str
    coin: str
    status: str | None = None  # Available | NotAvailable
    estimateApr: str | None = None
    minStakeAmount: str | None = None
    maxStakeAmount: str | None = None
    precision: str | None = None
    bonusEvents: list[BonusEvent] = Field(default_factory=list)
    minRedeemAmount: str | None = None
    maxRedeemAmount: str | None = None
    rewardDistributionType: str | None = None  # Simple | Compound | Other
    rewardIntervalMinute: int | None = None
    # Bybit V5 returns this inconsistently — `"0"` for some products,
    # raw int `0` for others (observed live 2026-05-27 on `list_earn_
    # products(FlexibleSaving, USDC)`). Accept both.
    redeemProcessingMinute: int | str | None = None


class FlexibleEarnProduct(_BaseEarnProduct):
    """`/v5/earn/product?category=FlexibleSaving` row. No lockup —
    `duration` is always `"Flexible"` (or empty string for legacy
    products); there is no `term` field server-side.
    """

    category: Literal["FlexibleSaving"]
    duration: str | None = None  # Flexible | "" (legacy)


class OnChainEarnProduct(_BaseEarnProduct):
    """`/v5/earn/product?category=OnChain` row. Carries the Fixed-vs-
    Flexible discriminator on `duration` + `term`:

    - `duration == "Fixed"` AND `term > 0`  →  lockup window in days,
      `stakeTime` + `term` give the settlement deadline. Validator
      (`.9`) must reject staking when `settlementTime < now + rebalance
      interval`.
    - `duration == "Flexible"` AND `term == 0`  →  instant redeem,
      same shape as `FlexibleEarnProduct` semantically.

    LST products (cmETH-like wrappers) populate the `swapCoin` /
    `*ExchangeRate` fields — orchestrator swaps `coin` → `swapCoin`
    at `stakeExchangeRate`.
    """

    category: Literal["OnChain"]
    duration: str | None = None  # Fixed | Flexible | "" (legacy)
    term: int | None = None  # in days; non-zero only for Fixed
    swapCoin: str | None = None
    swapCoinPrecision: str | None = None
    stakeExchangeRate: str | None = None
    redeemExchangeRate: str | None = None
    stakeTime: str | None = None  # unix ms as string
    interestCalculationTime: str | None = None  # unix ms as string


# Discriminated union for legacy `/v5/earn/product`. Use as a type
# annotation inside other pydantic models (pydantic resolves the
# discriminator natively when the field is `list[EarnProduct]` or
# `EarnProduct`). For ad-hoc parsing from a raw dict, call
# `parse_earn_product()`.
EarnProduct = Annotated[
    Union[FlexibleEarnProduct, OnChainEarnProduct],
    Field(discriminator="category"),
]

_EARN_PRODUCT_ADAPTER: TypeAdapter[Union[FlexibleEarnProduct, OnChainEarnProduct]] = (
    TypeAdapter(EarnProduct)
)


def parse_earn_product(data: dict[str, Any]) -> FlexibleEarnProduct | OnChainEarnProduct:
    """Parse one raw `/v5/earn/product` item into the matching typed
    subclass, discriminated by `category`. Use this in test fixtures and
    one-off parsing; inside other pydantic models prefer annotating the
    field with `EarnProduct` directly so pydantic does the dispatch.
    """
    return _EARN_PRODUCT_ADAPTER.validate_python(data)


class EarnProductList(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    items: list[EarnProduct] = Field(default_factory=list, alias="list")


class FreezeDetail(BaseModel):
    """A portion of an Earn position locked out of redemption (e.g.
    collateralizing a Fixed-Rate Loan). `availableAmount` on the parent
    position already nets these out — useful here for explainability."""

    model_config = ConfigDict(extra="ignore")

    amount: str | None = None
    description: str | None = None


class EarnPosition(BaseModel):
    """Basic Earn position per /v5/earn/position. `category` is added by
    the gather layer (Bybit doesn't echo it in the response) — kept
    optional so direct API calls don't break parsing.

    OnChain positions carry the richer lifecycle fields (id, orderId,
    estimate*Time, settlementTime, freezeDetails). FlexibleSaving
    positions typically only populate amount / availableAmount /
    autoReinvest / claimableYield.
    """

    model_config = ConfigDict(extra="ignore")

    productId: str
    coin: str
    amount: str
    category: str | None = None
    status: str | None = None  # Processing | Active (OnChain only)
    totalPnl: str | None = None  # OnChain non-LST only
    claimableYield: str | None = None
    id: str | None = None  # position id (OnChain only)
    orderId: str | None = None
    estimateRedeemTime: str | None = None  # unix ms
    estimateStakeTime: str | None = None  # unix ms
    estimateInterestCalculationTime: str | None = None  # unix ms
    settlementTime: str | None = None  # unix ms, OnChain Fixed
    autoReinvest: str | None = None  # Enable | Disable
    availableAmount: str | None = None
    freezeDetails: list[FreezeDetail] = Field(default_factory=list)


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
AccountType = Literal["FUND", "UNIFIED"]
EarnCategory = Literal["FlexibleSaving", "OnChain"]
AdvanceEarnCategory = Literal["SmartLeverage", "DiscountBuy", "DualAssets", "DoubleWin"]

# Per the V5 enum (Advanced-Earn-category, checked 2026-05-27).
# All four share /v5/earn/advance/* and discriminate by `category`.
# LiquidityMining was originally bucketed here but it lives in its own
# namespace `/v5/earn/liquidity-mining/*` (verified .24, 2026-05-27)
# with a different shape (baseCoin/quoteCoin LP pair instead of
# single-coin stake) — use the dedicated `list_liquidity_mining_products`
# family of methods instead.
ADVANCE_EARN_CATEGORIES: frozenset[str] = frozenset(
    {"SmartLeverage", "DiscountBuy", "DualAssets", "DoubleWin"}
)


class LinearTicker(BaseModel):
    """Single perpetual ticker entry from `/v5/market/tickers?category=linear`."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    lastPrice: str
    markPrice: str | None = None
    fundingRate: str | None = None  # current 8h funding, signed decimal-string
    nextFundingTime: str | None = None  # unix ms as string
    openInterestValue: str | None = None  # USD
    price24hPcnt: str | None = None  # 24h % change, signed decimal (0.01 = +1%)


class TickerList(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    items: list[LinearTicker] = Field(default_factory=list, alias="list")


class OrderbookSnapshot(BaseModel):
    """`/v5/market/orderbook` payload. `b` is bids, `a` is asks, each a
    list of [price, size] decimal-strings, best price first."""

    model_config = ConfigDict(extra="ignore")

    s: str  # symbol
    b: list[list[str]] = Field(default_factory=list)
    a: list[list[str]] = Field(default_factory=list)


class InstrumentLotSizeFilter(BaseModel):
    model_config = ConfigDict(extra="ignore")
    maxOrderQty: str | None = None
    minOrderQty: str | None = None
    qtyStep: str | None = None


class InstrumentLeverageFilter(BaseModel):
    model_config = ConfigDict(extra="ignore")
    maxLeverage: str | None = None
    minLeverage: str | None = None


class LinearInstrument(BaseModel):
    model_config = ConfigDict(extra="ignore")
    symbol: str
    status: str | None = None
    leverageFilter: InstrumentLeverageFilter | None = None
    lotSizeFilter: InstrumentLotSizeFilter | None = None


class InstrumentList(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    items: list[LinearInstrument] = Field(default_factory=list, alias="list")


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
        private_key: rsa.RSAPrivateKey,
        base_url: str = "https://api.bybit.com",
        recv_window: int = 5000,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._private_key = private_key
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
        if not key:
            raise RuntimeError("BYBIT_API_KEY is required to call private endpoints")
        pem_path = Path(cfg.BYBIT_PRIVATE_KEY_PATH).expanduser()
        if not pem_path.is_file():
            raise RuntimeError(
                f"BYBIT_PRIVATE_KEY_PATH={pem_path} does not exist — "
                "generate the RSA keypair and register the public PEM in Bybit UI"
            )
        loaded = serialization.load_pem_private_key(pem_path.read_bytes(), password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise RuntimeError(
                f"BYBIT_PRIVATE_KEY_PATH={pem_path} is not an RSA private key"
            )
        return cls(
            api_key=key,
            private_key=loaded,
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
        sig = self._private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def _auth_headers(self, timestamp: str, signature: str) -> dict[str, str]:
        return {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self._recv_window,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "1",
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
    ) -> list[FlexibleEarnProduct | OnChainEarnProduct]:
        """List Earn products via the legacy `/v5/earn/product` endpoint.
        `category` accepts `FlexibleSaving` (default) and `OnChain`. Other
        Earn families live on different paths — use
        `list_extended_earn_products` for those.
        """
        data = await self._request(
            "GET", "/v5/earn/product", params={"category": category, "coin": coin}
        )
        parsed = BybitResponse[EarnProductList].model_validate(data)
        return parsed.result.items if parsed.result else []

    # All advance-Earn categories share the same endpoint family
    # (/v5/earn/advance/{product,product-extra-info,position,place-order,
    # get-redeem-est-amount-list}) and discriminate by `category`. Schemas
    # vary per category, so list/quote/position methods return raw dicts.
    _ADVANCE_EARN_CATEGORIES: frozenset[str] = ADVANCE_EARN_CATEGORIES

    async def list_advance_earn_products(
        self, category: str, coin: str | None = None
    ) -> list[dict[str, Any]]:
        """List Earn products for advance-Earn categories
        (SmartLeverage, DiscountBuy, DualAssets, DoubleWin). Returns raw
        dicts since per-category schemas differ. Raises `ValueError` for
        unknown categories.
        """
        self._require_advance_category(category)
        data = await self._request(
            "GET",
            "/v5/earn/advance/product",
            params={"category": category, "coin": coin},
        )
        result = data.get("result") or {}
        items = result.get("list", [])
        return list(items) if isinstance(items, list) else []

    async def get_advance_product_quote(
        self, category: str, product_id: str | None = None
    ) -> dict[str, Any]:
        """Fetch the latest quote for an advance-Earn product via
        `/v5/earn/advance/product-extra-info`. For Stake orders this is a
        mandatory pre-call — `initialPrice`, `breakevenPrice`, `apyE8`,
        etc. must be echoed back into place-order. Returns the raw `result`
        dict because per-category fields differ (DiscountBuy returns
        `offers: [...]`; SmartLeverage returns a single quote object).
        """
        self._require_advance_category(category)
        data = await self._request(
            "GET",
            "/v5/earn/advance/product-extra-info",
            params={"category": category, "productId": product_id},
        )
        return data.get("result") or {}

    async def get_advance_earn_positions(
        self, category: str, product_id: str
    ) -> list[dict[str, Any]]:
        """Query open advance-Earn positions. Both `category` and
        `product_id` are required by the V5 endpoint. Returns raw dicts —
        position payloads carry per-category fields (positionId,
        strikePrice, breakevenPrice, expiryTime, ...) that don't fit the
        flat basic-Earn `EarnPosition` shape.
        """
        self._require_advance_category(category)
        data = await self._request(
            "GET",
            "/v5/earn/advance/position",
            params={"category": category, "productId": product_id},
        )
        result = data.get("result") or {}
        items = result.get("list", [])
        return list(items) if isinstance(items, list) else []

    async def get_redeem_estimate(
        self, category: str, position_ids: list[str] | str
    ) -> dict[str, Any]:
        """Get estimated redeem amount for one or more advance-Earn
        positions. Bybit caches the estimate for ~10min server-side; pass
        it back into `place_advance_earn_order` via the appropriate
        `*RedeemExtra` block.
        """
        self._require_advance_category(category)
        ids = ",".join(position_ids) if isinstance(position_ids, list) else position_ids
        data = await self._request(
            "GET",
            "/v5/earn/advance/get-redeem-est-amount-list",
            params={"category": category, "positionIds": ids},
        )
        return data.get("result") or {}

    async def place_advance_earn_order(
        self,
        *,
        category: str,
        product_id: str,
        side: EarnSide,
        account_type: AccountType,
        order_link_id: str,
        coin: str | None = None,
        amount: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stake or Redeem an advance-Earn product via
        `/v5/earn/advance/place-order`.

        The per-category `*Extra` block (smartLeverageStakeExtra,
        discountBuyExtra, dualAssetsExtra, doubleWinStakeExtra,
        smartLeverageRedeemExtra, ...) is passed as `extra` — caller is
        responsible for constructing it from the matching
        `get_advance_product_quote` / `get_redeem_estimate` response.

        `coin` and `amount` are required for Stake, omitted for Redeem
        (the position carries the coin). Returns raw `{orderId,
        orderLinkId}` dict since the place-order envelope is the same
        across categories.
        """
        self._require_advance_category(category)
        body: dict[str, Any] = {
            "category": category,
            "productId": product_id,
            "orderType": side,
            "accountType": account_type,
            "orderLinkId": order_link_id,
        }
        if coin is not None:
            body["coin"] = coin
        if amount is not None:
            body["amount"] = amount
        if extra:
            body.update(extra)
        data = await self._request("POST", "/v5/earn/advance/place-order", body=body)
        return data.get("result") or {}

    async def get_hourly_yield(
        self,
        category: str,
        product_id: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Historical hourly yield via `/v5/earn/hourly-yield`. Window is
        capped at 7d server-side; paginate with `cursor` for longer
        ranges. Returns the raw `{list, nextPageCursor}` dict.
        """
        params: dict[str, Any] = {"category": category}
        if product_id is not None:
            params["productId"] = product_id
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        data = await self._request("GET", "/v5/earn/hourly-yield", params=params)
        return data.get("result") or {}

    async def get_apr_history(
        self,
        category: str,
        product_id: str,
        days: int = 30,
    ) -> dict[str, Any]:
        """Daily APR history via `/v5/earn/apr-history`. Scoped to
        FlexibleSaving + OnChain categories; 6-month server-side cap.
        Snapshot ranker must use this instead of `EarnProduct.estimateApr`
        — the latter is base APR only and excludes promo/subsidy (USD1
        Flexible: estimateApr=0.65% vs effective 7.52%, a 10x+ gap).

        `days` is windowed client-side into `startTime` / `endTime` so the
        caller doesn't have to compute unix-ms boundaries. Returns the raw
        `{list: [{timestamp, apr}, ...]}` envelope — let Phase B /
        snapshot collector decide the typed shape once we have live
        captures.

        Path note: V5 docs originally placed this under
        `/v5/earn/easy-onchain/apr-history`; live-probe 2026-05-27
        confirmed the deployed path is `/v5/earn/apr-history` (no
        `/easy-onchain` segment).
        """
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * 24 * 60 * 60 * 1000
        data = await self._request(
            "GET",
            "/v5/earn/apr-history",
            params={
                "category": category,
                "productId": product_id,
                "startTime": start_ms,
                "endTime": end_ms,
            },
        )
        return data.get("result") or {}

    async def get_yield_history(
        self,
        category: str,
        start_time: int,
        end_time: int,
        product_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Realized yield records via `/v5/earn/yield`. Post-hoc accruals
        per active position over [start_time, end_time] (unix ms).
        Server caps the window at 7 days and retains 3 months of data;
        paginate with `cursor` for longer scans.

        `product_id` filter is **not** supported for `category=OnChain`
        — Bybit returns an error if passed.

        Distinct from `get_apr_history` (forward-looking daily APR) and
        `get_hourly_yield` (hourly granularity). Returns raw
        `{yield: [...], nextPageCursor}` envelope.

        Path note: V5 docs originally placed this under
        `/v5/earn/easy-onchain/yield-history` (and the V5 changelog
        adds a third spelling `/v5/finance/earn/easy-onchain/yield-history`);
        both 404. Live-probe 2026-05-27 confirmed deployed path is
        `/v5/earn/yield`.
        """
        params: dict[str, Any] = {
            "category": category,
            "startTime": start_time,
            "endTime": end_time,
        }
        if product_id is not None:
            params["productId"] = product_id
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        data = await self._request("GET", "/v5/earn/yield", params=params)
        return data.get("result") or {}

    @classmethod
    def _require_advance_category(cls, category: str) -> None:
        if category not in cls._ADVANCE_EARN_CATEGORIES:
            raise ValueError(
                f"unknown advance-Earn category {category!r}; "
                f"valid: {sorted(cls._ADVANCE_EARN_CATEGORIES)}"
            )

    # ─── Liquidity Mining (own /v5/earn/liquidity-mining/* namespace) ────
    # LM is structurally different from the four advance-Earn categories
    # — products are LP pairs (baseCoin + quoteCoin) with leverage, not
    # single-coin stakes — so it gets its own endpoint family rather than
    # riding /v5/earn/advance/*. All methods return raw dicts per the
    # .17 / .20 Variant C decision; per-category typed models can be
    # added later if `.6` needs them.

    async def list_liquidity_mining_products(
        self,
        base_coin: str | None = None,
        quote_coin: str | None = None,
    ) -> list[dict[str, Any]]:
        """List Liquidity Mining products via
        `/v5/earn/liquidity-mining/product`. Each row carries an LP pair
        (`baseCoin` + `quoteCoin`), `maxLeverage`, `apyE8` / `apy7dE8`
        (e8 precision — divide by 1e8 for the actual rate), pool size,
        and the slippage tier ladder. Returns the inner `products` array
        as raw dicts.
        """
        params: dict[str, Any] = {}
        if base_coin is not None:
            params["baseCoin"] = base_coin
        if quote_coin is not None:
            params["quoteCoin"] = quote_coin
        data = await self._request(
            "GET", "/v5/earn/liquidity-mining/product", params=params
        )
        result = data.get("result") or {}
        items = result.get("products", [])
        return list(items) if isinstance(items, list) else []

    async def get_liquidity_mining_positions(
        self,
        product_id: str | None = None,
        base_coin: str | None = None,
    ) -> list[dict[str, Any]]:
        """Active Liquidity Mining positions via
        `/v5/earn/liquidity-mining/position`. Position amounts are
        dynamically calculated against the current market price —
        `quoteAmount`, `baseAmount`, `currentApr` (e8 precision),
        `liquidationPrice`, etc. all reflect snapshot-at-call.

        Requires Earn permission on the API key; same sub-account
        permission gate as `.4` — expect 10005 on sandbox until
        unblocked.
        """
        params: dict[str, Any] = {}
        if product_id is not None:
            params["productId"] = product_id
        if base_coin is not None:
            params["baseCoin"] = base_coin
        data = await self._request(
            "GET", "/v5/earn/liquidity-mining/position", params=params
        )
        result = data.get("result") or {}
        items = result.get("positions", [])
        return list(items) if isinstance(items, list) else []

    async def get_liquidity_mining_yield_records(
        self,
        base_coin: str | None = None,
        quote_coin: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Yield claim history via `/v5/earn/liquidity-mining/yield-records`.
        Includes both `Manual` claims and `RemoveLiquidity`-settled
        yields. Same Earn-permission gate as the positions endpoint.
        Returns the raw `{records, nextPageCursor}` envelope.
        """
        params: dict[str, Any] = {}
        if base_coin is not None:
            params["baseCoin"] = base_coin
        if quote_coin is not None:
            params["quoteCoin"] = quote_coin
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        data = await self._request(
            "GET", "/v5/earn/liquidity-mining/yield-records", params=params
        )
        return data.get("result") or {}

    async def place_earn_order(
        self,
        *,
        category: EarnCategory,
        product_id: str,
        amount: str,
        side: EarnSide,
        coin: str,
        account_type: AccountType,
        order_link_id: str,
    ) -> EarnOrderResult:
        """Stake or Redeem a basic Earn product (FlexibleSaving | OnChain).
        All seven fields are required by V5 `/v5/earn/place-order`:
        OnChain only accepts `accountType=FUND`; the same `order_link_id`
        cannot be reused within 30min (Bybit dedupes by it).

        For advance categories use `place_advance_earn_order`.
        """
        body: dict[str, Any] = {
            "category": category,
            "productId": product_id,
            "amount": amount,
            "orderType": side,
            "coin": coin,
            "accountType": account_type,
            "orderLinkId": order_link_id,
        }
        data = await self._request("POST", "/v5/earn/place-order", body=body)
        return BybitResponse[EarnOrderResult].model_validate(data).result  # type: ignore[return-value]

    async def redeem_from_earn(
        self,
        *,
        category: EarnCategory,
        product_id: str,
        amount: str,
        coin: str,
        account_type: AccountType,
        order_link_id: str,
    ) -> EarnOrderResult:
        """Named wrapper over `place_earn_order(..., side="Redeem")`. Same
        endpoint, separate method so withdraw-side callers read clearly
        ("redeem from Earn") instead of `place_earn_order(side="Redeem")`.
        """
        return await self.place_earn_order(
            category=category,
            product_id=product_id,
            amount=amount,
            side="Redeem",
            coin=coin,
            account_type=account_type,
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

    async def get_asset_overview(
        self,
        account_type: str | None = None,
        valuation_currency: str | None = None,
        member_id: str | None = None,
    ) -> dict[str, Any]:
        """Single-call holdings across all product categories
        (Spot, Derivatives, Earn, Funding, TradingBot, CopyTrading) for
        master + subaccounts via `/v5/asset/asset-overview`. Replaces
        what would otherwise be N separate `get_wallet_balance(account
        type=...)` calls in the snapshot collector.

        All params optional:
        - `account_type` — filter to one (UNIFIED|FUND|CONTRACT|OPTION|
          Earn|TradingBot|CopyTrading|...); omitted = all accounts
        - `valuation_currency` — fiat to value in; defaults to USD
        - `member_id` — required when master API key queries a subaccount

        Returns raw `{totalEquity, list: [{accountType, totalEquity,
        coinDetail?|categories?}, ...]}`. Typed shape deferred to `.6`
        once we have live captures.

        Path note: the V5 docs originally listed this under
        `/v5/asset/balance/asset-overview`; live-probe 2026-05-27
        confirmed the deployed path is `/v5/asset/asset-overview`
        (without `/balance`).
        """
        params: dict[str, Any] = {}
        if account_type is not None:
            params["accountType"] = account_type
        if valuation_currency is not None:
            params["valuationCurrency"] = valuation_currency
        if member_id is not None:
            params["memberId"] = member_id
        data = await self._request("GET", "/v5/asset/asset-overview", params=params)
        return data.get("result") or {}

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

    # ─── Public market data (signed harmlessly for consistency) ──────────

    async def get_tickers(
        self, category: str = "linear", symbol: str | None = None
    ) -> list[LinearTicker]:
        """`/v5/market/tickers`. For `category=linear`, each item carries
        `fundingRate` and `markPrice`."""
        data = await self._request(
            "GET", "/v5/market/tickers", params={"category": category, "symbol": symbol}
        )
        parsed = BybitResponse[TickerList].model_validate(data)
        return parsed.result.items if parsed.result else []

    async def get_orderbook(
        self, symbol: str, category: str = "linear", limit: int = 50
    ) -> OrderbookSnapshot | None:
        """`/v5/market/orderbook`. `limit` is depth levels per side; Bybit
        caps at 200 for linear."""
        data = await self._request(
            "GET",
            "/v5/market/orderbook",
            params={"category": category, "symbol": symbol, "limit": limit},
        )
        parsed = BybitResponse[OrderbookSnapshot].model_validate(data)
        return parsed.result

    async def get_instruments_info(
        self, category: str = "linear", symbol: str | None = None
    ) -> list[LinearInstrument]:
        """`/v5/market/instruments-info`. For `category=linear`, each item
        carries `leverageFilter.maxLeverage` and lot-size constraints."""
        data = await self._request(
            "GET",
            "/v5/market/instruments-info",
            params={"category": category, "symbol": symbol},
        )
        parsed = BybitResponse[InstrumentList].model_validate(data)
        return parsed.result.items if parsed.result else []
