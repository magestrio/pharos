"""Snapshot collector for the Bybit-only sandbox loop.

One call → one JSON blob carrying everything the Phase B LLM ranker
needs to decide:

- current wallet across all account types (single asset-overview call)
- any open Earn / LM positions (empty list + warning on sandbox sub-
  account per .4 Earn-permission gate)
- ranked top-20 products per category (FlexibleSaving, OnChain, LM)
- BTC + ETH market regime (price, 24h change, funding)
- USDC peg deviation (CoinGecko)

Advance-Earn categories (DualAssets, DiscountBuy, SmartLeverage,
DoubleWin) are intentionally excluded from the MVP per .22 finding —
those are mostly USDT-paired structured products with non-uniform
yield-field naming; low fit for a vUSDC ranker. Add them back as a
follow-up once Phase B prompt iteration shows the LLM wants them.

Output goes to `agent/sandbox/snapshots/<UTC-ts>.json`. The dir is
gitignored per CLAUDE.md alongside captures/decisions/executions.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agent.bybit_oracle.bybit_client import (
    BybitAPIError,
    BybitClient,
    FlexibleEarnProduct,
    LinearTicker,
    OnChainEarnProduct,
)
from agent.bybit_oracle.promo_whitelist import get_promo_effective_apr

SCHEMA_VERSION = 1
TOP_K = 20
_EARN_PERMISSION_RET_CODE = 10005


class ProductSummary(BaseModel):
    """One Earn product, normalized across category families.

    `effective_apr` is the rate the ranker uses — promo-whitelist
    override first, then `estimateApr` (basic Earn) or `apyE8` (LM),
    else `0`. `apr_source` tells the LLM where the number came from so
    it can downweight noisy sources (e.g. discount any product flagged
    `missing` instead of treating its 0% as a real rank).
    """

    model_config = ConfigDict(extra="ignore")

    category: str  # FlexibleSaving | OnChain | LiquidityMining
    product_id: str
    coin: str  # for LM: f"{baseCoin}/{quoteCoin}"
    effective_apr: Decimal  # fractional [0, 1]
    apr_source: str  # promo_whitelist | estimate_apr | apy_e8 | missing
    base_apr_string: str | None = None  # raw Bybit value (debug / audit)
    redeem_lockup_minutes: int | None = None
    notes: list[str] = Field(default_factory=list)


class WalletSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    total_equity_usd: Decimal
    # Raw `list` from `/v5/asset/asset-overview` — preserves the
    # `accountType` (long-form `UnifiedTradingAccount` etc.) +
    # optional `coinDetail` / `categories` blocks per .19.
    accounts: list[dict[str, Any]] = Field(default_factory=list)


class MarketSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    btc_price: Decimal | None = None
    btc_24h_change_pct: Decimal | None = None  # signed, e.g. +1.5 = +1.5%
    btc_funding_rate: Decimal | None = None  # current 8h funding
    eth_price: Decimal | None = None
    eth_24h_change_pct: Decimal | None = None
    eth_funding_rate: Decimal | None = None


class UsdcPegSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    price_usd: Decimal | None = None
    deviation_bps: Decimal | None = None  # (price - 1.0) * 10000
    source: str = "coingecko"
    fetched_at: datetime


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: int = SCHEMA_VERSION
    captured_at: datetime
    wallet: WalletSnapshot
    earn_positions: list[dict[str, Any]] = Field(default_factory=list)
    lm_positions: list[dict[str, Any]] = Field(default_factory=list)
    products: dict[str, list[ProductSummary]] = Field(default_factory=dict)
    market: MarketSnapshot
    usdc_peg: UsdcPegSnapshot
    # Non-fatal per-source warnings (e.g. Earn permission gate). Fatal
    # errors propagate from `collect_snapshot` — the caller decides
    # whether a missing snapshot is recoverable.
    errors: list[str] = Field(default_factory=list)


def _parse_percent(value: str | None) -> Decimal | None:
    """Parse Bybit's APR strings (`"0.65%"`, `"3.987471%"`, sometimes
    `""`) into fractional Decimal. Returns `None` on missing/malformed
    so the ranker can route those to `apr_source="missing"` instead of
    silently treating them as 0%."""
    if not value:
        return None
    s = value.strip().rstrip("%").strip()
    if not s:
        return None
    try:
        return Decimal(s) / Decimal(100)
    except InvalidOperation:
        return None


def _flex_or_onchain_summary(
    p: FlexibleEarnProduct | OnChainEarnProduct, category: str
) -> ProductSummary:
    base = _parse_percent(p.estimateApr)
    promo = get_promo_effective_apr(category, p.productId)
    if promo is not None:
        eff, src = promo, "promo_whitelist"
    elif base is not None:
        eff, src = base, "estimate_apr"
    else:
        eff, src = Decimal(0), "missing"

    lockup: int | None = None
    rpm = p.redeemProcessingMinute
    if rpm is not None:
        try:
            lockup = int(rpm)
        except (TypeError, ValueError):
            lockup = None

    notes: list[str] = []
    if isinstance(p, OnChainEarnProduct):
        if p.duration == "Fixed" and p.term:
            notes.append(f"fixed_term_days={p.term}")
        if p.swapCoin:
            notes.append(f"swap_to={p.swapCoin}")
    if p.bonusEvents:
        notes.append(f"bonus_events={len(p.bonusEvents)}")

    return ProductSummary(
        category=category,
        product_id=p.productId,
        coin=p.coin,
        effective_apr=eff,
        apr_source=src,
        base_apr_string=p.estimateApr,
        redeem_lockup_minutes=lockup,
        notes=notes,
    )


def _lm_summary(p: dict[str, Any]) -> ProductSummary:
    """LM products report APY as `apyE8` (integer in e8 precision, per
    .24). Divide by 1e8 to get the fractional rate."""
    raw = p.get("apyE8", "0")
    try:
        apy = Decimal(str(raw)) / Decimal("1e8")
        src = "apy_e8"
    except InvalidOperation:
        apy, src = Decimal(0), "missing"

    notes: list[str] = []
    lev = p.get("maxLeverage")
    if lev is not None:
        notes.append(f"max_leverage={lev}")

    return ProductSummary(
        category="LiquidityMining",
        product_id=str(p["productId"]),
        coin=f"{p.get('baseCoin', '?')}/{p.get('quoteCoin', '?')}",
        effective_apr=apy,
        apr_source=src,
        base_apr_string=None,
        redeem_lockup_minutes=None,
        notes=notes,
    )


def _rank(products: list[ProductSummary], top_k: int = TOP_K) -> list[ProductSummary]:
    """Sort by effective APR descending, cap at top_k. Stable sort —
    ties preserve Bybit's listing order."""
    return sorted(products, key=lambda s: s.effective_apr, reverse=True)[:top_k]


async def _safe_earn(
    coro, errors: list[str], label: str, default: Any
) -> Any:
    """Swallow `BybitAPIError(retCode=10005)` (Earn permission denied on
    the sandbox sub-account, per `.4`) and return `default` while
    appending a warning to `errors`. Other Bybit errors propagate —
    those are real problems, not the sub-account permission gate.
    """
    try:
        return await coro
    except BybitAPIError as e:
        if e.ret_code == _EARN_PERMISSION_RET_CODE:
            errors.append(
                f"{label}: Earn permission denied on sub-account "
                "(expected pre-unblock per .4)"
            )
            return default
        raise


def _ticker_24h(ticker: LinearTicker | None) -> Decimal | None:
    """Bybit returns `price24hPcnt` as a signed fractional string
    (`"0.01"` = +1%). Convert to percent form for the LLM so the prompt
    can read `+1.5` instead of `0.015` and not get the unit wrong."""
    if ticker is None or not ticker.price24hPcnt:
        return None
    try:
        return Decimal(ticker.price24hPcnt) * Decimal(100)
    except InvalidOperation:
        return None


def _ticker_price(ticker: LinearTicker | None) -> Decimal | None:
    if ticker is None or not ticker.lastPrice:
        return None
    try:
        return Decimal(ticker.lastPrice)
    except InvalidOperation:
        return None


def _ticker_funding(ticker: LinearTicker | None) -> Decimal | None:
    if ticker is None or not ticker.fundingRate:
        return None
    try:
        return Decimal(ticker.fundingRate)
    except InvalidOperation:
        return None


async def _fetch_usdc_peg(timeout: float = 5.0) -> UsdcPegSnapshot:
    """Single CoinGecko simple-price call. Public tier allows ~30
    req/min without an API key — plenty for one snapshot per 4h cycle.
    Fail-soft: on any network / parse error the snapshot still includes
    the peg block with nulls + the fetched timestamp."""
    fetched = datetime.now(UTC)
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "usd-coin", "vs_currencies": "usd"},
            )
            resp.raise_for_status()
            data = resp.json()
            price = Decimal(str(data["usd-coin"]["usd"]))
            dev = (price - Decimal(1)) * Decimal(10000)
            return UsdcPegSnapshot(
                price_usd=price, deviation_bps=dev, fetched_at=fetched
            )
    except (httpx.HTTPError, KeyError, ValueError, InvalidOperation):
        return UsdcPegSnapshot(price_usd=None, deviation_bps=None, fetched_at=fetched)


async def collect_snapshot(client: BybitClient) -> Snapshot:
    """Build a full sandbox snapshot. All independent calls run
    concurrently — wall-clock ~= max individual latency (~300ms typ).
    """
    errors: list[str] = []
    captured = datetime.now(UTC)

    # Public + read-scope calls (no permission gate)
    asset_task = asyncio.create_task(client.get_asset_overview())
    flex_task = asyncio.create_task(client.list_earn_products(category="FlexibleSaving"))
    onchain_task = asyncio.create_task(client.list_earn_products(category="OnChain"))
    lm_products_task = asyncio.create_task(client.list_liquidity_mining_products())
    btc_task = asyncio.create_task(
        client.get_tickers(category="linear", symbol="BTCUSDT")
    )
    eth_task = asyncio.create_task(
        client.get_tickers(category="linear", symbol="ETHUSDT")
    )
    peg_task = asyncio.create_task(_fetch_usdc_peg())

    # Earn-permission gated (10005 expected on sandbox)
    earn_pos_task = asyncio.create_task(
        _safe_earn(client.get_earn_positions(), errors, "earn_positions", [])
    )
    lm_pos_task = asyncio.create_task(
        _safe_earn(
            client.get_liquidity_mining_positions(), errors, "lm_positions", []
        )
    )

    asset_overview = await asset_task
    flex_products = await flex_task
    onchain_products = await onchain_task
    lm_products = await lm_products_task
    btc_tickers = await btc_task
    eth_tickers = await eth_task
    usdc_peg = await peg_task
    earn_positions = await earn_pos_task
    lm_positions = await lm_pos_task

    # Wallet
    try:
        total_equity = Decimal(str(asset_overview.get("totalEquity", "0") or "0"))
    except InvalidOperation:
        total_equity = Decimal(0)
    accounts = asset_overview.get("list", []) or []

    # Products: normalize + rank
    products = {
        "FlexibleSaving": _rank(
            [_flex_or_onchain_summary(p, "FlexibleSaving") for p in flex_products]
        ),
        "OnChain": _rank(
            [_flex_or_onchain_summary(p, "OnChain") for p in onchain_products]
        ),
        "LiquidityMining": _rank([_lm_summary(p) for p in lm_products]),
    }

    # Market — take first ticker per symbol (single-symbol query returns one row)
    btc = btc_tickers[0] if btc_tickers else None
    eth = eth_tickers[0] if eth_tickers else None
    market = MarketSnapshot(
        btc_price=_ticker_price(btc),
        btc_24h_change_pct=_ticker_24h(btc),
        btc_funding_rate=_ticker_funding(btc),
        eth_price=_ticker_price(eth),
        eth_24h_change_pct=_ticker_24h(eth),
        eth_funding_rate=_ticker_funding(eth),
    )

    # Convert raw position objects (pydantic models or dicts) to dicts
    # for JSON serialization without leaking pydantic types into Snapshot.
    earn_positions_dump = [
        p.model_dump(mode="json") if hasattr(p, "model_dump") else p
        for p in earn_positions
    ]

    return Snapshot(
        captured_at=captured,
        wallet=WalletSnapshot(total_equity_usd=total_equity, accounts=accounts),
        earn_positions=earn_positions_dump,
        lm_positions=lm_positions,
        products=products,
        market=market,
        usdc_peg=usdc_peg,
        errors=errors,
    )


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def write_snapshot(snap: Snapshot, snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    ts = snap.captured_at.strftime("%Y%m%dT%H%M%SZ")
    path = snapshot_dir / f"{ts}.json"
    path.write_text(snap.model_dump_json(indent=2))
    return path


def _main() -> None:
    import argparse

    from dotenv import load_dotenv

    from agent.bybit_oracle.config import OracleSettings

    parser = argparse.ArgumentParser(description="Collect one Bybit sandbox snapshot.")
    parser.add_argument(
        "--env-file",
        default=None,
        help="dotenv to load (e.g. ~/.config/vault8004/bybit-sandbox.env)",
    )
    args = parser.parse_args()

    async def run() -> None:
        if args.env_file:
            load_dotenv(args.env_file, override=True)
        async with BybitClient.from_settings(OracleSettings()) as client:
            snap = await collect_snapshot(client)
        path = write_snapshot(snap)
        total_products = sum(len(v) for v in snap.products.values())
        print(f"snapshot → {path}")
        print(
            f"  total_equity_usd={snap.wallet.total_equity_usd}  "
            f"products={total_products}  "
            f"earn_positions={len(snap.earn_positions)}  "
            f"lm_positions={len(snap.lm_positions)}"
        )
        if snap.usdc_peg.price_usd is not None:
            print(
                f"  usdc_peg={snap.usdc_peg.price_usd} "
                f"({snap.usdc_peg.deviation_bps} bps from $1)"
            )
        for e in snap.errors:
            print(f"  warn: {e}")

    asyncio.run(run())


if __name__ == "__main__":
    _main()
