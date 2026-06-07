"""Venue registry — single source of truth for what the agent may allocate to.

A venue is one whitelisted destination for capital. Adding a new strategy
(another DeFi protocol, another Bybit Earn family, a fresh perp basis
trade) is a one-line entry in `VENUE_REGISTRY` plus matching snapshot
plumbing; the validator and prompt pick up the new venue automatically
via metadata lookups against the registry.

`enabled=False` venues remain in the registry (so the schema accepts
them and downstream code keeps its branches) but the validator forbids
non-zero allocations to them — used for parked / not-yet-wired strategies
like Aave V3 USDC during the pre-mainnet phase.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

VenueId = Literal[
    "cash_usdc",
    "aave_v3_usdc",
    "bybit_flex",
    "bybit_onchain",
    "bybit_lm",
    "bybit_dual_asset",
    "bybit_discount_buy",
    "bybit_smart_leverage",
    "bybit_double_win",
    "bybit_alpha",
    "bybit_hold_to_earn",
    "bybit_funding_carry",
]


class VenueMeta(BaseModel):
    """Per-venue policy. Caps are FRACTIONS OF THE TOTAL BOOK ([0, 1]).

    `requires_picks`: True for ranker-backed venues (Bybit Earn categories)
    where the snapshot lists candidate products and the LLM must split the
    venue across one or more of them; False for single-pool venues (cash,
    Aave V3 USDC) where the venue itself IS the position.

    `snapshot_category`: when `requires_picks=True`, the matching key under
    `snapshot.products` (e.g. `FlexibleSaving`). The validator uses this to
    check picked product_ids exist in the snapshot.

    `notes`: human-readable description, surfaced into the system prompt
    so the LLM understands what each venue actually is. Keep these tight
    and accurate — they're the operator's contract with the model.
    """

    model_config = ConfigDict(extra="ignore")

    venue_id: VenueId
    enabled: bool
    max_weight: float = Field(ge=0, le=1)  # hard cap (fraction of total book)
    min_weight: float = Field(default=0.0, ge=0, le=1)  # hard floor (e.g. cash buffer)
    requires_picks: bool = False
    snapshot_category: str | None = None
    notes: str = ""


VENUE_REGISTRY: dict[str, VenueMeta] = {
    "cash_usdc": VenueMeta(
        venue_id="cash_usdc",
        enabled=True,
        max_weight=1.0,
        min_weight=0.10,
        requires_picks=False,
        notes=(
            "Idle USDC sitting on the venue account. Zero yield. Required "
            "as redemption / re-allocation buffer (floor 10% of book)."
        ),
    ),
    "aave_v3_usdc": VenueMeta(
        venue_id="aave_v3_usdc",
        enabled=True,
        max_weight=0.0,
        min_weight=0.0,
        requires_picks=False,
        snapshot_category="AaveV3",
        notes=(
            "Aave V3 USDC supply on Mantle (single pool, variable APY, "
            "withdrawable any time unless utilization is near 100%). "
            "Read-only surface (`.37a`): pool APR + vault balances are "
            "in snapshot, but `max_weight=0` so the validator rejects "
            "any non-zero pick — execute path lands in `.37b` once the "
            "CapitalManager contract deploys. LLM should treat the APR "
            "as a DeFi benchmark when reasoning about Bybit allocations."
        ),
    ),
    "bybit_flex": VenueMeta(
        venue_id="bybit_flex",
        enabled=True,
        max_weight=0.70,
        requires_picks=True,
        snapshot_category="FlexibleSaving",
        notes=(
            "Bybit Earn FlexibleSaving — instant / near-instant redeem, "
            "variable APR. Pick products from the snapshot's top-20 "
            "ranking by effective APR. Stables AND non-stables are equally "
            "eligible; non-stable picks (ID, IO, AGIX, etc.) get auto-hedged "
            "by the executor just like OnChain non-stables. DO NOT bias "
            "toward stables — operator hard rule 2026-05-29."
        ),
    ),
    "bybit_onchain": VenueMeta(
        venue_id="bybit_onchain",
        enabled=True,
        max_weight=0.70,  # 2026-05-29 bumped from 0.40 after small-vault/2-venue removal — Claude needs room to size USDE + hedged non-stable picks
        requires_picks=True,
        snapshot_category="OnChain",
        notes=(
            "Bybit OnChain Earn — LST-wrappers and short-lockup yield "
            "products. Some carry `swap_to=<coin>` (staking requires a "
            "swap first). Non-USD picks REQUIRE a paired perp hedge "
            "(see `hedges`); unhedged non-stable exposure is forbidden."
        ),
    ),
    "bybit_lm": VenueMeta(
        venue_id="bybit_lm",
        enabled=True,
        max_weight=0.30,
        requires_picks=True,
        snapshot_category="LiquidityMining",
        notes=(
            "Bybit Liquidity Mining — LP pairs (base/quote). At `max_leverage=1` "
            "this is a plain unleveraged LP position: only risk is IL on the "
            "pair plus quote-side price drift. Picks with `max_leverage > 1` "
            "are forbidden (validator rejects). 30% concentration cap is "
            "designed to bound IL exposure, not to discourage use — fill it "
            "up to the cap when calm markets and acceptable pairs exist."
        ),
    ),
    "bybit_dual_asset": VenueMeta(
        venue_id="bybit_dual_asset",
        enabled=True,
        max_weight=0.10,
        requires_picks=True,
        snapshot_category="DualAssets",
        notes=(
            "Bybit Advance-Earn Dual Asset — structured product on a "
            "baseCoin/quoteCoin pair with a strike at settlement time. "
            "Effectively a cash-secured covered call: you stake one coin and "
            "settle in either coin depending on price at expiry. "
            "Products list `baseCoin`, `quoteCoin`, `duration`, `settlementTime`. "
            "**APR for picks comes from the quote endpoint, NOT the list** — "
            "list-only snapshot tags pick `apr_source=missing` and the "
            "validator rejects non-zero weight until quote integration ships. "
            "Surface here lets the agent see the venue exists."
        ),
    ),
    "bybit_discount_buy": VenueMeta(
        venue_id="bybit_discount_buy",
        enabled=True,
        max_weight=0.10,
        requires_picks=True,
        snapshot_category="DiscountBuy",
        notes=(
            "Bybit Advance-Earn Discount Buy — pre-purchase a target coin at "
            "a discount: stake USDT today, receive `underlyingAsset` (BTC/ETH/SOL) "
            "at settlement at a known price below spot. Discount is the implicit "
            "yield. Same caveat as Dual Asset: list-only snapshot does not "
            "compute APR; quote endpoint required to make picks. Surfaced for "
            "visibility ahead of wiring."
        ),
    ),
    "bybit_smart_leverage": VenueMeta(
        venue_id="bybit_smart_leverage",
        enabled=True,
        max_weight=0.10,
        requires_picks=True,
        snapshot_category="SmartLeverage",
        notes=(
            "Bybit Advance-Earn Smart Leverage — leveraged directional spot "
            "(Long or Short) with explicit `leverage`, `duration`, settlement. "
            "Higher risk than Earn or LM; 10% concentration cap by design. "
            "`.55` adds a trailing-momentum APR proxy from the underlying's "
            "7d K-line: `annualized_return × direction × leverage × 0.3`, "
            "clamped to ±50% APR. Picks become real when momentum is "
            "available (`apr_source=\"momentum\"`); else `missing` and "
            "validator rejects. Momentum is a low-confidence signal — size "
            "well below 10% cap and justify the directional view in thesis."
        ),
    ),
    "bybit_double_win": VenueMeta(
        venue_id="bybit_double_win",
        enabled=True,
        max_weight=0.15,
        requires_picks=True,
        snapshot_category="DoubleWin",
        notes=(
            "Bybit Advance-Earn Double Win — range-bound trade on an underlying "
            "(`lowerPriceBuffer` / `upperPriceBuffer`). Stake USDT, win bonus "
            "yield if price stays in range until settlement, else settle at "
            "boundary. Quote endpoint required for APR — picks rejected by "
            "validator until then. Surfaced for visibility."
        ),
    ),
    "bybit_hold_to_earn": VenueMeta(
        venue_id="bybit_hold_to_earn",
        enabled=True,
        max_weight=0.0,
        requires_picks=True,
        snapshot_category="HoldToEarn",
        notes=(
            "Bybit Hold-to-Earn — stake a specific stable (USDE / USDTB / "
            "USD1) and earn yield in either the same coin or a campaign "
            "token (USD1 → WLFI is the canonical promo). Yield is paid as "
            "the `yields[*].coinName` token, NOT in the staked coin — so "
            "for USD1 the realized exposure is WLFI (directional), even "
            "though the principal is dollar-pegged. Read-only surface in "
            "`.57`: subscribe/redeem endpoints not yet wired. `max_weight=0` "
            "so validator rejects any non-zero pick — venue exists for "
            "benchmark visibility (LLM can compare 3.75%/3.4%/7.07% APY "
            "vs FlexibleSaving + LM alternatives in thesis). Note: the "
            "USD1 product's WLFI yield is the same promo `measured_yield` "
            "already captures when USD1 is held via FlexibleSaving — "
            "Hold-to-Earn may be a parallel subscribe path or just a "
            "marketing view of the same position; needs live-probe before "
            "execute wires."
        ),
    ),
    "bybit_funding_carry": VenueMeta(
        venue_id="bybit_funding_carry",
        enabled=True,
        max_weight=0.25,  # `.5` executor wired 2026-06-03
        requires_picks=True,
        snapshot_category="FundingCarry",
        notes=(
            "Bybit delta-neutral funding-rate carry — spot long + perp short "
            "on a coin with positive 7d-avg funding. Yield = funding payment "
            "the short receives (no Earn leg). Distinct from auto-hedge "
            "(which is derived from non-stable bybit_flex/bybit_onchain "
            "picks): carry is a STANDALONE venue, picks chosen here open "
            "their own paired spot+perp position via OPEN_FUNDING_CARRY / "
            "CLOSE_FUNDING_CARRY compound actions. Critical invariant (`.4` "
            "validator): a single coin CANNOT appear in `bybit_funding_carry` "
            "picks AND in non-stable Earn picks at the same time — would "
            "double-open the short and double-lock margin. Snapshot surfaces "
            "candidates in `products.FundingCarry` after friction-adjusted "
            "ranking (annualized funding APR minus ~1.8% round-trip cost; "
            "annualization respects each coin's funding_interval_hours). "
            "Exit floor is annualized at +5.475%/year (vs −10.95%/year for "
            "hedge case): without Earn APR as cushion, slightly-negative "
            "funding flips carry to net cost. Persistent state in "
            "`sandbox/state/funding_carry.json` tracks open positions so "
            "the hedge layer doesn't auto-close carry perp shorts. "
            "See `notes/bybit-funding-carry.md`."
        ),
    ),
    "bybit_alpha": VenueMeta(
        venue_id="bybit_alpha",
        enabled=False,
        max_weight=0.10,
        requires_picks=True,
        snapshot_category="AlphaFarm",
        notes=(
            "Bybit Alpha Farm — purchase on-chain (DEX) alpha tokens with CEX "
            "payment tokens via `/v5/alpha/trade/{quote,purchase,redeem}`. "
            "Directional exposure on an alpha token: no settlement, no accrual "
            "— 'return' is the price change of the alpha token vs the payment "
            "token over the holding period. Snapshot surfaces the top-K tokens "
            "by `liquidity` with rich metadata in `notes` (chain, riskFlag, "
            "min/max_order, price_usd, change_24h, vol_24h_usd, liquidity_usd, "
            "market_cap_usd, holders, pay_tokens). `.55` adds a trailing-"
            "momentum APR proxy from `change24h × 365 × 0.5`, clamped to "
            "±50% APR (`apr_source=\"momentum\"`). 24h is a noisy window — "
            "treat as directional speculation, NOT yield. Cap is 10% but "
            "any single momentum pick should sit well below 5%. Hedging: "
            "alpha tokens almost never have a linear perp pair, so this "
            "venue is declared hedge-exempt as a small directional bucket "
            "once `.54` execute ships."
        ),
    ),
}


def enabled_venues() -> list[VenueMeta]:
    """Venues with `enabled=True`. Used by the snapshot generator and "
    the prompt to decide which venues are live this cycle."""
    return [v for v in VENUE_REGISTRY.values() if v.enabled]


def disabled_venue_ids() -> list[str]:
    """Convenience for the prompt — listed so the LLM knows what NOT to
    allocate to without having to read the full metadata table."""
    return [v.venue_id for v in VENUE_REGISTRY.values() if not v.enabled]


# ─── Derived constants for downstream modules ──────────────────────────────
#
# The validator and executor both partition venues into "basic earn"
# (subscribe → spot+perp paired short via the auto-hedge layer) and
# "funding-carry" (standalone delta-neutral). They used to hold their
# own copies of the venue-id / snapshot-category strings — easy to
# desync when renaming or adding a venue. Single source: VENUE_REGISTRY
# entries below, with these top-level constants derived from them so
# the registry stays the only place where the snapshot_category strings
# need to match the snapshot.py product-category keys.

CARRY_VENUE_ID: VenueId = "bybit_funding_carry"
# Reads through the registry so a rename of `snapshot_category` in the
# carry VenueMeta propagates automatically.
CARRY_CATEGORY: str = VENUE_REGISTRY[CARRY_VENUE_ID].snapshot_category or "FundingCarry"

# Venues whose non-stable picks trigger the auto-hedge layer
# (paired perp short on the underlying). NOT including carry — carry
# picks open their own paired spot+perp via OPEN_FUNDING_CARRY and must
# stay out of the hedge reconciliation (`check_no_double_carry_hedge`
# enforces no coin overlap between the two).
HEDGE_VENUE_IDS: tuple[VenueId, ...] = ("bybit_onchain", "bybit_flex")

# `(venue_id, snapshot_category)` tuples for the auto-hedge venues —
# the validator and executor both iterate over this when sizing /
# accounting for paired hedges. Built from the registry so a category
# rename only requires touching the VenueMeta entry.
HEDGE_VENUES: tuple[tuple[VenueId, str], ...] = tuple(
    (vid, VENUE_REGISTRY[vid].snapshot_category)
    for vid in HEDGE_VENUE_IDS
    if VENUE_REGISTRY[vid].snapshot_category is not None
)  # type: ignore[misc]

# Snapshot-category strings for the auto-hedge venues. The executor
# uses this set when checking whether a pick category should consume
# the basic-earn (FUND / UNIFIED) flow vs an advance-earn flow.
BASIC_EARN_CATEGORIES: frozenset[str] = frozenset(
    cat for _vid, cat in HEDGE_VENUES
)

# Earn categories whose REDEEM does NOT credit the freed coin within a
# cycle (`bybit-sandbox.63`). OnChain stakes/redemptions sit in `Processing`
# for ~4 days, so a same-cycle subscribe funded by that freed coin can't be
# funded (180016) — the validator must not count it as freed and the
# executor must defer the dependent subscribe. FlexibleSaving redeems credit
# in <1min and are NOT here. Add LM / advance-Earn only if they prove to
# delay-settle. Single source of truth for execute.py + validate/rules.py.
SLOW_SETTLE_CATEGORIES: frozenset[str] = frozenset({"OnChain"})
