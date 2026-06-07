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
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, computed_field

from agent.bybit_oracle.bybit_client import (
    BybitAPIError,
    BybitClient,
    FlexibleEarnProduct,
    LinearTicker,
    OnChainEarnProduct,
    PerpPosition,
)
from agent.data.allora_client import (
    SUPPORTED_TOKENS as ALLORA_TOKENS,
    SUPPORTED_WINDOWS as ALLORA_WINDOWS,
    AlloraClient,
    AlloraInference,
)
from agent.reason.venues import CARRY_CATEGORY
from agent.sandbox.on_chain import (
    AAVE_V3_POOL_ADDRESS,
    AaveV3UsdcState,
    fetch_aave_v3_usdc_state,
    make_mantle_client,
    micro_to_usd,
)

SCHEMA_VERSION = 1
TOP_K = 20
# Per-category quote fan-out cap. Each quote is a separate /v5/earn/advance/
# product-extra-info call (~150ms). 5×2 yield-bearing categories = 10 quote
# calls per snapshot — bounded cost. The remaining advance products in
# the top-K list are still surfaced for visibility, just without APR.
ADVANCE_QUOTE_TOP_K = 10
# Per-coin perp fetch cap for hedge data. Each coin = 4 calls (ticker +
# orderbook + instruments-info + funding-history) parallelized; 16
# coins = 64 calls total, comfortably under Bybit's public-market rate
# limit. Bumped from 8 (`.47` 2026-05-29 follow-up) — OnChain alone
# routinely produces 7-8 non-stables, leaving zero budget for
# FlexibleSaving non-stables (ID, IO, AGIX, etc.). Validator rejects
# any non-stable Earn pick without a perp_market entry, so silent
# under-fetch turns into a hard skipped-cycle.
PERP_HEDGE_TOP_K = 16
# Funding-carry venue (`bybit-strategy-expansion.2`). Number of carry
# candidates surfaced in `products["FundingCarry"]` after the friction-
# adjusted ranking. Pre-fan-out we filter the all-linear-tickers feed
# down to `FUNDING_CARRY_PRESELECT_K` using single-period `fundingRate`
# (cheap proxy), then `_fetch_perp_info` resolves the 7d avg for those
# coins; the final list ranks by friction-adjusted `effective_apr` and
# is capped at `FUNDING_CARRY_TOP_K`.
FUNDING_CARRY_TOP_K = 10
FUNDING_CARRY_PRESELECT_K = 20
# Default funding-interval assumption when Bybit doesn't echo
# `fundingIntervalHour` for a symbol. 8h covers the majority of mature
# perps; 4h is the other common case (memecoins / new listings).
# Defaulting to 8h is the conservative choice for annualization — it
# UNDER-states APR by ~2× when the real interval is 4h, which sinks
# rank rather than over-promising yield. Callers must surface the
# fallback in errors so operator can spot symbols missing the field.
DEFAULT_FUNDING_INTERVAL_HOURS = Decimal("8")
# Total hours per year. Used in `funding_per_period × (HOURS_PER_YEAR /
# interval_hours)` annualization; equals `(24/interval) × 365` rewritten
# to avoid the float fraction.
HOURS_PER_YEAR = Decimal("8760")
# Minimum 7-day-avg funding ANNUALIZED (signed Decimal) for a carry
# pick to survive validator + ranker. Compares against the **annualized**
# rate to stay correct across 4h/8h/1h funding intervals — comparing
# per-period rates would over-reject 4h coins whose per-period rate is
# naturally ~½ of an 8h coin at the same annualized yield. 0.05475 =
# 0.00005/8h × 1095 (3×365) — preserves the original "+5.5% gross
# annualized" intent the spec was written around. Hedge case uses a
# looser floor (`agent.validate.rules.FUNDING_FLOOR_HEDGE_ANNUAL`,
# −0.1095 = −11%/year) because Earn APR there absorbs slightly-negative
# funding; carry has no such cushion.
FUNDING_FLOOR_CARRY_ANNUAL = Decimal("0.05475")
# Minimum perp orderbook depth within ±50 bps of mark, USD. Below this
# a carry-sized open/close would cross more than ~3 bps slippage and
# eat the funding subsidy fast. $50k is the soft cutoff for "we can
# round-trip a small-vault carry pick without market impact"; can scale
# down once sizing model accounts for ordersize/depth more granularly.
MIN_CARRY_DEPTH_USD = Decimal("50000")
# Cap on the per-pick USD notional as a fraction of total book — both
# protects the carry stack from concentration (one position blowing up
# liquidation through min_notional) AND ensures the venue cap (`.3`)
# can support the pick by sizing. The `(pick_usd ≤ X% × book)` rule is
# also what lets us pre-reject coins whose `min_notional_usd` exceeds
# this fraction (instrument so chunky we'd violate the venue cap just
# clearing min).
MAX_CARRY_NOTIONAL_FRACTION = Decimal("0.05")
# Annualized friction estimate for carry round-trip: swap entry+exit
# (taker ~10bps × 2) + perp open+close (~5bps × 2) + 1-2 entries per
# month average ≈ 1.8% drag. Conservative placeholder; recalibrate in
# `.6` smoke from realized P&L vs predicted carry.
FUNDING_CARRY_FRICTION_ANNUAL = Decimal("0.018")

# Reallocation horizon used to amortize zero-yield dead-time (subscribe
# warmup + post-redeem processing) into `effective_apr_net_holding`. The
# vault reallocates weekly; a product whose dead-time eats N of these 7
# days realizes only `(H − N)/H` of its headline APR over one hold. Same
# 7-day horizon the validator's lockup cap uses (validate.rules).
HOLDING_HORIZON_MINUTES = 7 * 24 * 60  # 10080

_EARN_PERMISSION_RET_CODE = 10005

# Stables-set used to guarantee USDC-equivalent picks always survive the
# top-K ranker, even when their APR ranks below alt-coin products. The
# vault is USDC-denominated, so a stable pick is always strategically
# interesting — leaving it out of the snapshot just because USDC pays
# 0.6% while ALT pays 92% would force the LLM into a token-risk trade.
STABLES: frozenset[str] = frozenset(
    {"USDC", "USDT", "USD1", "FDUSD", "DAI", "USDE", "USDTB", "PYUSD", "RLUSD"}
)

# Multiplier on perp-short notional → USDT margin demand. Both validator
# (pre-trade reservation) and executor (USDT budget enforcement) read
# this so adding a non-stable pick can't pass one layer's check on a
# tighter buffer and then fail the other on a looser one. Decimal type
# for executor's price-precision arithmetic; validator converts to float
# at the usage site.
HEDGE_MARGIN_BUFFER: Decimal = Decimal("1.05")


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
    apr_source: str  # apr_history | measured_yield | estimate_apr | apy_e8 | aave_pool | quote_dual_offer | quote_discount | missing
    # `effective_apr` discounted for the days capital earns NOTHING during
    # a move — subscribe warmup (`yield_start_delay_min`) + post-redeem
    # processing (`redeem_lockup_minutes`) — amortized over the weekly
    # reallocation horizon (`HOLDING_HORIZON_MINUTES`). This is the rate
    # to compare when deciding whether to CHURN into a product; the gross
    # `effective_apr` overstates the realized return for short holds.
    # None ⇒ no dead-time surfaced (net == gross).
    effective_apr_net_holding: Decimal | None = None
    base_apr_string: str | None = None  # raw Bybit value (debug / audit)
    redeem_lockup_minutes: int | None = None
    # OnChain Fixed-term lockup in days (`OnChainEarnProduct.term`, non-
    # zero only for `duration == "Fixed"`). Distinct from
    # `redeem_lockup_minutes` (post-redeem processing): this is the
    # principal lock until maturity. The validator rejects picks whose
    # fixed term exceeds the 7-day reallocation horizon. None ⇒ not a
    # Fixed-term product (instant / Flexible).
    fixed_term_days: int | None = None
    # Minutes between a fresh subscribe and the moment the position starts
    # ACCRUING yield (OnChain `interestCalculationTime − stakeTime`). Funds
    # subscribed today don't earn until this elapses — zero-yield dead-time
    # that erodes the realized APR over a short hold. None ⇒ not surfaced
    # (Flexible products accrue ~immediately).
    yield_start_delay_min: int | None = None
    # Minimum USD-equivalent the product accepts on a single subscribe.
    # Surfaced from Bybit's raw product fields (`minInvestmentQuote` for
    # LM, future: `minPurchaseAmount` for basic Earn) so the diff layer
    # can SKIP any pick whose `target_amount_usd` is below the product's
    # floor — avoiding live-side `180005` / `180016` rejections.
    # None = unknown / no floor surfaced for this product family.
    min_subscribe_usd: Decimal | None = None
    # Number of decimal places the Earn product accepts for `amount`
    # on `/v5/earn/place-order` (`.precision` field on the raw V5
    # product row). Live retCode=180001 on USDT FlexibleSaving product
    # 1 (2026-06-03) was triggered by sending amount="10.69056" — 5
    # decimals — to a product with precision=4. The planner now
    # quantizes SUBSCRIBE_EARN amounts to this value before emitting
    # the action. None ⇒ unknown precision; executor falls back to
    # passing amount as-is (legacy behavior). LM products don't
    # populate this field — their fill is `addLiquidity` not Earn.
    stake_precision: int | None = None
    notes: list[str] = Field(default_factory=list)


class WalletSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    total_equity_usd: Decimal
    # Raw `list` from `/v5/asset/asset-overview` — preserves the
    # `accountType` (long-form `UnifiedTradingAccount` etc.) +
    # optional `coinDetail` / `categories` blocks per .19.
    accounts: list[dict[str, Any]] = Field(default_factory=list)
    # USDT sitting in UNIFIED — the margin currency for linear perps.
    # Parsed from `accounts` so the diff layer (`.33`) can decide
    # whether to swap USDC → USDT before opening a hedge. Stored as
    # USD-equivalent (USDT ~ $1 by construction).
    usdt_available_usd: Decimal = Decimal(0)
    # Per-coin balances in UNIFIED, in native coin units. Lets the
    # executor diff plan a USDC → pick.coin swap leg when an Earn
    # SUBSCRIBE targets a stable (USD1 / FDUSD / DAI / USDE …) that the
    # wallet doesn't hold yet. Empty dict when the asset-overview has
    # no UNIFIED account.
    unified_coin_balances: dict[str, Decimal] = Field(default_factory=dict)
    # Total USDC the planner can spend on spot swaps this cycle — sum
    # across UNIFIED + FUND. The executor's `_transfer_satisfies_swap`
    # pre-flight can pull FUND USDC into UNIFIED, so for budgeting
    # purposes both wallets count. Used by the swap planner to cap
    # total swap demand and drop tail earn-swaps that would otherwise
    # 170131 the moment USDC drains.
    liquid_usdc_usd: Decimal = Decimal(0)
    # Same as above for USDT — sum across UNIFIED + FUND. The USDT side
    # of the budget covers (a) perp margin (UNIFIED USDT consumed by
    # OPEN_PERP_SHORT) and (b) non-stable Earn Buy swaps on {coin}USDT
    # pairs. Without this cap a small vault can chain 170131 when a
    # USDC→USDT hedge swap drains the available USDT before the next
    # Buy swap fires.
    liquid_usdt_usd: Decimal = Decimal(0)

    # Deployable-budget advisory (`bybit-sandbox.67`). The LLM repeatedly
    # sized NEW Earn picks off `total_equity_usd` even though most of a
    # small vault is locked in `earn_positions` — so it over-committed
    # (e.g. 70% into fresh OnChain stables on a $6-liquid book) and the
    # validator rejected every cycle. These computed fields hand the model
    # the spend ceiling pre-computed so it stops deriving (and ignoring)
    # it. Advisory only — the validator still gates; serialized into the
    # snapshot the LLM reads.

    @computed_field  # type: ignore[prop-decorator]
    @property
    def liquid_stables_usd(self) -> Decimal:
        """Spendable stable cash this cycle (liquid USDC + USDT, UNIFIED+
        FUND): the ceiling on NEW STABLE Earn deployment without redeeming
        anything (a stable subscribe costs ~1x its USD). `total_equity_usd`
        is NOT this number — capital locked in `earn_positions` can't be
        re-deployed until redeemed."""
        return self.liquid_usdc_usd + self.liquid_usdt_usd

    @computed_field  # type: ignore[prop-decorator]
    @property
    def max_new_nonstable_usd(self) -> Decimal:
        """Ceiling on the SUM of NEW hedged non-stable Earn picks this
        cycle. Each costs `pick_usd` (spot Buy) + `pick_usd x 1.05` (perp
        margin) ~= 2.05x, so this is `liquid_stables / (1 + margin buffer)`."""
        denom = Decimal(1) + HEDGE_MARGIN_BUFFER
        return (
            (self.liquid_usdc_usd + self.liquid_usdt_usd) / denom
        ).quantize(Decimal("0.01"))


class MarketSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    btc_price: Decimal | None = None
    btc_24h_change_pct: Decimal | None = None  # signed, e.g. +1.5 = +1.5%
    btc_funding_rate: Decimal | None = None  # current 8h funding
    eth_price: Decimal | None = None
    eth_24h_change_pct: Decimal | None = None
    eth_funding_rate: Decimal | None = None
    # Allora directional forecasts (`mainnet-operations.7`). Empty list
    # when ALLORA_API_KEY is unset OR every fetch in the cycle failed —
    # the LLM treats absence as "no external signal" and falls back to
    # its own market reasoning.
    allora_inferences: list[AlloraInference] = Field(default_factory=list)


class AaveV3UsdcSnapshot(BaseModel):
    """Aave V3 USDC pool state + vault balances on Mantle (`.37a`).

    `supply_apr` is fractional (0.0345 = 3.45% APY). Vault balances are
    USD-equivalent (USDC at 1:1) — the raw micro-units stay in the
    on_chain layer; here we surface the dollars-per-the-LLM."""

    model_config = ConfigDict(extra="ignore")
    block_number: int
    fetched_at: datetime
    pool_address: str
    supply_apr: Decimal
    vault_usdc_usd: Decimal
    vault_ausdc_usd: Decimal


class OnChainState(BaseModel):
    """Mantle on-chain context for the LLM. Currently scoped to Aave V3
    USDC (`.37a`); future venues (Lendle, Pendle, etc.) attach as
    sibling fields."""

    model_config = ConfigDict(extra="ignore")
    aave_v3_usdc: AaveV3UsdcSnapshot | None = None


class UsdcPegSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    price_usd: Decimal | None = None
    deviation_bps: Decimal | None = None  # (price - 1.0) * 10000
    source: str = "coingecko"
    fetched_at: datetime


class PerpInfo(BaseModel):
    """Linear-perp market context for one coin's USDT-pair (e.g. TONUSDT).

    Feeds the hedging-feasibility rules in the system prompt: Claude
    sizes a short-perp leg against a non-USD Earn pick so the combined
    position is delta-neutral, and the agent should only initiate that
    hedge when funding rate, depth, and min-notional cooperate.

    All fields are best-effort; missing values are `None` and the
    prompt/validator treat them as "can't price this hedge" → skip the
    underlying pick.
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str  # e.g. "TONUSDT"
    # Current per-period funding rate (signed). The "period" is
    # `funding_interval_hours` — typically 8 but 4 is common (memecoins,
    # high-vol perps), and some symbols ship 1h. The field name keeps
    # `_8h` for backwards compat with existing readers (snapshots on
    # disk, backtest fixtures); annualization MUST use the interval
    # field below, NOT a hardcoded × 3 × 365.
    funding_rate_8h: Decimal | None = None
    # 7-day average funding rate (21 periods × interval_hours), signed.
    # Smoother than `funding_rate_8h` for sizing decisions — single-
    # period noise can flip sign while the regime is unchanged.
    # Validator uses the annualized form of this for the "negative
    # funding → exit pick" gate; prompt's effective-APR formula reads
    # it through `_annual_funding` so 4h and 8h coins compare like for
    # like. None = endpoint failed or not enough history for the symbol
    # (newly listed perp).
    funding_rate_7d_avg: Decimal | None = None
    # Funding-cadence period in whole hours. None = Bybit didn't echo
    # the field; readers default to `DEFAULT_FUNDING_INTERVAL_HOURS`
    # (8h). Added 2026-06-03 (`bybit-strategy-expansion.2/.4` fix) —
    # prior code annualized via `× 3 × 365` everywhere, under-stating
    # APR ~2× on 4h funding pairs.
    funding_interval_hours: Decimal | None = None
    mark_price: Decimal | None = None
    # USD volume within ±50 bps of mark across both sides of the book.
    # Bigger → easier to enter/exit a hedge of intended size without slip.
    orderbook_depth_50bps_usd: Decimal | None = None
    min_order_qty: Decimal | None = None  # in base coin
    min_notional_usd: Decimal | None = None  # min_order_qty × mark_price
    # Increment Bybit accepts as the qty step on this symbol. Orders
    # with `qty` not a multiple of `qty_step` get rejected with
    # retCode=10001. Executor rounds down to the nearest step.
    qty_step: Decimal | None = None
    max_leverage: Decimal | None = None  # informational; we always hedge at 1x


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: int = SCHEMA_VERSION
    captured_at: datetime
    wallet: WalletSnapshot
    earn_positions: list[dict[str, Any]] = Field(default_factory=list)
    lm_positions: list[dict[str, Any]] = Field(default_factory=list)
    # Current Bybit Alpha holdings (`.54`). Each row carries `tokenCode`
    # (DEX_<id>), `tokenSymbol`, `chainCode`, `tokenAmount` (native
    # units), `tokenAmountUsd`, `costPrice`, `lastPrice`, `pnl`,
    # `pnlRatio`, `tradeFlag`, `assetStatus`. Empty when the sub-account
    # has no alpha positions or the `/v5/alpha/asset` call failed (which
    # is degraded silently per `_safe_alpha`).
    alpha_positions: list[dict[str, Any]] = Field(default_factory=list)
    products: dict[str, list[ProductSummary]] = Field(default_factory=dict)
    market: MarketSnapshot
    # Per-coin perp market context, indexed by the BASE coin (e.g. "TON").
    # Populated for non-stable coins surfaced in OnChain top-K so the
    # hedging-feasibility rules in the prompt have something to score.
    perp_market: dict[str, PerpInfo] = Field(default_factory=dict)
    # Raw `/v5/earn/advance/product-extra-info` quote payloads, keyed by
    # `"<Category>/<ProductId>"` (pydantic dislikes tuple keys). Carries
    # the actionable offer details (selectPrice, expiredAt, instUid,
    # purchasePrice, knockoutPrice, apyE8) the executor needs to build
    # the per-category `*Extra` block for `place_advance_earn_order`.
    # LLM doesn't see this — it consumes the normalized APR via
    # `products[Category][i].effective_apr` instead.
    advance_earn_quotes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Open advance-Earn positions (`/v5/earn/advance/position`), keyed by
    # `"<Category>/<ProductId>"`. Bybit's `orderLinkId` dedup window is
    # ~30min — re-subscribing to a product we still hold within the
    # position's settlement window opens a SECOND position rather than
    # being deduped, double-locking capital silently. The executor's
    # advance-Earn diff branch reads this and SKIPs re-subscribes when
    # any active position exists for the (category, product_id) pair.
    # Empty list ⇔ no position; missing key ⇔ product fell outside the
    # top-K fan-out (treated as "not held" — best-effort, with the
    # caveat that products outside the quote window also can't be
    # picked, so the dedup only needs to cover the pickable surface).
    advance_earn_positions: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict
    )
    # Mantle on-chain context (`.37a`). Carries Aave V3 USDC pool APR
    # + vault balances so the LLM can compare CEX vs DeFi rates in one
    # snapshot. None when the RPC fetch fails (Mantle outage, missing
    # vault address config) — Bybit side of the snapshot stays usable.
    on_chain_state: OnChainState | None = None
    # Open linear-perp positions (USDT-settled). Drives the close-arm of
    # the executor diff so each cycle reconciles current shorts against
    # `decision.hedges` instead of blindly opening new ones (.32).
    perp_positions: list[PerpPosition] = Field(default_factory=list)
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


def _parse_unix_ms(value: str | None) -> int | None:
    """Parse Bybit's unix-millisecond timestamp strings into int. Returns
    `None` on missing / malformed / non-positive."""
    if not value:
        return None
    try:
        ms = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return ms if ms > 0 else None


def _flex_or_onchain_summary(
    p: FlexibleEarnProduct | OnChainEarnProduct,
    category: str,
    measured_apr: Decimal | None = None,
    apr_history: Decimal | None = None,
) -> ProductSummary:
    # APR resolution order (`.70` re-prioritized apr_history over the
    # noise-prone measured_yield):
    #   1. `apr_history` — mean effective APR from `/v5/earn/apr-history`.
    #      Pool-level, subsidy-inclusive AND hourly-smoothed: captures the
    #      same promos as measured_yield (USD1 estimateApr 0.83% →
    #      apr-history ~2.1%) but WITHOUT the small-position rounding noise
    #      that inflated USDC product 2 to a spurious 4% (`.46`). Available
    #      for any FlexibleSaving / OnChain product, position or not.
    #   2. `measured_apr` — realized APR from `/v5/earn/hourly-yield` on our
    #      open position. Fallback when apr-history has no data. Noise-prone
    #      on tiny stakes (sub-precision hourly credits annualize huge), so
    #      no longer the primary.
    #   3. `estimateApr` — Bybit's quoted base APR. Excludes promos.
    #   4. `missing` — none available → 0% / validator rejects pick.
    base = _parse_percent(p.estimateApr)
    if apr_history is not None and apr_history > 0:
        eff, src = apr_history, "apr_history"
    elif measured_apr is not None and measured_apr > 0:
        eff, src = measured_apr, "measured_yield"
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
    fixed_term_days: int | None = None
    if isinstance(p, OnChainEarnProduct):
        if p.duration == "Fixed" and p.term:
            fixed_term_days = p.term
            notes.append(f"fixed_term_days={p.term}")
        if p.swapCoin:
            notes.append(f"swap_to={p.swapCoin}")
    if p.bonusEvents:
        notes.append(f"bonus_events={len(p.bonusEvents)}")

    # Yield-start delay: OnChain products expose `stakeTime` and
    # `interestCalculationTime` (unix ms). Funds subscribed at stakeTime
    # don't accrue until interestCalculationTime — zero-yield warmup.
    # Flexible products don't carry these → delay stays None.
    yield_start_delay_min: int | None = None
    if isinstance(p, OnChainEarnProduct):
        stake_ms = _parse_unix_ms(p.stakeTime)
        interest_ms = _parse_unix_ms(p.interestCalculationTime)
        if stake_ms is not None and interest_ms is not None:
            delay = (interest_ms - stake_ms) // 60000
            if delay > 0:
                yield_start_delay_min = int(delay)

    # Effective APR net of zero-yield dead-time amortized over the weekly
    # reallocation horizon. Dead-time = subscribe warmup + post-redeem
    # processing (NOT fixed_term — principal earns during the term, it's
    # just illiquid, which the lockup cap handles separately). Surfaced
    # only when there IS dead-time, so the LLM sees net != gross.
    effective_apr_net_holding: Decimal | None = None
    dead_min = (yield_start_delay_min or 0) + (lockup or 0)
    if dead_min > 0 and eff > 0:
        earning = max(0, HOLDING_HORIZON_MINUTES - dead_min)
        effective_apr_net_holding = (
            eff * Decimal(earning) / Decimal(HOLDING_HORIZON_MINUTES)
        )

    # minStakeAmount is denominated in the product's coin. For stables
    # (USDC/USDT/USDE/USD1/etc.) coin units ≈ USD, so it doubles as a
    # USD floor; for non-stables it's an under-estimate (we don't have
    # the spot price here without coupling to the market block — caller
    # treats this as a lower bound and the executor's 180012 rejection
    # is the backstop).
    min_stake: Decimal | None = None
    if p.minStakeAmount is not None:
        try:
            min_stake = Decimal(str(p.minStakeAmount))
        except (InvalidOperation, TypeError):
            min_stake = None

    # Bybit V5 returns `precision` as either a decimal-count string
    # ("4") or a step ("0.0001"). Normalize both to a positive int
    # (decimal places). Unparseable / missing → leave None and let
    # the executor fall back to raw amount.
    stake_precision: int | None = None
    raw_precision = getattr(p, "precision", None)
    if raw_precision is not None:
        s = str(raw_precision).strip()
        if s:
            try:
                n = int(s)
                if n >= 0:
                    stake_precision = n
            except ValueError:
                try:
                    step = Decimal(s)
                    if step > 0 and step <= 1:
                        # 0.0001 → -exponent (4); 1 → 0; 0.01 → 2
                        stake_precision = max(0, -step.as_tuple().exponent)
                except (InvalidOperation, TypeError):
                    pass

    return ProductSummary(
        category=category,
        product_id=p.productId,
        coin=p.coin,
        effective_apr=eff,
        apr_source=src,
        effective_apr_net_holding=effective_apr_net_holding,
        base_apr_string=p.estimateApr,
        redeem_lockup_minutes=lockup,
        fixed_term_days=fixed_term_days,
        yield_start_delay_min=yield_start_delay_min,
        min_subscribe_usd=min_stake,
        stake_precision=stake_precision,
        notes=notes,
    )


def _dual_asset_apr(quote: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    """Extract a representative APR for one DualAssets product from its
    quote. Quote shape is `{category, list: [{currentPrice,
    buyLowPrice: [{selectPrice, apyE8, ...}], sellHighPrice: [...]}]}` —
    offers are nested under `list[0]`. Each offer tier indexes a strike
    (`selectPrice`); APR varies dramatically by strike distance. We
    pick the highest APR offer across both sides as the headline rate —
    that's the strike closest to current price, where the model is also
    taking the most conversion risk. Returns `(None, None)` when the
    quote has no usable offers (expired window, empty server response).
    """
    if not isinstance(quote, dict):
        return None, None
    items = quote.get("list") or []
    if not items or not isinstance(items[0], dict):
        return None, None
    payload = items[0]
    best: Decimal | None = None
    best_raw: str | None = None
    for side in ("buyLowPrice", "sellHighPrice"):
        offers = payload.get(side) or []
        for offer in offers:
            raw = offer.get("apyE8")
            if raw is None:
                continue
            try:
                apy = Decimal(str(raw)) / Decimal("1e8")
            except InvalidOperation:
                continue
            if best is None or apy > best:
                best, best_raw = apy, str(raw)
    return best, best_raw


def _discount_buy_apr(
    quote: dict[str, Any], duration_days: int | None
) -> tuple[Decimal | None, str | None]:
    """Derive APR for a DiscountBuy offer from `currentPrice` vs
    `purchasePrice`. The discount is the implicit yield; annualize by
    duration. Caveat: real yield is conditional on the underlying not
    touching `knockoutPrice` — we surface the nominal APR and leave
    the knockout risk visible in `notes` for the LLM.
    """
    if not isinstance(quote, dict):
        return None, None
    offers = quote.get("offers") or []
    if not offers or duration_days is None or duration_days <= 0:
        return None, None
    offer = offers[0]
    try:
        cur = Decimal(str(offer.get("currentPrice", "0")))
        pur = Decimal(str(offer.get("purchasePrice", "0")))
    except InvalidOperation:
        return None, None
    if pur <= 0 or cur <= pur:
        return None, None
    period_yield = (cur - pur) / pur
    annualized = period_yield * Decimal(365) / Decimal(duration_days)
    raw = f"currentPrice={cur} purchasePrice={pur} duration_days={duration_days}"
    return annualized, raw


_DURATION_RE_DAYS: dict[str, int] = {
    "1d": 1, "2d": 2, "3d": 3, "7d": 7, "14d": 14, "30d": 30, "60d": 60, "90d": 90,
}


def _parse_duration_days(duration: str | None) -> int | None:
    """Bybit advance-Earn products encode duration as `"7d"`, `"14d"`, etc.
    Parse to integer days; return None for unknown strings (`"Flexible"`,
    missing) so callers can fall back."""
    if not duration:
        return None
    if duration in _DURATION_RE_DAYS:
        return _DURATION_RE_DAYS[duration]
    # Tolerate stray spaces and longer suffixes.
    s = duration.strip().lower()
    if s.endswith("d"):
        try:
            return int(s[:-1])
        except ValueError:
            return None
    return None


# `.55` — trailing-momentum proxy for venues with no native APR
# (Alpha Farm, SmartLeverage). Annualized realized return on the
# underlying, scaled by leverage and direction, then haircut, then
# clamped to ±MOMENTUM_APR_CAP. The clamp is load-bearing: without it
# a single hot week gets extrapolated to absurd APRs (a 30% 7d move on
# 5x leverage = +780% raw) which the LLM would naively trust as if it
# were a measured-yield rate. The cap converts "huge directional move"
# into a soft "this is the best signal we have, it's maxed out" — and
# the LLM is told in the prompt that `apr_source="momentum"` means
# low-confidence and to size below half the venue cap.
MOMENTUM_APR_CAP = Decimal("0.50")
ALPHA_MOMENTUM_HAIRCUT = Decimal("0.50")
SMART_LEVERAGE_MOMENTUM_HAIRCUT = Decimal("0.30")


def _momentum_apr(
    period_return: Decimal,
    period_days: Decimal | int,
    *,
    leverage: Decimal | int = 1,
    direction_sign: int = 1,
    haircut: Decimal = Decimal("1"),
) -> Decimal | None:
    """Annualize a realized period return into an APR signal.

    `period_return` is a signed fraction (`+0.05` = +5% over the period).
    `direction_sign` flips for Short positions (so a positive momentum
    on the underlying becomes a negative APR for a Short pick — exactly
    what we want: the LLM avoids shorting an uptrend). `leverage` scales
    the raw signal (a 2x leveraged long doubles both gain and loss).
    `haircut` damps the final number (Alpha 0.5, SmartLeverage 0.3)
    to discourage momentum-chasing.

    Returns `None` for non-positive `period_days` so callers can fall
    back to `apr_source="missing"` rather than crash on bad input.
    """
    days = Decimal(str(period_days))
    if days <= 0:
        return None
    annualized = period_return * (Decimal(365) / days)
    scaled = annualized * Decimal(direction_sign) * Decimal(str(leverage)) * haircut
    if scaled > MOMENTUM_APR_CAP:
        return MOMENTUM_APR_CAP
    if scaled < -MOMENTUM_APR_CAP:
        return -MOMENTUM_APR_CAP
    return scaled


def _kline_period_return(candles: list[dict[str, Any]]) -> Decimal | None:
    """Realized log-ish return across a K-line window.

    Bybit returns candles most-recent-first. We use the oldest
    candle's `open` as the start price and the newest `close` as the
    end price — a simple period return: `(end - start) / start`. Days
    in the window are inferred from row count minus 1 (8 daily candles
    span a 7-day window between candle 0 open and candle 7 close).

    Returns `None` when the window has fewer than 2 candles or when
    price parsing fails — caller drops the momentum signal.
    """
    if not candles or len(candles) < 2:
        return None
    try:
        end_price = Decimal(str(candles[0].get("close", "")))
        start_price = Decimal(str(candles[-1].get("open", "")))
    except (InvalidOperation, TypeError):
        return None
    if start_price <= 0:
        return None
    return (end_price - start_price) / start_price


def _advance_earn_summary(
    p: dict[str, Any], category: str,
    quote: dict[str, Any] | None = None,
    underlying_period_return: Decimal | None = None,
) -> ProductSummary:
    """Normalize one advance-Earn product (DualAssets, DiscountBuy,
    SmartLeverage, DoubleWin) for snapshot surface.

    APR is NOT computed: advance-Earn APR lives in the per-product
    quote endpoint (`/v5/earn/advance/product-extra-info`), not the
    list endpoint. We tag `apr_source="missing"` so the validator
    rejects any non-zero pick weight — the venue is visible to the
    LLM (so it knows the family exists) but un-pickable until a quote
    integration ships in a follow-up task.

    Per-category metadata is appended to `notes` so the prompt can
    surface the relevant fields without each category needing its own
    pydantic model:
    - DualAssets: `pair=BASE/QUOTE`, `settles_in_ms=<delta>`
    - DiscountBuy: `underlying=<coin>`, `duration=<str>`
    - SmartLeverage: `direction=Long|Short`, `leverage=<N>`
    - DoubleWin: `underlying=<coin>`, `range_buffer=±<lower|upper>`
    """
    notes: list[str] = []
    coin = p.get("coin") or p.get("investCoin") or "?"
    duration = p.get("duration")
    if duration:
        notes.append(f"duration={duration}")
    settlement = p.get("settlementTime")
    if settlement:
        notes.append(f"settlement_ms={settlement}")

    # Per-product minimum subscription size. We populate
    # `min_subscribe_usd` for DualAssets / DiscountBuy so the planner +
    # snapshot filter can size-gate picks before they hit Bybit's
    # retCode=180012. Values are denominated in the staking coin (USDT
    # for the products we trade), which ≈ USD for stables.
    min_subscribe: Decimal | None = None

    if category == "DualAssets":
        base = p.get("baseCoin", "?")
        quote_coin = p.get("quoteCoin", "?")
        coin = f"{base}/{quote_coin}"
        min_b = p.get("minPurchaseBaseAmount")
        min_q = p.get("minPurchaseQuoteAmount")
        if min_b is not None and min_q is not None:
            notes.append(f"min_purchase=base{min_b}/quote{min_q}")
        # Planner submits the quote-coin amount, so the USD floor is the
        # quote-side minimum (USDT ≈ USD).
        if min_q is not None:
            try:
                min_subscribe = Decimal(str(min_q))
            except (InvalidOperation, TypeError):
                pass
    elif category == "DiscountBuy":
        underlying = p.get("underlyingAsset")
        if underlying:
            notes.append(f"underlying={underlying}")
        min_pur = p.get("minPurchaseAmount")
        if min_pur is not None:
            notes.append(f"min_purchase={min_pur}")
            try:
                min_subscribe = Decimal(str(min_pur))
            except (InvalidOperation, TypeError):
                pass
    elif category == "SmartLeverage":
        underlying = p.get("underlyingAsset")
        direction = p.get("direction")
        leverage = p.get("leverage")
        if underlying:
            notes.append(f"underlying={underlying}")
        if direction:
            notes.append(f"direction={direction}")
        if leverage is not None:
            notes.append(f"leverage={leverage}")
    elif category == "DoubleWin":
        underlying = p.get("underlyingAsset")
        lb = p.get("lowerPriceBuffer")
        ub = p.get("upperPriceBuffer")
        if underlying:
            notes.append(f"underlying={underlying}")
        if lb is not None and ub is not None:
            notes.append(f"range_buffer=±{lb}/{ub}")

    # Quote-derived APR (.28) for DualAssets + DiscountBuy.
    # SmartLeverage gets a trailing-momentum proxy (`.55`) from the 7d
    # K-line of `underlyingAsset` — annualized × direction × leverage ×
    # haircut, clamped to ±MOMENTUM_APR_CAP. DoubleWin stays `missing`:
    # momentum is the wrong signal for a range-bound payoff (high
    # momentum → breakout → loss), and modeling that needs implied
    # volatility (.56 deferred).
    effective_apr: Decimal = Decimal(0)
    apr_source: str = "missing"
    base_apr_string: str | None = None
    if quote is not None:
        if category == "DualAssets":
            apr, raw = _dual_asset_apr(quote)
            if apr is not None:
                effective_apr, apr_source = apr, "quote_dual_offer"
                base_apr_string = raw
        elif category == "DiscountBuy":
            apr, raw = _discount_buy_apr(quote, _parse_duration_days(duration))
            if apr is not None:
                effective_apr, apr_source = apr, "quote_discount"
                base_apr_string = raw
    if (
        category == "SmartLeverage"
        and underlying_period_return is not None
    ):
        direction_str = str(p.get("direction") or "Long")
        direction_sign = -1 if direction_str.lower() == "short" else 1
        try:
            lev = Decimal(str(p.get("leverage") or "1"))
        except InvalidOperation:
            lev = Decimal(1)
        momentum = _momentum_apr(
            underlying_period_return,
            7,
            leverage=lev,
            direction_sign=direction_sign,
            haircut=SMART_LEVERAGE_MOMENTUM_HAIRCUT,
        )
        if momentum is not None:
            effective_apr = momentum
            apr_source = "momentum"
            base_apr_string = (
                f"underlying_7d={underlying_period_return} "
                f"direction={direction_str} leverage={lev}"
            )

    return ProductSummary(
        category=category,
        product_id=str(p.get("productId", "")),
        coin=coin,
        effective_apr=effective_apr,
        apr_source=apr_source,
        base_apr_string=base_apr_string,
        redeem_lockup_minutes=None,
        min_subscribe_usd=min_subscribe,
        notes=notes,
    )


def _hold_to_earn_summary(p: dict[str, Any]) -> ProductSummary | None:
    """Normalize one Hold-to-Earn product for snapshot surface (`.57`).

    Bybit shape (live-probe 2026-05-29):
        {coinName, apy: "3.75%", status, announcementUrl,
         yields: [{coinName, apy}]}

    We use `coinName` as the `product_id` since the endpoint has no
    distinct id field — each row IS one staked-coin product. The earned
    token from `yields[0].coinName` is appended to `notes` as
    `earn_in=<coin>` so the LLM can see the directional exposure
    explicitly (USD1 → earn WLFI means the realized yield is a WLFI
    position, not USD1 cash flow).

    Returns `None` if `coinName` is missing or the product is `status !=
    "Online"` so we don't surface paused products.
    """
    coin_name = p.get("coinName")
    if not coin_name:
        return None
    if p.get("status") and p.get("status") != "Online":
        return None
    apy_raw = str(p.get("apy") or "")
    effective_apr = _parse_percent(apy_raw)
    apr_source = "hold_to_earn" if effective_apr is not None else "missing"
    notes: list[str] = []
    yields = p.get("yields") or []
    if isinstance(yields, list) and yields:
        first = yields[0]
        if isinstance(first, dict):
            earn_coin = first.get("coinName") or coin_name
            notes.append(f"earn_in={earn_coin}")
            earn_apy = first.get("apy")
            if earn_apy and earn_apy != apy_raw:
                notes.append(f"earn_apy={earn_apy}")
    return ProductSummary(
        category="HoldToEarn",
        product_id=str(coin_name),
        coin=str(coin_name),
        effective_apr=effective_apr if effective_apr is not None else Decimal(0),
        apr_source=apr_source,
        base_apr_string=apy_raw or None,
        redeem_lockup_minutes=None,
        notes=notes,
    )


def _alpha_summary(
    p: dict[str, Any],
    price_info: dict[str, Any] | None = None,
) -> ProductSummary | None:
    """Normalize one Bybit Alpha token entry for snapshot surface.

    `.52` shipped a list-only surface with the wrong endpoint guessed
    from a third-party SDK reference. `.53` corrects it against the
    official docs (via context7):

    - Listing: `POST /v5/alpha/trade/biz-token-list` returns
      `tokenCode` (DEX_<id>), `symbol`, `chainCode`, `tokenAddress`,
      `tokenDecimals`, `riskFlag` (0=clean, 1=warn), `minOrderQuantity`,
      `maxOrderQuantity`, `payTokenCodes`, `tokenTags`.
    - Price feed: `POST /v5/alpha/trade/biz-token-price-list` returns
      `price`, `change24h`, `vol24h`, `marketCap`, `liquidity`, `holders`
      per token — the primary directional/risk signal feed.

    `.55` adds a trailing-momentum APR proxy: `apr_source="momentum"`
    from `change24h × 365 × ALPHA_MOMENTUM_HAIRCUT (0.5)`, clamped to
    ±MOMENTUM_APR_CAP (50%). 24h is the only window the price-list
    endpoint provides without a separate fetch; the clamp + haircut
    prevent a single hot 24h move from masquerading as a real APR.
    Picks remain low-confidence by construction — the prompt instructs
    the LLM to size momentum-sourced picks well below the venue cap and
    justify the directional thesis explicitly. Tokens without a
    `change24h` price-info row keep `apr_source="missing"` (validator
    rejects) — surface-only.

    Returns `None` when `tokenCode` is missing so the row is silently
    dropped — defends against an unexpected `result` shape mutation.
    """
    token_code = p.get("tokenCode")
    if not token_code:
        return None
    symbol = p.get("symbol") or "?"

    notes: list[str] = []
    chain = p.get("chainCode")
    if chain:
        notes.append(f"chain={chain}")
    risk = p.get("riskFlag")
    if risk == 1:
        notes.append("risk_flag=warn")
    min_q = p.get("minOrderQuantity")
    max_q = p.get("maxOrderQuantity")
    if min_q is not None:
        notes.append(f"min_order={min_q}")
    if max_q is not None:
        notes.append(f"max_order={max_q}")
    pay_codes = p.get("payTokenCodes")
    if isinstance(pay_codes, list) and pay_codes:
        notes.append(f"pay_tokens={','.join(str(c) for c in pay_codes)}")

    if price_info:
        price = price_info.get("price")
        if price is not None:
            notes.append(f"price_usd={price}")
        change_24h = price_info.get("change24h")
        if change_24h is not None:
            notes.append(f"change_24h={change_24h}")
        vol_24h = price_info.get("vol24h")
        if vol_24h is not None:
            notes.append(f"vol_24h_usd={vol_24h}")
        liquidity = price_info.get("liquidity")
        if liquidity is not None:
            notes.append(f"liquidity_usd={liquidity}")
        market_cap = price_info.get("marketCap")
        if market_cap is not None:
            notes.append(f"market_cap_usd={market_cap}")
        holders = price_info.get("holders")
        if holders is not None:
            notes.append(f"holders={holders}")

    min_subscribe: Decimal | None
    try:
        min_subscribe = Decimal(str(min_q)) if min_q is not None else None
    except (InvalidOperation, TypeError):
        min_subscribe = None

    effective_apr: Decimal = Decimal(0)
    apr_source: str = "missing"
    base_apr_string: str | None = None
    if price_info is not None:
        change_24h_raw = price_info.get("change24h")
        if change_24h_raw is not None:
            try:
                change_24h = Decimal(str(change_24h_raw))
            except InvalidOperation:
                change_24h = None
            if change_24h is not None:
                momentum = _momentum_apr(
                    change_24h,
                    1,
                    haircut=ALPHA_MOMENTUM_HAIRCUT,
                )
                if momentum is not None:
                    effective_apr = momentum
                    apr_source = "momentum"
                    base_apr_string = f"change_24h={change_24h}"

    return ProductSummary(
        category="AlphaFarm",
        product_id=str(token_code),
        coin=str(symbol),
        effective_apr=effective_apr,
        apr_source=apr_source,
        base_apr_string=base_apr_string,
        redeem_lockup_minutes=None,
        min_subscribe_usd=min_subscribe,
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

    # `minInvestmentQuote` is Bybit's per-product floor in the quote
    # coin (e.g. 50 USDC for BTC/USDC). The diff layer SKIPs any subscribe
    # below this — going through avoids a live `180005` rejection. None
    # when the field is missing from the raw product (older snapshots).
    min_q: Decimal | None
    try:
        raw_min = p.get("minInvestmentQuote")
        min_q = Decimal(str(raw_min)) if raw_min is not None else None
    except (InvalidOperation, TypeError):
        min_q = None

    return ProductSummary(
        category="LiquidityMining",
        product_id=str(p["productId"]),
        coin=f"{p.get('baseCoin', '?')}/{p.get('quoteCoin', '?')}",
        effective_apr=apy,
        apr_source=src,
        base_apr_string=None,
        redeem_lockup_minutes=None,
        min_subscribe_usd=min_q,
        notes=notes,
    )


def _lm_liquidation_distance_pct(pos: dict[str, Any]) -> Decimal | None:
    """Signed fractional distance from `currentPrice` to `liquidationPrice`
    for one LM position. Positive = price has room to drop before
    liquidation; negative = already past (Bybit would have closed).

    Formula: `(currentPrice - liquidationPrice) / currentPrice`. LM
    positions are net-long the base coin (pool buys more base as price
    drops), so liquidation triggers below current spot. Returns None on
    missing / unparseable fields — caller treats as "no signal" rather
    than misleading 0%.
    """
    cur = pos.get("currentPrice")
    liq = pos.get("liquidationPrice")
    if cur is None or liq is None:
        return None
    try:
        cur_d = Decimal(str(cur))
        liq_d = Decimal(str(liq))
    except (InvalidOperation, TypeError):
        return None
    if cur_d <= 0:
        return None
    return (cur_d - liq_d) / cur_d


def _rank_key(s: ProductSummary) -> Decimal:
    """Ranking key: dead-time-adjusted APR when surfaced
    (`effective_apr_net_holding`), else gross `effective_apr`. Ranking by
    net means a high-headline product whose subscribe warmup + redeem
    processing eats most of a weekly hold doesn't out-rank a flatter but
    instantly-liquid one. Products without dead-time fall back to gross,
    so existing FlexibleSaving ordering is unchanged."""
    return (
        s.effective_apr_net_holding
        if s.effective_apr_net_holding is not None
        else s.effective_apr
    )


def _rank(
    products: list[ProductSummary],
    top_k: int = TOP_K,
    must_include: Callable[[ProductSummary], bool] | None = None,
) -> list[ProductSummary]:
    """Sort by dead-time-adjusted APR descending (`_rank_key`), cap at
    top_k. Stable sort — ties preserve Bybit's listing order.

    `must_include`: optional predicate that promotes matching products
    into the result regardless of APR rank. Used to guarantee USDC-set
    stables and LM `max_leverage=1` pairs always appear so the LLM has
    a hedge-free / unleveraged pick available even when alt-coin APRs
    dominate the top of the list.
    """
    by_apr = sorted(products, key=_rank_key, reverse=True)
    if must_include is None:
        return by_apr[:top_k]
    must = [p for p in by_apr if must_include(p)]
    must_ids = {p.product_id for p in must}
    rest = [p for p in by_apr if p.product_id not in must_ids][:top_k]
    merged = must + rest
    return sorted(merged, key=_rank_key, reverse=True)


async def _measure_realized_apr(
    client: BybitClient,
    category: str,
    product_id: str,
    *,
    lookback_hours: int = 24,
) -> Decimal | None:
    """Compute realized APR for an Earn position from Bybit's
    `/v5/earn/hourly-yield` records (`.40` dynamic promo discovery).

    Each row carries `{amount, effectiveStakingAmount, hourlyDate}`:
    `amount` is the yield credited that hour (in the product's coin),
    `effectiveStakingAmount` is the active stake size that hour.
    Per-row APR = `amount / effectiveStakingAmount × 24 × 365`. We
    average across the lookback window so a single noisy hour doesn't
    skew the signal.

    Returns None when no rows are available (fresh position before
    Bybit's first hourly settlement, or position closed before
    lookback). Caller falls back to `estimateApr`."""
    end_ms = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = end_ms - lookback_hours * 3600 * 1000
    try:
        data = await client.get_hourly_yield(
            category=category,
            product_id=product_id,
            start_time=start_ms,
            end_time=end_ms,
            limit=lookback_hours + 4,
        )
    except BybitAPIError:
        return None
    rows = data.get("list") or []
    aprs: list[Decimal] = []
    for r in rows:
        try:
            yld = Decimal(str(r.get("amount", "0")))
            stake = Decimal(str(r.get("effectiveStakingAmount", "0")))
        except InvalidOperation:
            continue
        if stake <= 0:
            continue
        aprs.append(yld / stake * Decimal(24 * 365))
    if not aprs:
        return None
    return sum(aprs, Decimal(0)) / Decimal(len(aprs))


async def _measure_apr_history_mean(
    client: BybitClient,
    category: str,
    product_id: str,
    *,
    days: int = 7,
) -> Decimal | None:
    """Mean effective APR from Bybit's `/v5/earn/apr-history` (`.70`).

    Preferred over `_measure_realized_apr` (our 24h position `hourly-yield`):
    apr-history is **pool-level, subsidy-inclusive, and hourly-smoothed**, so
    it captures the same promos (USD1 estimateApr 0.83% → apr-history ~2.1%,
    peaks 4.8%) WITHOUT the small-position rounding noise that inflated USD1C
    product 2 to a spurious 4% (`bybit-sandbox.46`). Available for any
    FlexibleSaving / OnChain product without an open position.

    Returns the fractional [0,1] mean over the lookback, or None on no data /
    error (caller falls back to `measured_yield` → `estimateApr`)."""
    try:
        data = await client.get_apr_history(
            category=category, product_id=product_id, days=days
        )
    except BybitAPIError:
        return None
    vals: list[Decimal] = []
    for row in data.get("list") or []:
        v = _parse_percent(row.get("apr"))
        if v is not None and v >= 0:
            vals.append(v)
    if not vals:
        return None
    return sum(vals, Decimal(0)) / Decimal(len(vals))


async def _collect_allora(errors: list[str]) -> list[AlloraInference]:
    """Fan-out Allora fetches per (token, window). Returns the non-None
    results, appends one error per failed call into `errors`. No-op when
    `ALLORA_API_KEY` is unset.
    """
    key = os.environ.get("ALLORA_API_KEY", "").strip()
    if not key:
        return []
    client = AlloraClient(key)
    tasks = [
        (token, window, client.fetch_inference(token, window))
        for token in ALLORA_TOKENS
        for window in ALLORA_WINDOWS
    ]
    results = await asyncio.gather(
        *(t for _, _, t in tasks), return_exceptions=True
    )
    out: list[AlloraInference] = []
    for (token, window, _), r in zip(tasks, results):
        if isinstance(r, Exception):
            errors.append(f"allora[{token}/{window}]: {type(r).__name__}: {r}")
            continue
        if r is None:
            errors.append(f"allora[{token}/{window}]: empty")
            continue
        out.append(r)
    return out


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


def _safe_fetch_aave_v3(
    rpc_url: str,
    vault_address: str,
    errors: list[str],
) -> AaveV3UsdcState | None:
    """Synchronous Aave V3 fetch wrapped for the snapshot's thread-pool
    leg (`.37a`). Any RPC or contract error degrades the on-chain block
    to `None` with a warning so the Bybit side of the snapshot survives
    a Mantle outage — same fail-soft contract as `_safe_earn` /
    `_safe_perp_positions`.
    """
    try:
        w3 = make_mantle_client(rpc_url)
        return fetch_aave_v3_usdc_state(w3, vault_address)
    except Exception as e:  # noqa: BLE001
        errors.append(f"on_chain_state[aave_v3_usdc]: {type(e).__name__}: {e}")
        return None


async def _safe_perp_positions(
    coro, errors: list[str], label: str
) -> list[PerpPosition]:
    """Tolerate any Bybit error from `/v5/position/list` and degrade to
    an empty list with a warning. The executor will then plan as if no
    hedges exist — which means it would re-open any already-open shorts,
    so this is fail-loud-but-keep-going: better than crashing the loop,
    worse than knowing the truth, the warning is the operator's signal
    to investigate.
    """
    try:
        return await coro
    except BybitAPIError as e:
        errors.append(f"{label}: retCode={e.ret_code} {e.ret_msg}")
        return []
    except Exception as e:  # noqa: BLE001
        errors.append(f"{label}: {type(e).__name__}: {e}")
        return []


def _is_open_perp(p: PerpPosition) -> bool:
    if p.side not in ("Buy", "Sell"):
        return False
    try:
        return Decimal(p.size) > 0
    except (InvalidOperation, TypeError):
        return False


# Account types Bybit uses for the unified trading account. The long
# form `UnifiedTradingAccount` is what `/v5/asset/asset-overview`
# echoes (.6 live capture); `UNIFIED` is the short form used in the
# request param. Match both so the test mocks and the live snapshot
# both populate `wallet.usdt_available_usd` correctly.
_UNIFIED_ACCOUNT_TYPES: frozenset[str] = frozenset(
    {"UnifiedTradingAccount", "UNIFIED"}
)


def _usdt_in_unified(accounts: list[dict[str, Any]]) -> Decimal:
    """Pull the USDT balance from the UNIFIED account in an asset-overview
    `list`. UNIFIED is where linear-perp margin lives (Spot/Funding/Earn
    USDT doesn't count toward derivatives margin), so this is the number
    the diff layer needs to decide whether to swap USDC → USDT before
    opening a hedge (`.33`). Returns 0 when no UNIFIED account is present
    or the coin list doesn't carry USDT — the diff will then plan a
    full-notional swap, which is the correct fail-safe."""
    for acct in accounts:
        if acct.get("accountType") not in _UNIFIED_ACCOUNT_TYPES:
            continue
        for entry in acct.get("coinDetail") or []:
            if entry.get("coin") != "USDT":
                continue
            raw = entry.get("equity") or entry.get("walletBalance") or "0"
            try:
                return Decimal(str(raw))
            except (InvalidOperation, TypeError):
                return Decimal(0)
    return Decimal(0)


def _coin_across_all_accounts(
    accounts: list[dict[str, Any]], coin: str
) -> Decimal:
    """Sum a coin's equity across every account in the asset-overview
    list (UNIFIED + FUND + CONTRACT + …). Used by the swap planner to
    cap total swap demand at the source coin's full liquid balance —
    the executor's `_transfer_satisfies_swap` pulls from FUND as
    needed, so for budgeting purposes both UTA and FUND count."""
    total = Decimal(0)
    for acct in accounts:
        for entry in acct.get("coinDetail") or []:
            if entry.get("coin") != coin:
                continue
            raw = entry.get("equity") or entry.get("walletBalance") or "0"
            try:
                total += Decimal(str(raw))
            except (InvalidOperation, TypeError):
                continue
    return total


def _all_coins_in_unified(accounts: list[dict[str, Any]]) -> dict[str, Decimal]:
    """Build `{coin: balance}` for every coin in the UNIFIED account.
    Lets the executor diff plan an auto-swap (USDC → pick.coin) ahead of
    a SUBSCRIBE_EARN when the wallet doesn't already carry the pick's
    coin. Balances are in NATIVE coin units (1 USDT = 1.0, 1 BTC = 1.0)
    — caller is responsible for USD-equivalent conversion if needed."""
    out: dict[str, Decimal] = {}
    for acct in accounts:
        if acct.get("accountType") not in _UNIFIED_ACCOUNT_TYPES:
            continue
        for entry in acct.get("coinDetail") or []:
            coin = entry.get("coin")
            if not coin:
                continue
            raw = entry.get("equity") or entry.get("walletBalance") or "0"
            try:
                out[coin] = Decimal(str(raw))
            except (InvalidOperation, TypeError):
                continue
    return out


def _annual_funding(
    per_period: Decimal | None, interval_hours: Decimal | None
) -> Decimal | None:
    """Annualize a per-period funding rate using its cadence interval.

    `annual = per_period × (24/interval_hours) × 365 = per_period
    × HOURS_PER_YEAR / interval_hours`. When `interval_hours` is None
    (Bybit didn't echo `fundingIntervalHour` for the symbol), falls
    back to `DEFAULT_FUNDING_INTERVAL_HOURS` (8h) — same behavior the
    pre-2026-06-03 code had everywhere, made explicit.

    Returns `None` when `per_period` is None or `interval_hours <= 0`.
    """
    if per_period is None:
        return None
    interval = interval_hours if interval_hours is not None else DEFAULT_FUNDING_INTERVAL_HOURS
    if interval <= 0:
        return None
    return per_period * HOURS_PER_YEAR / interval


def _parse_funding_interval(raw: str | None) -> Decimal | None:
    """Bybit's `fundingIntervalHour` is documented as integer-string
    (whole hours only — "8", "4", "1"). Returns Decimal or None on
    missing/malformed so the caller can route through the default."""
    if not raw:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return None
    return value if value > 0 else None


async def _fetch_perp_info(
    client: BybitClient, coin: str, errors: list[str]
) -> tuple[str, PerpInfo | None]:
    """Fetch ticker + orderbook + instrument info for one coin's USDT
    perp pair and synthesize a `PerpInfo`. Returns `(coin, info_or_None)`
    so the caller can build the index. Failures per coin are swallowed
    and logged in `errors`.
    """
    symbol = f"{coin.upper()}USDT"
    try:
        tickers, book, instruments, funding_history = await asyncio.gather(
            client.get_tickers(category="linear", symbol=symbol),
            client.get_orderbook(symbol=symbol, category="linear", limit=50),
            client.get_instruments_info(category="linear", symbol=symbol),
            client.get_funding_history(symbol=symbol, category="linear", limit=21),
            return_exceptions=True,
        )
    except Exception as e:  # noqa: BLE001
        errors.append(f"perp_market[{coin}]: {type(e).__name__}: {e}")
        return coin, None
    # Per-call error handling — funding_history failures degrade to None,
    # critical calls (ticker/orderbook/instruments) collapse the row.
    if isinstance(tickers, BaseException):
        errors.append(f"perp_market[{coin}]: tickers: {type(tickers).__name__}")
        return coin, None
    if isinstance(book, BaseException):
        book = None
    if isinstance(instruments, BaseException):
        instruments = None
    funding_7d_avg: Decimal | None = None
    if isinstance(funding_history, list) and funding_history:
        funding_7d_avg = sum(funding_history, Decimal(0)) / Decimal(len(funding_history))
    elif isinstance(funding_history, BaseException):
        errors.append(
            f"perp_market[{coin}]: funding_history: "
            f"{type(funding_history).__name__}"
        )

    ticker = tickers[0] if tickers else None
    if ticker is None:
        return coin, None

    try:
        funding = (
            Decimal(ticker.fundingRate) if ticker.fundingRate else None
        )
    except InvalidOperation:
        funding = None
    try:
        mark = Decimal(ticker.markPrice) if ticker.markPrice else None
    except InvalidOperation:
        mark = None

    depth_usd: Decimal | None = None
    if book is not None and mark is not None:
        depth_usd = _depth_within_50bps_usd(book, mark)

    min_qty: Decimal | None = None
    max_lev: Decimal | None = None
    if instruments:
        inst = instruments[0]
        lot = inst.lotSizeFilter
        if lot and lot.minOrderQty:
            try:
                min_qty = Decimal(lot.minOrderQty)
            except (InvalidOperation, TypeError):
                min_qty = None
        qty_step_d: Decimal | None = None
        if lot and lot.qtyStep:
            try:
                qty_step_d = Decimal(lot.qtyStep)
            except (InvalidOperation, TypeError):
                qty_step_d = None
        lev = inst.leverageFilter
        if lev and lev.maxLeverage:
            try:
                max_lev = Decimal(lev.maxLeverage)
            except (InvalidOperation, TypeError):
                max_lev = None

    min_notional: Decimal | None = None
    if min_qty is not None and mark is not None:
        min_notional = min_qty * mark

    interval = _parse_funding_interval(ticker.fundingIntervalHour)
    if interval is None:
        errors.append(
            f"perp_market[{coin}]: fundingIntervalHour missing — "
            f"defaulting annualization to {DEFAULT_FUNDING_INTERVAL_HOURS}h"
        )

    return coin, PerpInfo(
        symbol=symbol,
        funding_rate_8h=funding,
        funding_rate_7d_avg=funding_7d_avg,
        funding_interval_hours=interval,
        mark_price=mark,
        orderbook_depth_50bps_usd=depth_usd,
        min_order_qty=min_qty,
        min_notional_usd=min_notional,
        qty_step=qty_step_d,
        max_leverage=max_lev,
    )


def _select_carry_candidate_coins(
    all_linear_tickers: list[LinearTicker],
    exclude_coins: set[str],
    cap: int,
) -> list[str]:
    """Pre-filter the full linear-perp ticker feed down to top-K carry
    candidates by **annualized** single-period `fundingRate` (cheap
    proxy for 7d avg before fan-out). Coins already in `exclude_coins`
    (hedge candidates, held perps) are skipped — they'll get their
    PerpInfo from the same fan-out anyway.

    Annualization reads `fundingIntervalHour` per ticker (Bybit echoes
    "4" / "8" / etc.); missing interval defaults to
    `DEFAULT_FUNDING_INTERVAL_HOURS` (8h). Without this normalization,
    a 4h coin at +0.0001/period (≈ +21.9% annualized) would rank below
    an 8h coin at +0.00015/period (≈ +16.4% annualized) — wrong order.

    Restrictions:
    - USDT-quoted only (`symbol.endswith("USDT")`). Inverse + USDC perps
      are different sizing models — out of scope for `.2`.
    - Base coin NOT in `STABLES` (carry on USDT/USDC funding is a
      different strategy class — defer).
    - Annualized funding > `FUNDING_FLOOR_CARRY_ANNUAL` (positive carry
      after friction floor). Coarse filter — accurate 7d avg comes
      after `_fetch_perp_info`.

    Ranked by annualized funding desc; the LLM eventually sees only
    top-K of the 7d-avg-ranked list in `_build_funding_carry_products`.
    """
    candidates: list[tuple[Decimal, str]] = []
    for t in all_linear_tickers:
        if not t.symbol.endswith("USDT"):
            continue
        coin = t.symbol.removesuffix("USDT").upper()
        if not coin or coin in STABLES or coin in exclude_coins:
            continue
        if not t.fundingRate:
            continue
        try:
            rate = Decimal(t.fundingRate)
        except (InvalidOperation, TypeError):
            continue
        interval = _parse_funding_interval(t.fundingIntervalHour)
        annual = _annual_funding(rate, interval)
        if annual is None or annual <= FUNDING_FLOOR_CARRY_ANNUAL:
            continue
        candidates.append((annual, coin))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [coin for _, coin in candidates[:cap]]


def _build_funding_carry_products(
    perp_market: dict[str, "PerpInfo"],
    total_equity_usd: Decimal,
) -> list[ProductSummary]:
    """Build `products["FundingCarry"]` from the resolved `perp_market`.

    Filters each coin against:
    - coin NOT in `STABLES`
    - `annualized(funding_rate_7d_avg, interval) ≥ FUNDING_FLOOR_CARRY_ANNUAL`
      (7d avg, NOT single-period — noise can flip sign even when
      regime is intact). Annualization respects each coin's
      `funding_interval_hours` so 4h and 8h pairs compare like-for-like.
    - `orderbook_depth_50bps_usd ≥ MIN_CARRY_DEPTH_USD` (can round-trip
      a carry-sized open/close without burning the funding edge)
    - `min_notional_usd ≤ total_equity × MAX_CARRY_NOTIONAL_FRACTION`
      (we can clear the perp's min on a per-pick basis at the venue cap)

    For each surviving coin computes
    `effective_apr = annualized_funding − FUNDING_CARRY_FRICTION_ANNUAL`,
    ranks desc, returns top-K. The interval is also surfaced in `notes`
    so the LLM (and any downstream auditor) can spot 4h pairs without
    re-reading `perp_market`.

    `product_id` is the canonical USDT-perp symbol — the venue's `.3`
    registry treats this as the pickable id.
    """
    if total_equity_usd <= 0:
        return []
    max_pick_usd = total_equity_usd * MAX_CARRY_NOTIONAL_FRACTION
    rows: list[ProductSummary] = []
    for coin, info in perp_market.items():
        if coin in STABLES:
            continue
        if info.funding_rate_7d_avg is None:
            continue
        interval = info.funding_interval_hours or DEFAULT_FUNDING_INTERVAL_HOURS
        gross_annual = _annual_funding(info.funding_rate_7d_avg, interval)
        if gross_annual is None or gross_annual < FUNDING_FLOOR_CARRY_ANNUAL:
            continue
        depth = info.orderbook_depth_50bps_usd
        if depth is None or depth < MIN_CARRY_DEPTH_USD:
            continue
        min_not = info.min_notional_usd
        if min_not is None or min_not > max_pick_usd:
            continue
        effective_apr = gross_annual - FUNDING_CARRY_FRICTION_ANNUAL
        rows.append(
            ProductSummary(
                category=CARRY_CATEGORY,
                product_id=info.symbol,
                coin=coin,
                effective_apr=effective_apr,
                apr_source="funding_carry",
                base_apr_string=str(gross_annual),
                redeem_lockup_minutes=0,
                notes=[
                    f"gross_funding_apr={gross_annual:.4f}",
                    f"funding_rate_7d_avg={info.funding_rate_7d_avg:+.6f}",
                    f"funding_interval_hours={interval}",
                    f"orderbook_depth_50bps_usd={depth:.0f}",
                    f"min_notional_usd={min_not:.2f}",
                    f"max_safe_notional_usd={max_pick_usd:.2f}",
                ],
            )
        )
    rows.sort(key=lambda r: r.effective_apr, reverse=True)
    return rows[:FUNDING_CARRY_TOP_K]


def _hedge_candidate_coins(
    summaries_groups: list[list[ProductSummary]], cap: int
) -> list[str]:
    """Pick the Earn coins that actually need a perp hedge —
    everything non-stable, deduped, capped at `cap`. Walks each group
    in order (OnChain first, then FlexibleSaving) preserving APR-desc
    ranking from `_rank` so high-yield non-stable picks claim the fan-
    out budget first. `.47` follow-up 2026-05-29: extended from
    OnChain-only to cover both auto-hedge categories — non-stable
    FlexibleSaving picks (ID, IO, AGIX, etc.) also need perp data so
    the validator's hedge-feasibility gate has real numbers to check."""
    seen: set[str] = set()
    out: list[str] = []
    for summaries in summaries_groups:
        for p in summaries:
            coin = p.coin.upper()
            if coin in STABLES or coin in seen:
                continue
            seen.add(coin)
            out.append(coin)
            if len(out) >= cap:
                return out
    return out


def _depth_within_50bps_usd(
    book: Any, mark: Decimal
) -> Decimal | None:
    """USD volume on both sides of the book within ±50 bps of `mark`.

    Bybit returns `b` (bids) and `a` (asks) as `[[price, size], ...]`
    decimal-strings. We sum `price × size` for every level whose price
    is within the band — the wider the depth, the safer it is to enter
    or exit a hedge of comparable size without crossing.
    """
    if mark <= 0:
        return None
    band = mark * Decimal("0.005")  # 50 bps
    lo, hi = mark - band, mark + band
    total = Decimal(0)
    for level in list(book.b or []) + list(book.a or []):
        if len(level) < 2:
            continue
        try:
            price = Decimal(str(level[0]))
            size = Decimal(str(level[1]))
        except (InvalidOperation, TypeError):
            continue
        if lo <= price <= hi:
            total += price * size
    return total


async def _quote_advance_top_k(
    client: BybitClient,
    advance_products: dict[str, list[dict[str, Any]]],
    errors: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Fan-out `get_advance_product_quote` for the top-K products in
    each yield-bearing advance-Earn category. Returns a `{(category,
    product_id): quote_dict}` mapping. Categories that aren't yield-
    bearing (SmartLeverage, DoubleWin) are skipped — their picks stay
    `apr_source="missing"` so the validator rejects allocation until a
    follow-up models the conditional payoff.

    Failures per product are swallowed and logged in `errors` so a
    single bad quote doesn't poison the snapshot.
    """
    yield_bearing = ("DualAssets", "DiscountBuy")
    pairs: list[tuple[str, str]] = []
    coros = []
    for cat in yield_bearing:
        items = advance_products.get(cat) or []
        for p in items[:ADVANCE_QUOTE_TOP_K]:
            pid = str(p.get("productId", ""))
            if not pid:
                continue
            pairs.append((cat, pid))
            coros.append(client.get_advance_product_quote(category=cat, product_id=pid))
    if not coros:
        return {}
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for (cat, pid), res in zip(pairs, results):
        if isinstance(res, BaseException):
            errors.append(
                f"advance_quote[{cat}/{pid}]: {type(res).__name__}: {res}"
            )
            continue
        out[(cat, pid)] = res
    return out


async def _positions_advance_top_k(
    client: BybitClient,
    advance_products: dict[str, list[dict[str, Any]]],
    errors: list[str],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Fan-out `get_advance_earn_positions` for the same yield-bearing
    advance-Earn top-K we quote (`.48`). Returns `{(category,
    product_id): [position_dict, ...]}` so the executor diff can SKIP
    re-subscribes when a position is already open.

    Same scope as `_quote_advance_top_k` so the position-feed and the
    pickable surface stay in lockstep — products outside the quote
    window can't be picked anyway, so they don't need dedup coverage.

    Failures per product are swallowed and logged in `errors`; missing
    keys downstream mean "no position info, treat as not held" — best-
    effort fail-soft, mirroring the quote fan-out contract.
    """
    yield_bearing = ("DualAssets", "DiscountBuy")
    pairs: list[tuple[str, str]] = []
    coros = []
    for cat in yield_bearing:
        items = advance_products.get(cat) or []
        for p in items[:ADVANCE_QUOTE_TOP_K]:
            pid = str(p.get("productId", ""))
            if not pid:
                continue
            pairs.append((cat, pid))
            coros.append(
                client.get_advance_earn_positions(category=cat, product_id=pid)
            )
    if not coros:
        return {}
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (cat, pid), res in zip(pairs, results):
        if isinstance(res, BaseException):
            errors.append(
                f"advance_position[{cat}/{pid}]: {type(res).__name__}: {res}"
            )
            continue
        if isinstance(res, list):
            out[(cat, pid)] = res
        # Non-list / non-exception → AsyncMock leak from a test, or a
        # Bybit shape drift; ignore (treat as no position info).
    return out


async def _kline_smart_leverage_underlyings(
    client: BybitClient,
    advance_products: dict[str, list[dict[str, Any]]],
    errors: list[str],
) -> dict[str, Decimal]:
    """Fan out 7d K-line fetch for every unique SmartLeverage underlying
    surfaced in this cycle (`.55`). Returns `{underlying: period_return}`
    — the realized fractional return across the 7-day window. Used by
    `_advance_earn_summary` to derive `apr_source="momentum"` for
    SmartLeverage picks.

    Symbol mapping: SmartLeverage's `underlyingAsset` is the bare coin
    (e.g. "BTC"); the matching linear perp is `{underlying}USDT`.
    Underlyings without a corresponding USDT-perp listing fail K-line
    fetch and the snapshot degrades to `apr_source="missing"` for that
    pick — same fail-soft contract as advance-Earn quote fetch.
    """
    items = advance_products.get("SmartLeverage") or []
    underlyings: list[str] = []
    seen: set[str] = set()
    for p in items[:ADVANCE_QUOTE_TOP_K]:
        u = p.get("underlyingAsset")
        if not u:
            continue
        u_up = str(u).upper()
        if u_up in seen:
            continue
        seen.add(u_up)
        underlyings.append(u_up)
    if not underlyings:
        return {}
    coros = [
        client.get_kline(symbol=f"{u}USDT", interval="D", limit=8)
        for u in underlyings
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: dict[str, Decimal] = {}
    for u, res in zip(underlyings, results):
        if isinstance(res, BaseException):
            errors.append(f"smart_leverage_kline[{u}]: {type(res).__name__}: {res}")
            continue
        period = _kline_period_return(res)
        if period is not None:
            out[u] = period
    return out


async def _safe_advance(
    coro, errors: list[str], label: str, default: Any
) -> Any:
    """Swallow common advance-Earn list errors. Beyond the 10005 Earn
    permission gate, individual advance-Earn categories can return
    180001 (invalid parameter) when the category is disabled on the
    account or geographically restricted. Treat both as "no products
    available this cycle" so the snapshot still builds.
    """
    try:
        return await coro
    except BybitAPIError as e:
        if e.ret_code in (_EARN_PERMISSION_RET_CODE, 180001):
            errors.append(f"{label}: retCode={e.ret_code} {e.ret_msg}")
            return default
        raise


async def _safe_alpha(
    coro, errors: list[str], label: str, default: Any
) -> Any:
    """Swallow any Bybit error from the Alpha listing call (`.52`).

    The `/v5/alpha/*` namespace was only discovered via the third-party
    SDK reference — endpoint availability and per-sub-account permission
    requirements aren't fully mapped yet. Treat any `BybitAPIError` as
    "Alpha unavailable this cycle" and degrade to `default` so the rest
    of the snapshot still builds. Real ret-codes observed in the wild
    will narrow this in `.53`.
    """
    try:
        return await coro
    except BybitAPIError as e:
        errors.append(f"{label}: retCode={e.ret_code} {e.ret_msg}")
        return default


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


async def collect_snapshot(
    client: BybitClient,
    *,
    mantle_rpc_url: str | None = None,
    mantle_vault_address: str | None = None,
) -> Snapshot:
    """Build a full sandbox snapshot. All independent calls run
    concurrently — wall-clock ~= max individual latency (~300ms typ).

    `mantle_rpc_url` + `mantle_vault_address` opt into the on-chain leg
    (`.37a`). When either is omitted, the on-chain block is skipped (a
    warning lands in `errors` and `Snapshot.on_chain_state` stays None),
    so tests + Bybit-only deployments still work without RPC.
    """
    errors: list[str] = []
    captured = datetime.now(UTC)

    # Public + read-scope calls (no permission gate)
    asset_task = asyncio.create_task(client.get_asset_overview())
    flex_task = asyncio.create_task(client.list_earn_products(category="FlexibleSaving"))
    onchain_task = asyncio.create_task(client.list_earn_products(category="OnChain"))
    lm_products_task = asyncio.create_task(client.list_liquidity_mining_products())
    # Advance-Earn families (DualAssets/DiscountBuy/SmartLeverage/DoubleWin).
    # Tolerate per-category failures: advance-Earn often requires a higher
    # API permission scope and individual categories can 10005 while
    # others succeed. `_safe_advance` swallows 10005 + 180001 and returns
    # an empty list so the rest of the snapshot still builds.
    advance_tasks = {
        cat: asyncio.create_task(
            _safe_advance(
                client.list_advance_earn_products(category=cat),
                errors,
                f"advance_earn[{cat}]",
                [],
            )
        )
        for cat in ("DualAssets", "DiscountBuy", "SmartLeverage", "DoubleWin")
    }
    # Bybit Hold-to-Earn (`.57`) — stake-stable-receive-promo-token
    # products (USDE 3.75%, USDTB 3.4%, USD1 → WLFI 7.07%). Read-only
    # surface — venue is `max_weight=0` so picks are rejected; LLM uses
    # the APR as a benchmark only. Failures degrade to empty list.
    hold_to_earn_task = asyncio.create_task(
        _safe_earn(
            client.list_hold_to_earn_products(),
            errors,
            "hold_to_earn[list]",
            [],
        )
    )

    # Bybit Alpha Farm — on-chain DEX tokens purchased with CEX payment
    # tokens via `/v5/alpha/trade/{quote,purchase,redeem}`. Two listing
    # fans-out: biz-token list (universe + per-token metadata like
    # `riskFlag`, `minOrderQuantity`, `payTokenCodes`) and price-list
    # (`change24h`, `liquidity`, `marketCap`, `vol24h`, `holders`). Both
    # are POST endpoints (.53 correction over .52's SDK-guessed GET).
    # Tolerate any Bybit error (per-account Alpha enablement not yet
    # mapped); rest of the snapshot still builds with `AlphaFarm` empty.
    alpha_list_task = asyncio.create_task(
        _safe_alpha(
            client.list_alpha_products(),
            errors,
            "alpha_farm[list]",
            [],
        )
    )
    # Note (`.54` live-probe 2026-05-29): the price-list endpoint requires
    # an explicit `tokenAddressInfo` array — we don't have one until the
    # biz-token listing returns. Price-list fetch happens AFTER
    # `alpha_list_task` resolves; see `_alpha_price_for_listing` below.
    # Pay-token-list moved to execute time (it's per-token, not global).
    # Current alpha holdings (`.54`). Mirrors `lm_positions` shape: the
    # executor diff folds these into the (category, product_id) keyset so
    # tokens we hold but the LLM dropped get redeemed back to USDT.
    alpha_pos_task = asyncio.create_task(
        _safe_alpha(
            client.get_alpha_positions(),
            errors,
            "alpha_farm[positions]",
            {},
        )
    )
    btc_task = asyncio.create_task(
        client.get_tickers(category="linear", symbol="BTCUSDT")
    )
    eth_task = asyncio.create_task(
        client.get_tickers(category="linear", symbol="ETHUSDT")
    )
    # Full linear-perp ticker feed for funding-carry pre-selection
    # (`bybit-strategy-expansion.2`). One call ≈ 1 round-trip, no symbol
    # filter — returns ~500 tickers with `fundingRate` + `markPrice` per
    # row. We use it to coarse-rank carry candidates by single-period
    # 8h funding before fan-out, then `_fetch_perp_info` resolves the
    # accurate 7d avg for the top-K. Failures degrade to empty list —
    # carry products just don't get surfaced this cycle, rest of snapshot
    # builds normally.
    linear_tickers_task = asyncio.create_task(
        client.get_tickers(category="linear")
    )
    peg_task = asyncio.create_task(_fetch_usdc_peg())

    # Earn-permission gated (10005 expected on sandbox).
    # `/v5/earn/position` requires `category` server-side (180001 without
    # it), so fan out per category and tag the rows on the way back —
    # `EarnPosition.category` is documented as gather-layer-set.
    earn_flex_pos_task = asyncio.create_task(
        _safe_earn(
            client.get_earn_positions(category="FlexibleSaving"),
            errors,
            "earn_positions[FlexibleSaving]",
            [],
        )
    )
    earn_onchain_pos_task = asyncio.create_task(
        _safe_earn(
            client.get_earn_positions(category="OnChain"),
            errors,
            "earn_positions[OnChain]",
            [],
        )
    )
    lm_pos_task = asyncio.create_task(
        _safe_earn(
            client.get_liquidity_mining_positions(), errors, "lm_positions", []
        )
    )
    # Linear perp positions (USDT-settled). One request returns every open
    # hedge — coin-specific filtering happens later in the executor diff.
    # Wrapped in a per-task catch so a perp-permission gate or transient
    # 5xx degrades the snapshot to "no known positions" + warning instead
    # of failing the whole capture (consistent with `_safe_earn`).
    perp_pos_task = asyncio.create_task(
        _safe_perp_positions(
            client.get_positions(category="linear", settle_coin="USDT"),
            errors,
            "perp_positions[linear]",
        )
    )
    # Mantle on-chain leg (`.37a`). web3.py is synchronous; wrap in a
    # thread so it joins the fan-out without blocking the event loop.
    # When config is missing, skip with a warning and leave on_chain_state
    # null — the Bybit half of the snapshot is still useful.
    on_chain_task: asyncio.Task[AaveV3UsdcState | None] | None
    if mantle_rpc_url and mantle_vault_address:
        on_chain_task = asyncio.create_task(
            asyncio.to_thread(
                _safe_fetch_aave_v3,
                mantle_rpc_url,
                mantle_vault_address,
                errors,
            )
        )
    else:
        on_chain_task = None
        errors.append(
            "on_chain_state: skipped — MANTLE_RPC_URL and MANTLE_VAULT_ADDRESS "
            "required to fetch Aave V3 USDC pool state"
        )

    asset_overview = await asset_task
    flex_products = await flex_task
    onchain_products = await onchain_task
    lm_products = await lm_products_task
    advance_products = {cat: await task for cat, task in advance_tasks.items()}
    hold_to_earn_products = await hold_to_earn_task
    alpha_products = await alpha_list_task
    alpha_pos_payload = await alpha_pos_task
    alpha_positions_raw = (
        alpha_pos_payload.get("assetList") or []
        if isinstance(alpha_pos_payload, dict)
        else []
    )
    # Price-list fan-out (`.54` live-probe 2026-05-29). Bybit's
    # /v5/alpha/trade/biz-token-price-list requires `tokenAddressInfo: [
    # {chainCode, tokenAddress}, ...]` (max 20 per request). We rank the
    # `alpha_products` listing top-TOP_K, build the {chainCode,
    # tokenAddress} batch from those, and fetch one call's worth of
    # price metadata to feed into `_alpha_summary` for the momentum APR.
    # Failures degrade to empty price-info — venue stays surfaced with
    # `apr_source="missing"`.
    alpha_price_info: list[dict[str, Any]] = []
    if alpha_products:
        top_alpha = alpha_products[:TOP_K]
        token_addr_info = [
            {
                "chainCode": p.get("chainCode", ""),
                "tokenAddress": p.get("tokenAddress", ""),
            }
            for p in top_alpha
            if p.get("chainCode") and p.get("tokenAddress")
        ]
        if token_addr_info:
            try:
                alpha_price_info = await client.list_alpha_price_info(
                    token_address_info=token_addr_info
                )
            except BybitAPIError as e:
                errors.append(
                    f"alpha_farm[price_list]: retCode={e.ret_code} {e.ret_msg}"
                )
    btc_tickers = await btc_task
    eth_tickers = await eth_task
    # Full linear-perp ticker feed for carry pre-selection. On error we
    # log + degrade to empty — fan-out below skips carry coins and
    # `_build_funding_carry_products` returns `[]`.
    try:
        all_linear_tickers = await linear_tickers_task
    except BaseException as e:  # noqa: BLE001
        errors.append(
            f"linear_tickers[all]: {type(e).__name__}: {e}"
        )
        all_linear_tickers = []
    usdc_peg = await peg_task
    earn_flex_positions = await earn_flex_pos_task
    earn_onchain_positions = await earn_onchain_pos_task
    lm_positions = await lm_pos_task
    # Derive per-position liquidation distance so Claude sees risk
    # proximity directly (leveraged LM `.47` follow-up, 2026-05-29).
    # Mutating the raw dicts in place — they're forwarded into the
    # prompt as-is and the executor reads `liquidation_distance_pct`
    # for the de-risk trigger.
    for pos in lm_positions:
        dist = _lm_liquidation_distance_pct(pos)
        if dist is not None:
            pos["liquidation_distance_pct"] = str(dist)
    perp_positions_raw = await perp_pos_task
    aave_state = await on_chain_task if on_chain_task is not None else None
    # Filter out zero-size rows Bybit may echo for recently-traded symbols
    # (`side="None", size="0"`). What we want here is the set of *open*
    # hedges the executor needs to reconcile against.
    perp_positions = [p for p in perp_positions_raw if _is_open_perp(p)]

    # Tag category — Bybit doesn't echo it; downstream filters need it.
    for p in earn_flex_positions:
        if getattr(p, "category", None) is None:
            p.category = "FlexibleSaving"
    for p in earn_onchain_positions:
        if getattr(p, "category", None) is None:
            p.category = "OnChain"
    earn_positions = list(earn_flex_positions) + list(earn_onchain_positions)

    # Measured-yield APR (`.40` dynamic promo discovery). For every
    # active Earn position we hold, fetch `/v5/earn/hourly-yield` and
    # derive realized APR. Captures any UI-only promo subsidy that
    # Bybit's `estimateApr` field doesn't carry (e.g. USD1 "Hold/Earn
    # WLFI" campaign — estimateApr=0.59% but realized ~7%). All fans
    # out concurrently; per-product failures degrade silently to None
    # so the snapshot still builds when Bybit hourly-yield rate-limits.
    measured_apr_pairs = [
        (p.category or "", p.productId)
        for p in earn_positions
        if p.category and p.productId
    ]
    measured_aprs: dict[tuple[str, str], Decimal] = {}
    if measured_apr_pairs:
        results = await asyncio.gather(
            *(_measure_realized_apr(client, cat, pid)
              for cat, pid in measured_apr_pairs),
            return_exceptions=True,
        )
        for (cat, pid), apr in zip(measured_apr_pairs, results):
            if isinstance(apr, BaseException):
                errors.append(f"measured_apr[{cat}/{pid}]: {type(apr).__name__}: {apr}")
                continue
            if apr is not None:
                measured_aprs[(cat, pid)] = apr

    # apr-history APR (`.70`): pool-level, subsidy-inclusive effective APR
    # for EVERY FlexibleSaving / OnChain candidate (no open position needed),
    # the ranker's preferred signal over the noise-prone 24h `measured_yield`
    # (see `_measure_apr_history_mean`). Fans out concurrently; per-product
    # failures degrade to None so the summary falls back to measured_yield /
    # estimateApr and the snapshot still builds under rate-limit.
    aprhist_pairs = [
        ("FlexibleSaving", p.productId) for p in flex_products if p.productId
    ] + [("OnChain", p.productId) for p in onchain_products if p.productId]
    aprhist_aprs: dict[tuple[str, str], Decimal] = {}
    if aprhist_pairs:
        results = await asyncio.gather(
            *(_measure_apr_history_mean(client, cat, pid)
              for cat, pid in aprhist_pairs),
            return_exceptions=True,
        )
        for (cat, pid), apr in zip(aprhist_pairs, results):
            if isinstance(apr, BaseException):
                errors.append(f"apr_history[{cat}/{pid}]: {type(apr).__name__}: {apr}")
                continue
            if apr is not None:
                aprhist_aprs[(cat, pid)] = apr

    # Wallet
    try:
        total_equity = Decimal(str(asset_overview.get("totalEquity", "0") or "0"))
    except InvalidOperation:
        total_equity = Decimal(0)
    accounts = asset_overview.get("list", []) or []
    usdt_available = _usdt_in_unified(accounts)
    unified_coins = _all_coins_in_unified(accounts)
    # USDC across UNIFIED + FUND + others. USDC is our universal swap
    # source coin — the planner needs the full liquid balance to
    # decide how many swaps it can afford without draining mid-cycle.
    liquid_usdc = _coin_across_all_accounts(accounts, "USDC")
    liquid_usdt = _coin_across_all_accounts(accounts, "USDT")

    # Products: normalize + rank with diversification floor.
    stable_floor = lambda s: s.coin in STABLES  # noqa: E731
    lm_unleveraged = lambda s: "max_leverage=1" in s.notes  # noqa: E731
    products = {
        "FlexibleSaving": _rank(
            [
                _flex_or_onchain_summary(
                    p,
                    "FlexibleSaving",
                    measured_apr=measured_aprs.get(("FlexibleSaving", p.productId)),
                    apr_history=aprhist_aprs.get(("FlexibleSaving", p.productId)),
                )
                for p in flex_products
            ],
            must_include=stable_floor,
        ),
        "OnChain": _rank(
            [
                _flex_or_onchain_summary(
                    p,
                    "OnChain",
                    measured_apr=measured_aprs.get(("OnChain", p.productId)),
                    apr_history=aprhist_aprs.get(("OnChain", p.productId)),
                )
                for p in onchain_products
            ],
            must_include=stable_floor,
        ),
        "LiquidityMining": _rank(
            [_lm_summary(p) for p in lm_products],
            must_include=lm_unleveraged,
        ),
    }
    # Advance-Earn families. APR for DualAssets + DiscountBuy comes
    # from the per-product quote endpoint (.28); SmartLeverage and
    # DoubleWin are structured non-yield products (left `missing`).
    # We fan out quote calls only for the yield-bearing categories to
    # keep rate-limit pressure bounded, and only for the first
    # ADVANCE_QUOTE_TOP_K products per category — enough to give the
    # LLM real picks without hitting the quote endpoint 80 times.
    quote_results = await _quote_advance_top_k(
        client, advance_products, errors
    )
    position_results = await _positions_advance_top_k(
        client, advance_products, errors
    )
    smart_leverage_momentum = await _kline_smart_leverage_underlyings(
        client, advance_products, errors
    )
    for cat, raw_items in advance_products.items():
        if not raw_items:
            continue
        products[cat] = [
            _advance_earn_summary(
                p,
                cat,
                quote_results.get((cat, str(p.get("productId", "")))),
                underlying_period_return=(
                    smart_leverage_momentum.get(str(p.get("underlyingAsset", "")).upper())
                    if cat == "SmartLeverage"
                    else None
                ),
            )
            for p in raw_items[:TOP_K]
        ]
    # Hold-to-Earn (`.57`) — surface as a category with normalized APY
    # from the `apy` string. Read-only: venue cap is 0, so picks are
    # rejected by validator; LLM uses these as a benchmark when reasoning
    # about FlexibleSaving USD1 / USDE / USDTB alternatives.
    if hold_to_earn_products:
        h2e_rows = [
            row
            for p in hold_to_earn_products
            if (row := _hold_to_earn_summary(p)) is not None
        ]
        if h2e_rows:
            products["HoldToEarn"] = h2e_rows

    # Alpha Farm (`.52` shipped list-only; `.53` corrects endpoints + adds
    # price-list metadata; `.55` momentum APR; `.54` live-probe fixed the
    # price-list contract — keyed by `(chainCode, tokenAddress)`, not
    # `tokenCode`). Rank by `liquidity` (USD) desc, tiebreak `vol24h`.
    # Tokens without a price-info row sink to the bottom.
    if alpha_products:
        price_info_by_addr = {
            (row.get("chainCode"), row.get("tokenAddress")): row
            for row in alpha_price_info
            if isinstance(row, dict)
            and row.get("chainCode")
            and row.get("tokenAddress")
        }
        def _alpha_info(p: dict[str, Any]) -> dict[str, Any] | None:
            return price_info_by_addr.get(
                (p.get("chainCode"), p.get("tokenAddress"))
            )
        def _alpha_rank_key(p: dict[str, Any]) -> tuple[Decimal, Decimal]:
            info = _alpha_info(p) or {}
            try:
                liq = Decimal(str(info.get("liquidity") or "0"))
            except (InvalidOperation, TypeError):
                liq = Decimal(0)
            try:
                vol = Decimal(str(info.get("vol24h") or "0"))
            except (InvalidOperation, TypeError):
                vol = Decimal(0)
            return (liq, vol)

        sorted_alpha = sorted(alpha_products, key=_alpha_rank_key, reverse=True)
        alpha_rows = [
            row
            for p in sorted_alpha[:TOP_K]
            if (row := _alpha_summary(p, _alpha_info(p))) is not None
        ]
        if alpha_rows:
            products["AlphaFarm"] = alpha_rows
    # Persist the raw quotes for executor consumption (`.35`). Keyed as
    # `"<Category>/<ProductId>"` because pydantic dict fields can't carry
    # tuple keys through model_dump_json round-trips.
    advance_earn_quotes = {
        f"{cat}/{pid}": payload
        for (cat, pid), payload in quote_results.items()
    }
    advance_earn_positions = {
        f"{cat}/{pid}": rows
        for (cat, pid), rows in position_results.items()
    }

    # Aave V3 USDC surface (`.37a`). When the on-chain fetch succeeded,
    # publish the pool's supply APR as a single ProductSummary so the
    # ranker sees CEX vs DeFi rates side-by-side. The venue is enabled
    # but capped at 0 weight until execute lands (`.37b`).
    on_chain_state: OnChainState | None = None
    if aave_state is not None:
        products["AaveV3"] = [
            ProductSummary(
                category="AaveV3",
                product_id="usdc-supply",
                coin="USDC",
                effective_apr=aave_state.supply_apr,
                apr_source="aave_pool",
                base_apr_string=str(aave_state.supply_apr),
                redeem_lockup_minutes=0,
                notes=[
                    f"pool={aave_state.pool_address}",
                    f"block={aave_state.block_number}",
                ],
            )
        ]
        on_chain_state = OnChainState(
            aave_v3_usdc=AaveV3UsdcSnapshot(
                block_number=aave_state.block_number,
                fetched_at=aave_state.fetched_at,
                pool_address=aave_state.pool_address,
                supply_apr=aave_state.supply_apr,
                vault_usdc_usd=micro_to_usd(aave_state.vault_usdc_micro),
                vault_ausdc_usd=micro_to_usd(aave_state.vault_ausdc_micro),
            )
        )

    # Per-coin perp data for non-stable Earn picks (OnChain + Flexible-
    # Saving). Drives the hedging-feasibility rules in the prompt and
    # the validator's auto-hedge gates (`.47` follow-up 2026-05-29).
    # OnChain is walked first so high-yield non-stable OnChain picks
    # always claim the fan-out budget before FlexibleSaving non-stables.
    perp_coins = _hedge_candidate_coins(
        [products["OnChain"], products["FlexibleSaving"]],
        cap=PERP_HEDGE_TOP_K,
    )
    # 2026-06-03 fix: ALSO include coins from currently-held Earn /
    # perp positions even if they fell out of the ranked picks this
    # cycle. Without this, a non-stable position we still hold (e.g.
    # LIT after it dropped from the top-N flex ranking) shows up with
    # no perp_market entry, the executor's `_amount_to_usd` returns 0,
    # and the diff layer silently skips the REDEEM — leaving naked spot
    # exposure when the LLM zeros the pick. Live hit 2026-06-03: LIT
    # perp closed, LIT spot stayed open and unhedged.
    held_perp_coins: list[str] = []
    for p in earn_positions:
        coin = (
            (p.coin if hasattr(p, "coin") else p.get("coin"))
            if (hasattr(p, "coin") or isinstance(p, dict))
            else None
        )
        if not coin:
            continue
        coin = str(coin).upper()
        if coin in STABLES:
            continue
        held_perp_coins.append(coin)
    for p in perp_positions:
        sym = p.symbol if hasattr(p, "symbol") else (p.get("symbol") if isinstance(p, dict) else None)
        if not sym:
            continue
        coin = sym.removesuffix("USDT") if sym.endswith("USDT") else sym
        if coin in STABLES:
            continue
        held_perp_coins.append(coin)
    # Merge: dedupe, preserve perp_coins order first (ranked picks take
    # priority within the per-coin fan-out budget), then append unseen
    # held coins beyond the cap so they ALWAYS get a quote.
    seen_perp: set[str] = set(perp_coins)
    for coin in held_perp_coins:
        if coin not in seen_perp:
            perp_coins.append(coin)
            seen_perp.add(coin)
    # Funding-carry pre-selection (`bybit-strategy-expansion.2`). Carry
    # picks come from coins NOT already in the hedge/held set, surfaced
    # by single-period 8h funding (coarse proxy); accurate 7d avg
    # resolves in `_fetch_perp_info`. Appended AFTER hedge/held coins —
    # both layers share the perp_market dict, no duplicate API calls.
    carry_coins = _select_carry_candidate_coins(
        all_linear_tickers, seen_perp, FUNDING_CARRY_PRESELECT_K
    )
    for coin in carry_coins:
        if coin not in seen_perp:
            perp_coins.append(coin)
            seen_perp.add(coin)
    if perp_coins:
        perp_results = await asyncio.gather(
            *(_fetch_perp_info(client, c, errors) for c in perp_coins)
        )
        perp_market = {
            coin: info for coin, info in perp_results if info is not None
        }
    else:
        perp_market = {}

    # Funding-carry category. Filters + ranks coins from perp_market by
    # friction-adjusted carry APR. Venue `bybit_funding_carry` (`.3`)
    # binds to this category via `snapshot_category="FundingCarry"`;
    # until that venue lands the data sits unused by the LLM (extra
    # tokens, not a correctness bug).
    carry_rows = _build_funding_carry_products(perp_market, total_equity)
    if carry_rows:
        products[CARRY_CATEGORY] = carry_rows

    # Market — take first ticker per symbol (single-symbol query returns one row)
    btc = btc_tickers[0] if btc_tickers else None
    eth = eth_tickers[0] if eth_tickers else None

    # Allora directional forecasts. Best-effort: missing key → empty list,
    # per-call failure → recorded in errors. The LLM treats absence as
    # "no external signal" and falls back to spot+funding reasoning.
    allora_inferences = await _collect_allora(errors)

    market = MarketSnapshot(
        btc_price=_ticker_price(btc),
        btc_24h_change_pct=_ticker_24h(btc),
        btc_funding_rate=_ticker_funding(btc),
        eth_price=_ticker_price(eth),
        eth_24h_change_pct=_ticker_24h(eth),
        eth_funding_rate=_ticker_funding(eth),
        allora_inferences=allora_inferences,
    )

    # Small-vault product filter (2026-06-03). Drop any product whose
    # per-pick minimum exceeds 30% of total vault equity. Rationale:
    # the agent's hard caps already bound `effective_weight = venue.w ×
    # pick.w` to ≤ 30% of book (LM leverage cap, plus implicit Earn
    # diversification). A product with `min_subscribe_usd > 0.30 ×
    # total_equity` cannot mathematically fit any in-cap pick at our
    # current vault size — letting the LLM pick it just produces a
    # downstream Bybit retCode=180012/170140 rejection.
    #
    # Filtering is data-layer (snapshot writes the catalog) rather than
    # prompt-side because (a) prompt rules are trust-based and (b) the
    # filter naturally re-includes products as the vault grows. The
    # validator still enforces the formal cap; this is just keeping
    # un-pickable items out of the LLM's choice set.
    MAX_PICK_WEIGHT_FRACTION = Decimal("0.30")
    capacity_floor = total_equity * MAX_PICK_WEIGHT_FRACTION
    filter_stats: list[str] = []
    for cat, items in list(products.items()):
        if cat in ("HoldToEarn", "AlphaFarm"):
            # HoldToEarn is read-only (venue cap 0); AlphaFarm has its
            # own un-pickable gate via apr_source="momentum". Leave both
            # untouched so the LLM still sees them as benchmark info.
            continue
        kept: list[ProductSummary] = []
        dropped = 0
        for item in items:
            if (
                item.min_subscribe_usd is None
                or item.min_subscribe_usd <= 0
                or item.min_subscribe_usd <= capacity_floor
            ):
                kept.append(item)
            else:
                dropped += 1
        if dropped > 0:
            filter_stats.append(f"{cat}={dropped}")
        products[cat] = kept
    if filter_stats and capacity_floor > 0:
        # snapshot.py runs without a module-level logger; surface the
        # filter outcome on stderr so operators see it in the live loop
        # tail without taking on a logging dependency mid-file.
        print(
            f"snapshot: vault ${float(total_equity):.2f} × "
            f"{float(MAX_PICK_WEIGHT_FRACTION * 100):.0f}% = "
            f"${float(capacity_floor):.2f} cap → filtered out "
            f"under-min products: {', '.join(filter_stats)}",
            file=__import__('sys').stderr,
        )

    # Convert raw position objects (pydantic models or dicts) to dicts
    # for JSON serialization without leaking pydantic types into Snapshot.
    earn_positions_dump = [
        p.model_dump(mode="json") if hasattr(p, "model_dump") else p
        for p in earn_positions
    ]

    return Snapshot(
        captured_at=captured,
        wallet=WalletSnapshot(
            total_equity_usd=total_equity,
            accounts=accounts,
            usdt_available_usd=usdt_available,
            unified_coin_balances=unified_coins,
            liquid_usdc_usd=liquid_usdc,
            liquid_usdt_usd=liquid_usdt,
        ),
        earn_positions=earn_positions_dump,
        lm_positions=lm_positions,
        alpha_positions=alpha_positions_raw,
        products=products,
        market=market,
        perp_market=perp_market,
        perp_positions=perp_positions,
        advance_earn_quotes=advance_earn_quotes,
        advance_earn_positions=advance_earn_positions,
        on_chain_state=on_chain_state,
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
