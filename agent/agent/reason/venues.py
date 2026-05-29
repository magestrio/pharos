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
        max_weight=0.20,
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
        max_weight=0.20,
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
    "bybit_alpha": VenueMeta(
        venue_id="bybit_alpha",
        enabled=True,
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
