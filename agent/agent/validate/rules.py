"""Deterministic validator for the Vault8004 agent's `Decision`.

Separates *prompt errors* (the LLM hallucinated a product_id, picked a
leveraged LM pool, used a disabled venue) from *real market decisions* —
runs as a hard gate before any execution.

Caps come from `agent.reason.venues.VENUE_REGISTRY`, so adding a new
venue is a registry change, not a validator edit. Conditional rules
(peg stress, hedge requirements, LM leverage, missing APR) live as
named functions next to the core hard caps and are individually
auditable against the system prompt.

Returns `(ok, errors)` — empty `errors` ⇔ `ok=True`. The downstream
loop skips the cycle on `ok=False` and logs the errors as the reason;
this is the desired behavior when confidence is low or signals are
missing. Shape-level invariants (sums to 1.0, weights normalized,
picks↔venues consistency) are already enforced by `Decision` at parse
time, so they're not re-checked here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from agent.reason.schema import Decision, VenueAllocation
from agent.reason.venues import (
    BASIC_EARN_CATEGORIES,
    CARRY_CATEGORY,
    CARRY_VENUE_ID,
    HEDGE_VENUES,
    LM_BASE_LEG_FRACTION,
    LM_RESIDUAL_NAKED_MAX,
    SLOW_SETTLE_CATEGORIES,
    VENUE_REGISTRY,
    VenueId,
)
from agent.sandbox.snapshot import (
    DEFAULT_FUNDING_INTERVAL_HOURS,
    FUNDING_CARRY_FRICTION_ANNUAL,
    FUNDING_FLOOR_CARRY_ANNUAL,
    HEDGE_MARGIN_BUFFER,
    STABLES,
    ProductSummary,
    Snapshot,
    _annual_funding,
    _lm_hedge_residual,
)

MIN_CONFIDENCE = 0.4
# Cap on a single non-stable product as fraction of TOTAL book.
# Non-stables carry directional + funding-rate risk on top of
# counterparty risk, but the directional leg is auto-hedged and the
# funding leg is now priced into the ranking (`effective_apr_net_hedge`,
# .66), so concentrating into the single best risk-adjusted net-yield
# pick is the intended behavior — the owner's mandate is to grow the
# book by picking the best, not to spread across mediocre substitutes.
# Raised 0.50 → 0.60 on 2026-06-08 to permit that concentration while
# still keeping any single coin below two-thirds of the book.
MAX_EFFECTIVE_PRODUCT = 0.60
# Stable Earn picks (USD1/USDC/USDT/USDE/...) on FlexibleSaving / OnChain
# get a wider cap. The dominant risk on a stable Earn pick is Bybit's
# Earn-product counterparty risk (custody, smart contract, settlement),
# which is independent of *which* stable is staked. Splitting USD1
# across two products on the same venue doesn't actually reduce that
# risk — it just dilutes APR onto the lower-yielding fallback. So we
# let the LLM concentrate on the highest-APR stable Earn pick — but
# concentration is still bounded. Set to 0.60 on 2026-06-08 (operator
# call): the owner's mandate is to concentrate into the single best
# risk-adjusted pick, not to dilute APR by splitting one stable across
# two products that share the same counterparty risk. This REVERSES the
# 2026-06-07 lowering to 0.40 (which forced scatter + idle cash); 0.60
# still keeps a single Earn product below two-thirds of the book.
MAX_EFFECTIVE_STABLE_PRODUCT = 0.60

# Conditional cap thresholds
PEG_STRESS_BPS = 100
PEG_STRESS_STABLES_FLOOR = 0.50  # cash + bybit_flex must be ≥ this when peg is stressed

# Stables-set treated as "hedge not required" by the OnChain validator.
# Single source of truth lives in `agent.sandbox.snapshot.STABLES` — the
# snapshot's ranker uses it to whitelist USDC-equivalents through the
# top-K filter, and the validator needs the same set so a coin can't be
# classified as "stable" in one layer and "non-stable" in another (which
# would let an un-hedged non-stable pick slip through). Aliased here for
# readability in the rule bodies.
_STABLE_COINS = STABLES

Check = tuple[bool, str | None]


# ─── Hard caps from registry ───────────────────────────────────────────────


def check_disabled_venues(d: Decision) -> Check:
    """No non-zero allocation to a venue with `enabled=False`."""
    violations: list[str] = []
    for v in d.venues:
        meta = VENUE_REGISTRY[v.venue_id]
        if not meta.enabled and v.weight > 0:
            violations.append(f"{v.venue_id}={v.weight:.2%}")
    if violations:
        return False, (
            f"non-zero allocation to disabled venue(s): {', '.join(violations)}"
        )
    return True, None


def check_venue_caps(d: Decision) -> Check:
    """Per-venue `max_weight` cap from `VENUE_REGISTRY`."""
    violations: list[str] = []
    for v in d.venues:
        meta = VENUE_REGISTRY[v.venue_id]
        if v.weight > meta.max_weight + 1e-9:
            violations.append(
                f"{v.venue_id}={v.weight:.2%}>{meta.max_weight:.0%}"
            )
    if violations:
        return False, f"venue caps exceeded: {', '.join(violations)}"
    return True, None


def check_venue_floors(d: Decision) -> Check:
    """Per-venue `min_weight` floor (e.g. cash buffer)."""
    violations: list[str] = []
    venue_weight = {v.venue_id: v.weight for v in d.venues}
    for meta in VENUE_REGISTRY.values():
        if meta.min_weight <= 0:
            continue
        actual = venue_weight.get(meta.venue_id, 0.0)
        if actual + 1e-9 < meta.min_weight:
            violations.append(
                f"{meta.venue_id}={actual:.2%}<{meta.min_weight:.0%}"
            )
    if violations:
        return False, f"venue floors not met: {', '.join(violations)}"
    return True, None


def check_picks_required(d: Decision) -> Check:
    """Venues with `requires_picks=True` MUST have picks when weight > 0.
    Venues with `requires_picks=False` MUST NOT have picks (cash / Aave
    are single-pool; nowhere to allocate inside)."""
    violations: list[str] = []
    for v in d.venues:
        meta = VENUE_REGISTRY[v.venue_id]
        if meta.requires_picks and v.weight > 0 and not v.picks:
            violations.append(f"{v.venue_id} non-zero but no picks")
        if not meta.requires_picks and v.picks:
            violations.append(f"{v.venue_id} carries picks but is single-pool")
    if violations:
        return False, "; ".join(violations)
    return True, None


def check_effective_pick_cap(d: Decision, snapshot: Snapshot) -> Check:
    """No single product holds more than its category-appropriate cap of
    the total book. Effective share = venue.weight × pick.weight.

    Two caps: stable Earn picks (FlexibleSaving / OnChain whose coin is
    a stable per `_STABLE_COINS`) get `MAX_EFFECTIVE_STABLE_PRODUCT`,
    everything else gets `MAX_EFFECTIVE_PRODUCT`. See module-level
    constants for the rationale. Snapshot is needed to look up the coin
    per product_id.
    """
    idx = _snapshot_index(snapshot)
    violations: list[str] = []
    for v in d.venues:
        meta = VENUE_REGISTRY[v.venue_id]
        cat = getattr(meta, "snapshot_category", None)
        cat_idx = idx.get(cat, {}) if cat else {}
        for p in v.picks:
            eff = v.weight * p.weight
            summary = cat_idx.get(p.product_id)
            coin = (summary.coin.upper() if summary else "").upper()
            # Stables-only relaxation applies to basic Earn categories
            # (FlexibleSaving / OnChain). Advance-Earn, LM, Alpha get
            # the strict cap regardless — their risk profile is not
            # comparable to a flat stable Earn product.
            stable_eligible = (
                cat in BASIC_EARN_CATEGORIES
                and coin in _STABLE_COINS
            )
            cap = (
                MAX_EFFECTIVE_STABLE_PRODUCT
                if stable_eligible
                else MAX_EFFECTIVE_PRODUCT
            )
            if eff > cap + 1e-9:
                violations.append(
                    f"{v.venue_id}/{p.product_id}({coin or '?'})"
                    f"={eff:.2%}>{cap:.0%}"
                )
    if violations:
        return False, (
            f"effective product positions exceed cap (stable picks "
            f"{MAX_EFFECTIVE_STABLE_PRODUCT:.0%}, others "
            f"{MAX_EFFECTIVE_PRODUCT:.0%}): {', '.join(violations)}"
        )
    return True, None


def check_confidence(d: Decision) -> Check:
    if d.confidence < MIN_CONFIDENCE:
        return False, (
            f"confidence {d.confidence:.2f} below minimum {MIN_CONFIDENCE:.1f}"
        )
    return True, None


def check_risk_flags(d: Decision) -> Check:
    if d.risk_flags:
        return False, f"risk_flags present: {d.risk_flags}"
    return True, None


# ─── Conditional caps (snapshot-aware) ─────────────────────────────────────


def check_peg_stress(d: Decision, snapshot: Snapshot) -> Check:
    """Under peg stress (or missing peg data) majority must sit in
    fast-redeem stables: `cash_usdc + bybit_flex >= 0.50`. Missing peg
    data is treated as triggered — fail-closed."""
    dev = snapshot.usdc_peg.deviation_bps
    triggered = dev is None or abs(dev) > PEG_STRESS_BPS
    if not triggered:
        return True, None
    cash = _weight(d, "cash_usdc")
    flex = _weight(d, "bybit_flex")
    stables = cash + flex
    if stables + 1e-9 < PEG_STRESS_STABLES_FLOOR:
        dev_str = "unavailable" if dev is None else f"{dev:.0f}bps"
        return False, (
            f"peg deviation={dev_str} requires cash_usdc + bybit_flex "
            f">= {PEG_STRESS_STABLES_FLOOR:.0%} (got {stables:.2%})"
        )
    return True, None


def _snapshot_index(
    snapshot: Snapshot,
) -> dict[str, dict[str, ProductSummary]]:
    return {
        category: {p.product_id: p for p in products}
        for category, products in snapshot.products.items()
    }


def check_product_ids_in_snapshot(d: Decision, snapshot: Snapshot) -> Check:
    """Every picked product_id MUST appear in the snapshot's matching
    category. Pulling an id from `earn_positions` (current holdings) is
    the canonical hallucination — caught here."""
    idx = _snapshot_index(snapshot)
    missing: list[str] = []
    for v in d.venues:
        meta = VENUE_REGISTRY[v.venue_id]
        if not meta.requires_picks or not meta.snapshot_category:
            continue
        category_idx = idx.get(meta.snapshot_category, {})
        for pick in v.picks:
            if pick.product_id not in category_idx:
                missing.append(f"{v.venue_id}/{pick.product_id}")
    if missing:
        return False, (
            f"product_id(s) not found in snapshot: {', '.join(missing)}"
        )
    return True, None


# Maximum effective lockup the vault tolerates. Picks whose snapshot
# `redeem_lockup_minutes` exceeds this get rejected — the vault
# reallocates on a weekly horizon and can't price the opportunity-cost
# of being stuck longer. Mirrors the prompt's `# Hard caps` rule but
# enforced at code level since the LLM occasionally ignores the soft
# guidance (e.g. ATOM OnChain 36000-min lockup picked despite rule).
MAX_LOCKUP_MINUTES: int = 7 * 24 * 60  # 10080 = 7 days


def check_lockup_cap(d: Decision, snapshot: Snapshot) -> Check:
    """Reject any pick whose effective lockup exceeds `MAX_LOCKUP_MINUTES`.

    Two distinct lockup sources, both capped at the 7-day reallocation
    horizon:
    - `redeem_lockup_minutes` — post-redeem processing window
      (`redeemProcessingMinute`). None ⇒ instant redeem (e.g.
      FlexibleSaving), allowed through.
    - `fixed_term_days` — OnChain Fixed-term principal lock until
      maturity (`OnChainEarnProduct.term`). The docstring on
      `OnChainEarnProduct` requires the validator to reject staking that
      can't unwind before the next rebalance; this enforces it. None ⇒
      Flexible / instant, allowed through.
    """
    idx = _snapshot_index(snapshot)
    violations: list[str] = []
    for v in d.venues:
        meta = VENUE_REGISTRY[v.venue_id]
        if not meta.requires_picks or not meta.snapshot_category:
            continue
        category_idx = idx.get(meta.snapshot_category, {})
        for pick in v.picks:
            summary = category_idx.get(pick.product_id)
            if summary is None:
                continue
            lockup = summary.redeem_lockup_minutes
            if lockup is not None and lockup > MAX_LOCKUP_MINUTES:
                days = lockup // 1440
                violations.append(
                    f"{v.venue_id}/{pick.product_id} "
                    f"({lockup} min ≈ {days}d, cap 7d)"
                )
            term_days = summary.fixed_term_days
            if term_days is not None and term_days * 1440 > MAX_LOCKUP_MINUTES:
                violations.append(
                    f"{v.venue_id}/{pick.product_id} "
                    f"(fixed-term {term_days}d, cap 7d)"
                )
    if violations:
        return False, (
            f"picks exceed {MAX_LOCKUP_MINUTES}-minute (7-day) lockup cap: "
            f"{', '.join(violations)}"
        )
    return True, None


def check_no_missing_apr_source(d: Decision, snapshot: Snapshot) -> Check:
    """A pick whose snapshot entry has `apr_source == "missing"` cannot
    be priced (yield = 0) — allowing it through would mean ranking on a
    fake APR. The prompt tells the LLM to give such products weight 0;
    the validator enforces it."""
    idx = _snapshot_index(snapshot)
    violations: list[str] = []
    for v in d.venues:
        meta = VENUE_REGISTRY[v.venue_id]
        if not meta.requires_picks or not meta.snapshot_category:
            continue
        category_idx = idx.get(meta.snapshot_category, {})
        for pick in v.picks:
            summary = category_idx.get(pick.product_id)
            if summary is not None and summary.apr_source == "missing":
                violations.append(f"{v.venue_id}/{pick.product_id}")
    if violations:
        return False, (
            f"picks with apr_source=missing must have weight 0: "
            f"{', '.join(violations)}"
        )
    return True, None


# Leveraged LM size budget. `position_usd ≤ vault × LM_LEVERAGE_CAP_FACTOR /
# max(1, leverage)`. At leverage=1 the cap is 30% (same as bybit_lm venue
# max_weight); at 5x → 6%; at 10x → 3%. Sized so worst-case liquidation
# loses ≤ ~3% of book even on max-leverage products. Operator agreed
# 2026-05-29 (`.47` follow-up) — was a hard `leverage ≤ 1` ban prior.
LM_LEVERAGE_CAP_FACTOR: float = 0.30


def check_lm_leverage_size_cap(d: Decision, snapshot: Snapshot) -> Check:
    """LM picks scale their max position size DOWN with leverage so a
    worst-case liquidation can't blow the book. Formula:

        effective_position_usd ≤ vault × LM_LEVERAGE_CAP_FACTOR / leverage

    Where `effective_position_usd = vault × venue.weight × pick.weight`
    (same sizing model as `check_effective_pick_cap`). Picks without a
    `max_leverage` note in the snapshot are treated as leverage=1.
    Liquidation handling at runtime is the executor's job (partial
    REDEEM_LM + liquidation_distance signal in snapshot)."""
    lm = d.venue("bybit_lm")
    if lm is None or not lm.picks:
        return True, None
    lm_idx = _snapshot_index(snapshot).get("LiquidityMining", {})
    violations: list[str] = []
    for pick in lm.picks:
        summary = lm_idx.get(pick.product_id)
        if summary is None:
            continue  # already caught by check_product_ids_in_snapshot
        lev = _extract_max_leverage(summary) or 1
        effective_weight = lm.weight * pick.weight
        max_weight = LM_LEVERAGE_CAP_FACTOR / max(1, lev)
        # Tolerance: 1e-9 absolute catches float-multiplication noise on
        # small numbers, 0.5% relative catches Claude rounding to nice
        # decimals (e.g. lm=0.10, pick=0.43 → 4.3% vs cap 0.30/7=4.286%;
        # 0.014% over is round-down headroom, not real risk). A 0.5%
        # over on a 30% LM allocation is ~0.15% of book — well below
        # any meaningful liquidation tail.
        tolerance = max(1e-9, max_weight * 0.005)
        if effective_weight > max_weight + tolerance:
            violations.append(
                f"{pick.product_id}(leverage={lev}, size="
                f"{effective_weight:.1%} > cap {max_weight:.1%})"
            )
    if violations:
        return False, (
            f"LM picks exceed leverage-scaled size cap "
            f"({LM_LEVERAGE_CAP_FACTOR:.0%}/leverage): {', '.join(violations)}"
        )
    return True, None


def check_lm_leverage_forbidden(d: Decision, snapshot: Snapshot) -> Check:
    """LM picks must be UNLEVERAGED (`max_leverage == 1`). A leveraged LP on a
    volatile token is speculative directional risk with a liquidation tail —
    the opposite of the owner's "controlled risk" mandate (`bybit-sandbox.66`;
    a 5x TIA/USDT LP shipped live and triggered this). Picks with no
    `max_leverage` note are treated as 1 (allowed). This supersedes the older
    size-scaling tolerance of `check_lm_leverage_size_cap` (which still runs as
    a backstop bounding the unleveraged pick to the 30% venue cap). The
    snapshot also drops leveraged LM rows from the choice set, so a compliant
    LLM never sees them; this gate is the deterministic enforcement."""
    lm = d.venue("bybit_lm")
    if lm is None or not lm.picks:
        return True, None
    lm_idx = _snapshot_index(snapshot).get("LiquidityMining", {})
    violations: list[str] = []
    for pick in lm.picks:
        summary = lm_idx.get(pick.product_id)
        if summary is None:
            continue  # already caught by check_product_ids_in_snapshot
        lev = _extract_max_leverage(summary) or 1
        if lev > 1:
            violations.append(f"{pick.product_id}(max_leverage={lev})")
    if violations:
        return False, (
            f"LM picks must be unleveraged (max_leverage=1); leveraged LP is "
            f"speculative directional risk: {', '.join(violations)}"
        )
    return True, None


def _extract_max_leverage(summary: ProductSummary) -> int | None:
    for note in summary.notes:
        if note.startswith("max_leverage="):
            try:
                return int(note.split("=", 1)[1])
            except (ValueError, IndexError):
                return None
    return None


# Earn venues whose non-stable picks must clear the auto-hedge
# feasibility gate. Imported from `agent.reason.venues.HEDGE_VENUES` —
# single source of truth so a rename of `snapshot_category` in
# VENUE_REGISTRY propagates automatically and the validator can't drift
# from the executor on which venues participate in auto-hedge.
_AUTO_HEDGE_VENUES = HEDGE_VENUES


def check_hedges_for_non_usd_picks(d: Decision, snapshot: Snapshot) -> Check:
    """Auto-hedge era (2026-05-29): hedges are derived from non-stable
    Earn picks (OnChain + FlexibleSaving) at execute time. This rule
    is the feasibility gate — for each non-stable Earn pick, verify
    the perp pair exists in `snapshot.perp_market` and that the resulting
    hedge clears `min_notional_usd`. Picks whose hedge wouldn't fit get
    rejected here (would otherwise SKIP at executor and leave the
    underlying unhedged)."""
    perp_market = getattr(snapshot, "perp_market", None) or {}
    # Decimal throughout — matches executor's sizing math (execute.py
    # `_funding_carry_targets` etc.) so a pick that passes here can't be
    # rejected downstream by a cents-level rounding gap.
    total_book = snapshot.wallet.total_equity_usd
    bad: list[str] = []
    for venue_id, category in _AUTO_HEDGE_VENUES:
        venue = d.venue(venue_id)  # type: ignore[arg-type]
        if venue is None or not venue.picks:
            continue
        idx = _snapshot_index(snapshot).get(category, {})
        for pick in venue.picks:
            summary = idx.get(pick.product_id)
            if summary is None:
                continue
            coin = summary.coin.upper()
            if coin in _STABLE_COINS:
                continue
            info = perp_market.get(coin) or perp_market.get(coin.lower())
            if info is None:
                bad.append(f"{venue_id}/{pick.product_id}({coin}): no perp_market entry")
                continue
            if info.min_notional_usd is None:
                bad.append(f"{venue_id}/{pick.product_id}({coin}): min_notional unknown")
                continue
            pick_usd = (
                total_book * Decimal(str(venue.weight)) * Decimal(str(pick.weight))
            )
            if pick_usd < info.min_notional_usd:
                bad.append(
                    f"{venue_id}/{pick.product_id}({coin}): pick "
                    f"${pick_usd:.2f} below perp min_notional "
                    f"${info.min_notional_usd:.2f} — hedge can't be placed"
                )

    # LM base legs are auto-hedged too (executor `_lm_hedge_targets`): a
    # single-sided LM deposit rebalances 50/50, so half the pick is a
    # directional long on the BASE coin (`BASE/QUOTE` pair) hedged by a
    # perp short on that half. Feasibility-gate it like the Earn picks —
    # reject when the base has no perp or the half-notional can't clear
    # min_notional, otherwise the executor would open an un-hedgeable
    # naked LM base leg.
    lm_venue = d.venue("bybit_lm")
    if lm_venue is not None and lm_venue.picks:
        lm_idx = _snapshot_index(snapshot).get("LiquidityMining", {})
        for pick in lm_venue.picks:
            summary = lm_idx.get(pick.product_id)
            if summary is None:
                continue
            parts = summary.coin.split("/", 1)
            if len(parts) != 2:
                continue
            base = parts[0].upper()
            if not base or base in _STABLE_COINS:
                continue
            info = perp_market.get(base) or perp_market.get(base.lower())
            if info is None:
                bad.append(f"bybit_lm/{pick.product_id}({base}): no perp_market entry")
                continue
            if info.min_notional_usd is None:
                bad.append(f"bybit_lm/{pick.product_id}({base}): min_notional unknown")
                continue
            base_leg_usd = (
                total_book
                * Decimal(str(lm_venue.weight))
                * Decimal(str(pick.weight))
                * LM_BASE_LEG_FRACTION
            )
            if base_leg_usd < info.min_notional_usd:
                bad.append(
                    f"bybit_lm/{pick.product_id}({base}): base leg "
                    f"${base_leg_usd:.2f} below perp min_notional "
                    f"${info.min_notional_usd:.2f} — hedge can't be placed"
                )

    if bad:
        return False, "non-USD Earn picks not hedgeable: " + " | ".join(bad)
    return True, None


# Annualized 7d-avg funding floor for hedged non-stable Earn picks.
# Below this rate the perp-short hedge becomes net cost over a typical
# hold and erodes Earn APR. Operator hardcap pattern (CLAUDE.md):
# "7-day avg funding < 0 → mandatory exit"; we tighten to roughly
# −11%/year instead of strict zero so single-period noise doesn't yank
# good picks. Annualized form (renamed 2026-06-03 from the per-period
# `FUNDING_FLOOR_8H` constant): preserves the same intent for 8h coins
# (−0.0001/8h × 1095 ≈ −0.1095) AND correctly evaluates 4h coins —
# whose per-period rate is naturally ~½ the 8h equivalent at the same
# annualized yield.
FUNDING_FLOOR_HEDGE_ANNUAL = -0.1095

# Net-of-hedge yield floor (2026-06-08). A hedged non-stable Earn pick is
# delta-neutral; its realizable yield is `gross_earn_apr + annualized_funding
# − friction` (the `effective_apr_net_hedge` the snapshot ranker computes).
# The OLD floor gated raw funding alone (≥ -10.95%/yr), which blanket-rejected
# the entire high-APR hedgeable universe — a 101% Earn coin at -37%/yr funding
# nets to +64% delta-neutral (ME, live) but failed the raw-funding gate, so the
# agent fell back to ~3% stables (the whole vault's yield problem). We now gate
# on the NET: a hedge must be net-PROFITABLE after funding + friction. Negative
# (bleeding) hedges still rejected; net-positive high-yield hedges pass. The
# ranker surfaces the highest net; the prompt probe-sizes unconfirmed
# (estimate_apr) highs and scales on measured_yield = max yield, controlled risk.
NET_HEDGE_YIELD_FLOOR = 0.0


def net_hedge_yield(summary: Any, perp_info: Any) -> tuple[float | None, bool]:
    """Realizable net-of-hedge ANNUAL yield for a hedged non-stable Earn pick:
    `gross_earn_apr + annualized_7d_funding − friction` — the
    `effective_apr_net_hedge` the snapshot ranker precomputes.

    Returns `(net, interval_broken)`:
      - `net` is None when there's no funding signal at all (caller: "no
        signal → pass");
      - `interval_broken` is True when `funding_interval_hours` can't be
        annualized (caller: reject — a sizing clamp can't fix bad data).

    SINGLE SOURCE OF TRUTH for the net-yield gate so the validator
    (`check_funding_rate_floor`) and the loop's deterministic sub-floor clamp
    (`_pick_is_subfloor_nonstable`) can't drift. They DID drift (2026-06-08):
    the clamp still gated RAW funding (≥ -10.95%/yr) and dumped a +28%-net ME
    pick to cash while the net-aware validator would have passed it — leaving
    the vault parked in ~3% stables."""
    net = summary.effective_apr_net_hedge
    if net is not None:
        return float(net), False
    if perp_info is None or perp_info.funding_rate_7d_avg is None:
        return None, False
    interval = perp_info.funding_interval_hours or DEFAULT_FUNDING_INTERVAL_HOURS
    annual = _annual_funding(perp_info.funding_rate_7d_avg, interval)
    if annual is None:
        return None, True
    gross = summary.effective_apr_gross or summary.effective_apr
    return (
        float(gross) + float(annual) - float(FUNDING_CARRY_FRICTION_ANNUAL),
        False,
    )


def check_lm_residual_naked_exposure(d: Decision, snapshot: Snapshot) -> Check:
    """Reject a NEW or GROWN LM pick whose post-floor-round naked base-coin
    residual would exceed `LM_RESIDUAL_NAKED_MAX` of book.

    The LM base leg auto-hedges only in WHOLE perp lots, so a pick whose base
    leg (pick_usd × ½) isn't ≈ a clean multiple of one lot leaves a naked
    base-coin long stuck INSIDE the LP that no hedge can reach — only redeeming
    the LP closes it (lm-residual epic). Gating NEW exposure here stops the
    agent OPENING an un-cleanly-hedgeable LP and teaches it (via the reject
    message → next-cycle summary) to size to a clean lot multiple or pick a
    finer-lot pair.

    HELD positions are EXEMPT (net-new < `_MIN_ACTION_USDC`): their residual is
    handled by the snapshot/prompt surface + the loop's de-risk redeem sweep,
    NOT a cycle reject that would amount to force-redeeming a hold. Net-new is
    scoped exactly like `check_funding_rate_floor`'s LM block; missing/unpriced
    perp data ⇒ exempt (`check_hedges_for_non_usd_picks` owns un-hedgeable
    picks, and an unpriced perp yields a 0 residual → pass). Residual math
    reuses `snapshot._lm_hedge_residual` so the gate, the snapshot surface, and
    the executor redeem sweep can't drift."""
    lm_venue = d.venue("bybit_lm")
    if lm_venue is None or not lm_venue.picks:
        return True, None
    total_book = float(snapshot.wallet.total_equity_usd)
    if total_book <= 0:
        return True, None
    perp_market = getattr(snapshot, "perp_market", None) or {}
    lm_idx = _snapshot_index(snapshot).get("LiquidityMining", {})
    held_lm = _held_lm_usd_by_product(snapshot)
    floor_usd = float(LM_RESIDUAL_NAKED_MAX) * total_book
    bad: list[str] = []
    for pick in lm_venue.picks:
        summary = lm_idx.get(pick.product_id)
        if summary is None:
            continue
        parts = summary.coin.split("/", 1)
        if len(parts) != 2:
            continue
        base = parts[0].upper()
        if not base or base in _STABLE_COINS:
            continue
        pick_usd = total_book * float(lm_venue.weight) * float(pick.weight)
        net_new = pick_usd - held_lm.get(pick.product_id, 0.0)
        if net_new < _MIN_ACTION_USDC:
            continue  # hold or reduce — exempt (de-risk handled elsewhere)
        info = perp_market.get(base) or perp_market.get(base.lower())
        if info is None:
            continue  # un-hedgeable — check_hedges_for_non_usd_picks owns this
        base_leg = Decimal(str(pick_usd)) * LM_BASE_LEG_FRACTION
        _, residual = _lm_hedge_residual(
            base_leg, info.mark_price, info.qty_step, info.min_order_qty
        )
        residual_usd = float(residual)
        if residual_usd > floor_usd:
            lot = (
                info.qty_step * info.mark_price
                if info.qty_step is not None and info.mark_price is not None
                else None
            )
            lot_s = f"${float(lot):.2f}" if lot is not None else "n/a"
            bad.append(
                f"bybit_lm/{pick.product_id}({base}): NEW base leg "
                f"${float(base_leg):.2f} floor-rounds the hedge to leave "
                f"${residual_usd:.2f} naked "
                f"({residual_usd / total_book * 100:.2f}% of book) > "
                f"{float(LM_RESIDUAL_NAKED_MAX) * 100:.0f}% floor (one perp lot "
                f"≈ {lot_s}); size to a clean lot multiple or pick a finer-lot pair"
            )
    if bad:
        return False, (
            "NEW LM picks leave un-hedgeable naked base residual above the floor: "
            + " | ".join(bad)
        )
    return True, None


def check_funding_rate_floor(d: Decision, snapshot: Snapshot) -> Check:
    """For each non-stable Earn pick (OnChain + FlexibleSaving), reject if
    the REALIZABLE NET-OF-HEDGE yield is not profitable. A non-stable Earn
    long is auto-hedged by a short perp (delta-neutral); the short receives
    positive funding (subsidy) and pays negative funding (cost), so the rate
    the vault actually earns is `gross_earn_apr + annualized_funding −
    friction` — the `effective_apr_net_hedge` the snapshot ranker computes.

    Net-yield floor (2026-06-08, replaces the raw-funding floor): reject only
    when this NET is ≤ `NET_HEDGE_YIELD_FLOOR` (0 ⇒ not profitable after the
    hedge bleeds it). The OLD rule gated raw funding ≥ -10.95%/yr, which
    blanket-rejected the entire high-APR hedgeable universe — e.g. ME at 101%
    Earn / -37%/yr funding nets to +64% delta-neutral but failed the
    raw-funding gate, so the agent fell back to ~3% stables. That contradicted
    the mandate (MAX yield at controlled risk): a high Earn APR that more than
    covers negative funding is a PRIME controllable-risk pick, not a reject.
    The ranker surfaces the highest net; the prompt probe-sizes unconfirmed
    (`estimate_apr`) highs and scales on `measured_yield`.

    Net-new only (2026-06-07, `bybit-sandbox.65`): gates OPENING or GROWING,
    not KEEPING. A held position the LLM keeps/reduces (net-new spend below
    `_MIN_ACTION_USDC`) is exempt — OnChain `Processing` positions can't be
    redeemed and the prompt instructs holding them, so rejecting the hold
    would strand the cycle skipped:invalid. Missing perp data → exempt here
    (`check_hedges_for_non_usd_picks` rejects un-hedgeable picks); missing
    funding → no signal → pass."""
    perp_market = getattr(snapshot, "perp_market", None) or {}
    total_book = float(snapshot.wallet.total_equity_usd)
    held_map = _held_usd_by_product(snapshot)
    bad: list[str] = []
    for venue_id, category in _AUTO_HEDGE_VENUES:
        venue = d.venue(venue_id)  # type: ignore[arg-type]
        if venue is None or not venue.picks:
            continue
        idx = _snapshot_index(snapshot).get(category, {})
        for pick in venue.picks:
            summary = idx.get(pick.product_id)
            if summary is None:
                continue
            coin = summary.coin.upper()
            if coin in _STABLE_COINS:
                continue
            net_new = (
                total_book * float(venue.weight) * float(pick.weight)
                - held_map.get((category, pick.product_id), 0.0)
            )
            if net_new < _MIN_ACTION_USDC:
                # Hold or reduce — not fresh exposure. Exempt
                # (may be un-exitable Processing; exits go via redeem path).
                continue
            info = perp_market.get(coin) or perp_market.get(coin.lower())
            if info is None:
                # Un-hedgeable — check_hedges_for_non_usd_picks owns this.
                continue
            # Realizable net-of-hedge yield via the shared helper (same one
            # the loop's sub-floor clamp uses, so they can't drift).
            net, interval_broken = net_hedge_yield(summary, info)
            if interval_broken:
                bad.append(
                    f"{venue_id}/{pick.product_id}({coin}): perp_market "
                    f"funding_interval_hours={info.funding_interval_hours!r} "
                    "invalid — cannot annualize hedge funding"
                )
                continue
            if net is None:
                continue  # no funding signal — can't assess, pass
            if net <= NET_HEDGE_YIELD_FLOOR:
                fr = info.funding_rate_7d_avg
                fr_s = f"{float(fr):+.6f}" if fr is not None else "n/a"
                bad.append(
                    f"{venue_id}/{pick.product_id}({coin}): net-of-hedge yield "
                    f"{net * 100:+.2f}%/yr <= floor "
                    f"{NET_HEDGE_YIELD_FLOOR * 100:.0f}% (7d funding {fr_s}; "
                    "hedge not profitable — Earn APR doesn't cover funding cost)"
                )

    # LM picks carry the same net-of-hedge gate: now that the base leg is
    # auto-hedged, a high-gross LP whose base funding bleeds the hedge can
    # net negative. The snapshot ranker stores the LM net (gross_LP +
    # ½·funding − ½·friction) in `effective_apr_net_hedge`, so the shared
    # `net_hedge_yield` helper reads it directly. Net-new scoped against
    # held LM principal so KEEPING a held LP whose funding turned negative
    # isn't re-rejected (exit goes via the redeem path, not a cycle reject).
    lm_venue = d.venue("bybit_lm")
    if lm_venue is not None and lm_venue.picks:
        lm_idx = _snapshot_index(snapshot).get("LiquidityMining", {})
        held_lm = _held_lm_usd_by_product(snapshot)
        for pick in lm_venue.picks:
            summary = lm_idx.get(pick.product_id)
            if summary is None:
                continue
            parts = summary.coin.split("/", 1)
            if len(parts) != 2:
                continue
            base = parts[0].upper()
            if not base or base in _STABLE_COINS:
                continue
            net_new = (
                total_book * float(lm_venue.weight) * float(pick.weight)
                - held_lm.get(pick.product_id, 0.0)
            )
            if net_new < _MIN_ACTION_USDC:
                continue  # hold or reduce — not fresh exposure
            info = perp_market.get(base) or perp_market.get(base.lower())
            if info is None:
                continue  # un-hedgeable — check_hedges_for_non_usd_picks owns this
            net, interval_broken = net_hedge_yield(summary, info)
            if interval_broken:
                bad.append(
                    f"bybit_lm/{pick.product_id}({base}): perp_market "
                    f"funding_interval_hours={info.funding_interval_hours!r} "
                    "invalid — cannot annualize hedge funding"
                )
                continue
            if net is None:
                continue  # no funding signal — can't assess, pass
            if net <= NET_HEDGE_YIELD_FLOOR:
                fr = info.funding_rate_7d_avg
                fr_s = f"{float(fr):+.6f}" if fr is not None else "n/a"
                bad.append(
                    f"bybit_lm/{pick.product_id}({base}): net-of-hedge LP yield "
                    f"{net * 100:+.2f}%/yr <= floor "
                    f"{NET_HEDGE_YIELD_FLOOR * 100:.0f}% (7d funding {fr_s}; "
                    "base-leg hedge bleeds the LP below profitable)"
                )

    if bad:
        return False, (
            "non-USD Earn picks whose hedged net yield isn't profitable: "
            + " | ".join(bad)
        )
    return True, None


# ─── Funding-carry rules (`bybit-strategy-expansion.4`) ────────────────────


# Carry venue id and snapshot category. The pair `(venue_id,
# snapshot_category)` mirrors the hedge layer's `HEDGE_VENUES`, but
# carry is intentionally NOT in that tuple — its picks don't trigger
# auto-hedge (they ARE the hedge). Both come from
# `agent.reason.venues` so the registry stays the only place where the
# strings are written.
_CARRY_VENUE_ID = CARRY_VENUE_ID
_CARRY_CATEGORY = CARRY_CATEGORY


def check_funding_carry_floor(d: Decision, snapshot: Snapshot) -> Check:
    """Each `bybit_funding_carry` pick must have its perp pair's 7d-avg
    funding rate **annualized** at or above `FUNDING_FLOOR_CARRY_ANNUAL`
    (~+5.5%/year). The snapshot's carry builder already filters by this
    threshold, but the validator restates the invariant defensively in
    case (a) the LLM hallucinates a product_id outside the snapshot
    (covered by `check_product_ids_in_snapshot`, but layered here for
    clarity) or (b) downstream code adds a FundingCarry row without
    going through `_build_funding_carry_products`. Missing perp_market
    entry → reject (no way to price the carry without funding data).

    Annualization reads each coin's `funding_interval_hours` so 4h /
    8h / 1h pairs compare like-for-like — a 4h coin at +0.00003/period
    (≈ +6.6%/year) is correctly accepted, while the pre-fix per-period
    comparison would have rejected it as "below floor".
    """
    carry = d.venue(_CARRY_VENUE_ID)  # type: ignore[arg-type]
    if carry is None or carry.weight <= 0 or not carry.picks:
        return True, None
    perp_market = getattr(snapshot, "perp_market", None) or {}
    carry_products = {
        p.product_id: p
        for p in snapshot.products.get(_CARRY_CATEGORY, [])
    }
    bad: list[str] = []
    for pick in carry.picks:
        summary = carry_products.get(pick.product_id)
        if summary is None:
            # Caught by check_product_ids_in_snapshot; skip duplicate
            # message here to keep error output focused.
            continue
        coin = summary.coin.upper()
        info = perp_market.get(coin) or perp_market.get(coin.lower())
        if info is None or info.funding_rate_7d_avg is None:
            bad.append(
                f"{pick.product_id}({coin}): perp_market funding_rate_7d_avg "
                "missing — cannot validate carry floor"
            )
            continue
        interval = (
            info.funding_interval_hours or DEFAULT_FUNDING_INTERVAL_HOURS
        )
        annual = _annual_funding(info.funding_rate_7d_avg, interval)
        if annual is None or annual < FUNDING_FLOOR_CARRY_ANNUAL:
            annual_pct = float(annual) * 100 if annual is not None else float("nan")
            floor_pct = float(FUNDING_FLOOR_CARRY_ANNUAL) * 100
            bad.append(
                f"{pick.product_id}({coin}): 7d avg funding "
                f"{float(info.funding_rate_7d_avg):+.6f}/{interval}h "
                f"({annual_pct:+.3f}% annualized) "
                f"below carry floor {floor_pct:+.3f}%/year"
            )
    if bad:
        return False, (
            "funding-carry picks below floor (exit required): "
            + " | ".join(bad)
        )
    return True, None


def check_no_double_carry_hedge(d: Decision, snapshot: Snapshot) -> Check:
    """A single coin cannot be carried in `bybit_funding_carry` AND
    appear as a non-stable Earn pick (`bybit_onchain` / `bybit_flex`) in
    the same decision. Both layers open a paired spot+perp short on
    that coin; running both at once would double-lock USDT margin AND
    double-open the short — neither catastrophic in isolation, but the
    second short violates the "delta-neutral, sized to spot" invariant
    that lets the executor reconcile per-coin. See
    `notes/bybit-funding-carry.md`.
    """
    carry = d.venue(_CARRY_VENUE_ID)  # type: ignore[arg-type]
    if carry is None or carry.weight <= 0 or not carry.picks:
        return True, None
    carry_products = {
        p.product_id: p
        for p in snapshot.products.get(_CARRY_CATEGORY, [])
    }
    carry_coins: set[str] = set()
    for pick in carry.picks:
        summary = carry_products.get(pick.product_id)
        if summary is None:
            continue
        carry_coins.add(summary.coin.upper())
    if not carry_coins:
        return True, None

    overlaps: list[str] = []
    for venue_id, category in _AUTO_HEDGE_VENUES:
        venue = d.venue(venue_id)  # type: ignore[arg-type]
        if venue is None or venue.weight <= 0 or not venue.picks:
            continue
        idx = _snapshot_index(snapshot).get(category, {})
        for pick in venue.picks:
            summary = idx.get(pick.product_id)
            if summary is None:
                continue
            coin = summary.coin.upper()
            if coin in _STABLE_COINS:
                continue
            if coin in carry_coins:
                overlaps.append(
                    f"{coin} in {_CARRY_VENUE_ID} AND {venue_id}/{pick.product_id}"
                )
    if overlaps:
        return False, (
            "coin overlap between funding-carry and non-stable Earn picks "
            "(would double-open perp short + double-lock margin): "
            + " | ".join(overlaps)
        )
    return True, None


def _pick_usd_value(
    d: Decision, snapshot: Snapshot, category: str, coin: str
) -> float:
    """Sum the USD-equivalent of every pick in `category` whose
    underlying coin matches `coin`. Used to size the hedge sanity
    check. Treats `snapshot.wallet.total_equity_usd` as the book
    baseline — same number the executor uses.
    """
    total_book = float(snapshot.wallet.total_equity_usd)
    if total_book <= 0:
        return 0.0
    idx = _snapshot_index(snapshot).get(category, {})
    target = coin.upper()
    # Reverse-map from snapshot_category back to venue_id. Built from
    # HEDGE_VENUES so a new auto-hedge venue gets picked up
    # automatically.
    category_to_venue = {cat: vid for vid, cat in HEDGE_VENUES}
    venue_id = category_to_venue.get(category)
    if not venue_id:
        return 0.0
    venue = d.venue(venue_id)  # type: ignore[arg-type]
    if venue is None or venue.weight <= 0 or not venue.picks:
        return 0.0
    out = 0.0
    for pick in venue.picks:
        summary = idx.get(pick.product_id)
        if summary is None:
            continue
        if summary.coin.upper() != target:
            continue
        out += total_book * float(venue.weight) * float(pick.weight)
    return out


# ─── Stable-spend cap (2026-06-03) ─────────────────────────────────────────


# Single source of truth for the margin-buffer multiplier lives in
# `agent.sandbox.snapshot.HEDGE_MARGIN_BUFFER` so the executor's USDT
# budget and the validator's pre-trade reservation can't drift. We hold
# a float alias here because the validator runs all its capital-flow math
# in floats — Decimal precision isn't required at the budget-screening
# scale, and propagating Decimal through every multiplication adds noise
# without changing outcomes.
_VALIDATOR_HEDGE_MARGIN_BUFFER = float(HEDGE_MARGIN_BUFFER)

# Cash floor as a fraction of total book — read off the cash_usdc venue's
# `min_weight` so a registry change propagates here without an edit. The
# capital-flow simulation treats the floor as the equity slice unavailable
# for commitment (the redemption / re-allocation buffer the executor uses
# to absorb withdrawal timing + slippage). Single source of truth: registry.
CASH_FLOOR = float(VENUE_REGISTRY["cash_usdc"].min_weight)

# Minimum USD delta the executor acts on — below this the diff layer
# skips the (un)subscribe entirely (`MIN_ACTION_USDC` in
# `agent.sandbox.execute`). The validator mirrors it so a near-no-op
# "hold" (target ≈ currently-held) reserves no liquid stables and trips
# no min-stake floor, matching what the live diff actually does.
_MIN_ACTION_USDC = 0.50


def _held_earn_detail(snapshot: Snapshot) -> dict[tuple[str, str], dict[str, Any]]:
    """Held Earn detail per `(category, product_id)`: `coin`, total `usd`,
    the REDEEMABLE (non-`Processing`) `redeemable_usd` portion, and an
    `is_stable` flag. Mirrors the executor's `_current_positions_by_pid` /
    `_amount_to_usd` (`agent.sandbox.execute`): stable balances at 1:1
    USD, non-stables priced via `perp_market[coin].mark_price`, multiple
    Bybit rows for one product SUMMED (a fresh OnChain subscribe shows up
    as a settled chunk + a `Processing` chunk until it clears).

    `Processing` chunks are excluded from `redeemable_usd` — they can't be
    redeemed this cycle (place-order Redeem reverts retCode=180020), so the
    capital they hold can't fund a same-cycle rotation. Validator and
    executor MUST agree on held USD: the net-new screens use `target −
    held` (the delta the live diff acts on); the stable-funding screen
    uses the redeemable portion as freeable supply."""
    perp_market = getattr(snapshot, "perp_market", None) or {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for p in getattr(snapshot, "earn_positions", None) or []:
        data = p.model_dump(mode="python") if hasattr(p, "model_dump") else p
        category = data.get("category") or ""
        pid = str(data.get("productId") or data.get("product_id") or "")
        if not category or not pid:
            continue
        try:
            amt = float(data.get("amount", 0) or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt <= 0:
            continue
        coin = (data.get("coin") or "USDC").upper()
        is_stable = coin in _STABLE_COINS
        if is_stable:
            usd = amt
        else:
            info = perp_market.get(coin) or perp_market.get(coin.lower())
            mark = getattr(info, "mark_price", None) if info else None
            usd = amt * float(mark) if mark and float(mark) > 0 else 0.0
        redeemable = str(data.get("status") or "").strip().lower() != "processing"
        entry = out.get((category, pid))
        if entry is None:
            entry = {
                "coin": coin,
                "usd": 0.0,
                "redeemable_usd": 0.0,
                "is_stable": is_stable,
            }
            out[(category, pid)] = entry
        entry["usd"] += usd
        if redeemable:
            entry["redeemable_usd"] += usd
    return out


def _held_usd_by_product(snapshot: Snapshot) -> dict[tuple[str, str], float]:
    """Total held Earn USD per `(category, product_id)` (incl. Processing
    chunks). Thin wrapper over `_held_earn_detail`; the net-new screens
    (stable-spend cap, min-stake, funding floor) compare `target − held`,
    the delta the live diff acts on. LM / advance-Earn holdings don't live
    in `earn_positions`, so they collapse to held=0 (gross), preserving
    pre-net-new behavior for those venues."""
    return {k: v["usd"] for k, v in _held_earn_detail(snapshot).items()}


def _held_lm_usd_by_product(snapshot: Snapshot) -> dict[str, float]:
    """Held LM principal (USD) per product_id, from `lm_positions`. LM
    positions aren't in `earn_positions`, so the funding-floor net-new
    screen needs this separately — without it, KEEPING a held LM whose
    funding turned negative would re-trip the floor and strand the cycle
    `skipped:invalid` (the `bybit-sandbox.65` failure mode). Reads Bybit's
    consolidated `principalLiquidityValue`; absent/malformed → 0."""
    out: dict[str, float] = {}
    for pos in getattr(snapshot, "lm_positions", None) or []:
        pid = str(pos.get("productId") or "")
        if not pid:
            continue
        raw = pos.get("principalLiquidityValue")
        try:
            val = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            val = 0.0
        if val > 0:
            out[pid] = out.get(pid, 0.0) + val
    return out


def check_stable_spend_cap(d: Decision, snapshot: Snapshot) -> Check:
    """Reject decisions whose NEW non-stable spend can't be funded by the
    liquid stable balance. Spend is scoped to `max(0, target − held)` per
    pick (the same delta the executor's `diff_to_actions` acts on), so
    keeping an existing position reserves nothing — only growing one or
    opening a fresh pick draws on the pool. Each unit of new non-stable
    Earn spend triggers two USD outflows the executor must satisfy from
    the stable pool:

      • spot leg — `pick_usd` worth of USDT spent on Buy {coin}USDT
      • perp margin — `pick_usd × HEDGE_MARGIN_BUFFER` of USDT locked
        in UNIFIED for the paired short

    Supply is `liquid_usdc + liquid_usdt` (UNIFIED+FUND across both),
    matching the executor's `_enforce_*_budget` pre-trade caps. When
    demand exceeds supply the executor will cascade-drop tail Buy
    swaps + paired subscribes/perps (safe), but the LLM could have
    avoided the partial fill by downsizing. This check surfaces the
    over-commit upstream so the rejected cycle's notes reach the next
    LLM turn instead of leaving partial exposure behind.

    Stable Earn picks (USDT/USD1/...) are not counted here — they
    consume USDC via a Sell swap, which is sized comfortably against
    `liquid_usdc` alone (the executor's USDC budget already handles
    that). This rule scopes specifically to the non-stable case where
    perp + spot demand stack on the USDT side.
    """
    total_book = float(snapshot.wallet.total_equity_usd)
    if total_book <= 0:
        return True, None

    perp_market = getattr(snapshot, "perp_market", None) or {}
    # Only NEW spend draws on the liquid pool. A pick the LLM keeps at
    # its current size (target ≈ currently-held) costs the diff nothing —
    # counting its gross target as fresh spend falsely rejects every hold
    # whenever a held non-stable position exceeds the liquid stable
    # balance (the dominant cause of the small-vault `skipped:invalid`
    # loop). Screen `max(0, target − held)` instead, mirroring the
    # executor's `delta = target_amt − current_amt` in `diff_to_actions`.
    held_map = _held_usd_by_product(snapshot)
    perp_demand = 0.0
    spot_demand = 0.0
    contributors: list[str] = []

    for venue_id, category in _AUTO_HEDGE_VENUES:
        venue = d.venue(venue_id)  # type: ignore[arg-type]
        if venue is None or venue.weight <= 0 or not venue.picks:
            continue
        idx = _snapshot_index(snapshot).get(category, {})
        for pick in venue.picks:
            summary = idx.get(pick.product_id)
            if summary is None:
                continue
            coin = summary.coin.upper()
            if coin in _STABLE_COINS:
                continue
            # Skip picks that wouldn't hedge anyway (no perp pair) —
            # check_hedges_for_non_usd_picks already rejects them, no
            # need to double-count here.
            info = perp_market.get(coin) or perp_market.get(coin.lower())
            if info is None:
                continue
            pick_usd = total_book * float(venue.weight) * float(pick.weight)
            net_new = pick_usd - held_map.get((category, pick.product_id), 0.0)
            if net_new < _MIN_ACTION_USDC:
                # Hold or reduce — the diff funds nothing from the liquid
                # pool, so this pick reserves no stables.
                continue
            held = held_map.get((category, pick.product_id), 0.0)
            perp_demand += net_new * _VALIDATOR_HEDGE_MARGIN_BUFFER
            spot_demand += net_new
            contributors.append(
                f"{venue_id}/{pick.product_id}({coin})=${net_new:.2f} new"
                + (f" (held ${held:.2f})" if held > 0 else "")
            )

    # Funding-carry picks (`bybit-strategy-expansion.4`). Each carry
    # pick opens its own paired spot Buy + perp short — same USDT
    # outflow shape as a hedged non-stable Earn pick. Capital-flow
    # accounting must include both layers or the carry venue would
    # silently overrun the stable supply.
    carry = d.venue(_CARRY_VENUE_ID)  # type: ignore[arg-type]
    if carry is not None and carry.weight > 0 and carry.picks:
        carry_idx = _snapshot_index(snapshot).get(_CARRY_CATEGORY, {})
        for pick in carry.picks:
            summary = carry_idx.get(pick.product_id)
            if summary is None:
                continue
            coin = summary.coin.upper()
            if coin in _STABLE_COINS:
                continue
            pick_usd = total_book * float(carry.weight) * float(pick.weight)
            held = held_map.get((_CARRY_CATEGORY, pick.product_id), 0.0)
            net_new = pick_usd - held
            if net_new < _MIN_ACTION_USDC:
                continue
            perp_demand += net_new * _VALIDATOR_HEDGE_MARGIN_BUFFER
            spot_demand += net_new
            contributors.append(
                f"{_CARRY_VENUE_ID}/{pick.product_id}({coin})=${net_new:.2f} new"
                + (f" (held ${held:.2f})" if held > 0 else "")
            )

    if not contributors:
        return True, None

    total_demand = perp_demand + spot_demand
    liquid_usdc = float(snapshot.wallet.liquid_usdc_usd)
    liquid_usdt = float(snapshot.wallet.liquid_usdt_usd)
    supply = liquid_usdc + liquid_usdt

    # When the snapshot didn't populate either field (pre-pivot fixtures,
    # the test sandbox, or a legacy collector), fall through — same
    # no-op semantics as the executor-side budget enforcers.
    if supply <= 0:
        return True, None

    if total_demand > supply + 1e-9:
        return False, (
            f"non-stable spend ${total_demand:.2f} (perp margin "
            f"${perp_demand:.2f} + spot ${spot_demand:.2f}) exceeds "
            f"liquid stables ${supply:.2f} "
            f"(USDC ${liquid_usdc:.2f} + USDT ${liquid_usdt:.2f}) — "
            f"downsize non-stable picks: {', '.join(contributors)}"
        )
    return True, None


# ─── Stable Earn funding gate (2026-06-07, `bybit-sandbox.65`) ─────────────


def check_stable_earn_funding(d: Decision, snapshot: Snapshot) -> Check:
    """NEW stable Earn subscribes must be fundable from liquid stables
    plus capital freed by redeeming/reducing OTHER redeemable stable Earn
    positions this cycle. `check_stable_spend_cap` screens the non-stable
    (hedged) side; this screens the stable side, where the live failure is
    `retCode=180016 Balance not enough` on SUBSCRIBE_EARN — e.g. the LLM
    keeps USD1 and still allocates 70% of book to fresh OnChain USDC/USDT
    against ~$6 liquid (prod 2026-06-07).

    Net-new + freed-by-redeem, mirroring the executor's redeem-first
    ordering (`diff_to_actions` emits redeems before subscribes and polls
    the freed balance credited before spending it). Freed counts only the
    REDEEMABLE portion of reduced/dropped stable positions — `Processing`
    chunks can't settle in time to fund a same-cycle subscribe.

    Scope / known limitations (all err toward PERMISSIVE — this check
    never blocks a fundable decision, so it can't re-introduce the
    every-cycle `skipped:invalid` loop it was added to fix; cases it
    misses fall through to the executor's `retCode=180016` + atomic-pair
    guard, which fail safe without naked exposure):
      • Pooled, not per-coin: supply is `liquid_usdc + liquid_usdt` and
        freed is credited coin-agnostically, but the executor funds a
        USDC subscribe only from USDC (no swap) and swaps USDC→coin for
        other stables. So a cross-coin rotation it passes may still 180016
        at execute. Over-credits supply → permissive.
      • Freed counts only non-`Processing` reduce/drops that settle within
        the cycle — slow OnChain redeems (~4d) are excluded (`.63`), so a
        rotation funded by one is correctly seen as unfundable here and
        deferred by the executor.
      • Shared pool with the non-stable side (`check_stable_spend_cap`):
        each subtracts the pool independently, so a kept non-stable +
        large new stable could overrun. Rare on small vaults.
    A future unified per-coin liquidity model would tighten all three;
    `check_capital_flow_simulation` is the gross-equity backstop."""
    total_book = float(snapshot.wallet.total_equity_usd)
    if total_book <= 0:
        return True, None

    detail = _held_earn_detail(snapshot)
    idx = _snapshot_index(snapshot)

    # Target USD per STABLE Earn (category, product_id) the decision asks
    # for, paired with the pick's coin (for the error message).
    targets: dict[tuple[str, str], tuple[float, str]] = {}
    for venue_id, category in _AUTO_HEDGE_VENUES:
        venue = d.venue(venue_id)  # type: ignore[arg-type]
        if venue is None or venue.weight <= 0 or not venue.picks:
            continue
        cat_idx = idx.get(category, {})
        for pick in venue.picks:
            summary = cat_idx.get(pick.product_id)
            if summary is None or summary.coin.upper() not in _STABLE_COINS:
                continue
            targets[(category, pick.product_id)] = (
                total_book * float(venue.weight) * float(pick.weight),
                summary.coin.upper(),
            )

    new_spend = 0.0
    freed = 0.0
    contributors: list[str] = []
    keys = set(targets) | {k for k, v in detail.items() if v["is_stable"]}
    for key in keys:
        info = detail.get(key)
        held = float(info["usd"]) if info else 0.0
        redeemable = float(info["redeemable_usd"]) if info else 0.0
        target, target_coin = targets.get(key, (0.0, ""))
        delta = target - held
        if delta >= _MIN_ACTION_USDC:
            new_spend += delta
            coin = info["coin"] if info else target_coin
            contributors.append(f"{key[0]}/{key[1]}({coin or '?'})=+${delta:.2f}")
        elif -delta >= _MIN_ACTION_USDC:
            # Reduced or dropped — frees its REDEEMABLE portion only, and
            # only if it settles within the cycle. A slow-settling OnChain
            # redeem (`bybit-sandbox.63`, ~4d Processing) can't fund a
            # same-cycle subscribe, so it doesn't count as freed — matching
            # the executor's `_redeem_settles_in_cycle` credit exclusion.
            if key[0] not in SLOW_SETTLE_CATEGORIES:
                freed += min(held - target, redeemable)

    if new_spend < _MIN_ACTION_USDC:
        return True, None

    liquid = float(snapshot.wallet.liquid_usdc_usd) + float(
        snapshot.wallet.liquid_usdt_usd
    )
    supply = liquid + freed
    # No liquidity signal at all (pre-pivot fixtures / legacy collector) →
    # no-op, mirroring `check_stable_spend_cap`'s supply<=0 fall-through.
    if supply <= 0:
        return True, None

    if new_spend > supply + 1e-9:
        return False, (
            f"new stable Earn spend ${new_spend:.2f} exceeds liquid stables "
            f"${liquid:.2f} + freed-by-redeem ${freed:.2f} = ${supply:.2f} "
            f"(retCode=180016 at execute) — redeem a held stable to fund the "
            f"rotation or downsize: {', '.join(contributors)}"
        )
    return True, None


# ─── Per-product min-stake gate (2026-06-04, `.51`) ────────────────────────


def check_min_stake(d: Decision, snapshot: Snapshot) -> Check:
    """Reject decisions where any pick's NEW subscribe (`target − held`,
    the amount the diff actually places) falls below the product's
    `min_subscribe_usd` floor.

    Bybit rejects sub-floor subscribes with `retCode=180012` (Purchase
    share invalid). The executor's diff layer already SKIPs them so the
    live call doesn't fire — but that quietly drops the pick from the
    cycle WITHOUT surfacing the violation to Claude. Catching it here
    puts the rejection in `_validator.errors`, where
    `_summarize_prior_decision` carries it into the next cycle as
    "downsize ID below $1.79 floor" instead of producing a silent
    partial allocation.

    Applies to every venue family where the snapshot category populates
    `min_subscribe_usd` (FlexibleSaving / OnChain / LiquidityMining /
    DualAssets / DiscountBuy). For stable coins (USDC/USDT/USD1/...)
    the snapshot stores the floor 1:1 with USD (Bybit's `minStakeAmount`
    is denominated in the product's coin); for non-stables it's an
    under-estimate so this rule reads as "best-effort lower bound" and
    the executor's diff-level SKIP is the backstop for the under-priced
    cases.
    """
    total_book = float(snapshot.wallet.total_equity_usd)
    if total_book <= 0:
        return True, None

    snapshot_idx = _snapshot_index(snapshot)
    held_map = _held_usd_by_product(snapshot)
    violations: list[str] = []

    for v in d.venues:
        if v.weight <= 0 or not v.picks:
            continue
        meta = VENUE_REGISTRY[v.venue_id]
        category = meta.snapshot_category
        if not category:
            continue
        cat_idx = snapshot_idx.get(category, {})
        for p in v.picks:
            summary = cat_idx.get(p.product_id)
            if summary is None:
                continue
            min_usd = summary.min_subscribe_usd
            if min_usd is None or min_usd <= 0:
                continue
            pick_usd = total_book * float(v.weight) * float(p.weight)
            # Only the NEW subscribe portion (target − held) hits Bybit's
            # min-stake floor. A hold (delta < MIN_ACTION) places no
            # subscribe so it can't trip retCode=180012; a delta below
            # MIN_ACTION the executor skips silently. Flag the band the
            # live diff would attempt-then-reject: a real subscribe
            # (>= MIN_ACTION) that's still under the floor. (LM /
            # advance-Earn holdings aren't in earn_positions → held=0 →
            # gross, unchanged from before.)
            held = held_map.get((category, p.product_id), 0.0)
            delta = pick_usd - held
            # Only a FRESH sub-min pick (no held position) trips 180012 and is
            # dropped wholesale — surface THAT. A sub-min delta ON TOP of a
            # held position is hold-rounding drift: the executor skips the tiny
            # add (execute.py SUBSCRIBE_EARN min gate) and keeps the position,
            # so it's no failure and must not block the cycle. Held Processing
            # positions the LLM intends to HOLD drift target-vs-held by weight
            # quantization every cycle (e.g. $0.56 on a $67 stake), which was
            # stranding the loop `skipped:invalid` for a pure hold.
            if (
                held < _MIN_ACTION_USDC
                and delta >= _MIN_ACTION_USDC
                and delta + 1e-9 < float(min_usd)
            ):
                violations.append(
                    f"{v.venue_id}/{p.product_id}({summary.coin})="
                    f"${delta:.2f}<min ${float(min_usd):.2f}"
                )
    if violations:
        return False, (
            f"picks below per-product min_subscribe_usd "
            f"(Bybit retCode=180012 at execute): {', '.join(violations)}"
        )
    return True, None


# ─── Capital-flow simulation (2026-06-04, `.50`) ───────────────────────────


# Venues whose picks commit at face value — no margin layer, no hedged
# spot leg. LM at leverage=1 is unleveraged; advance-Earn / hold-to-earn
# / alpha / aave all consume a single coin balance without a paired
# perp. cash_usdc is the float itself, excluded from commitment.
_FACE_VALUE_VENUES: frozenset[VenueId] = frozenset({
    "bybit_dual_asset",
    "bybit_discount_buy",
    "bybit_smart_leverage",
    "bybit_double_win",
    "bybit_hold_to_earn",
    "bybit_alpha",
    "aave_v3_usdc",
})


def check_capital_flow_simulation(d: Decision, snapshot: Snapshot) -> Check:
    """Reject target portfolios whose committed capital overflows book
    equity once hedge margin is layered on top of Earn stakes.

    Each non-stable Earn pick on `bybit_flex`/`bybit_onchain` (and every
    `bybit_funding_carry` pick) commits two slices of book:
      • spot stake — `pick_usd` worth of the underlying coin
      • perp margin — `pick_usd × HEDGE_MARGIN_BUFFER` of USDT locked
        in UNIFIED for the paired short

    Only the stake is captured by `sum(venue.weight) ≤ 1`; the margin
    is invisible to per-venue cap math. So a portfolio like
    `bybit_onchain=0.65 + bybit_lm=0.25 + cash=0.10` passes every cap
    individually but commits `0.65×1.05 + 0.25 + 0.10 = 1.03×book` —
    the live cycle will `retCode=170131` on the perp open.

    A non-stable LM pick commits its full deposit (stake) plus perp
    margin on the BASE half (`pick_usd × 0.5 × HEDGE_MARGIN_BUFFER`) —
    the base leg is now auto-hedged. Stable Earn picks, advance-Earn,
    hold-to-earn, alpha, and Aave commit at face value (no margin lock).
    Picks without a `perp_market` entry on the symbol get face value
    too — `check_hedges_for_non_usd_picks` rejects them upstream, so
    counting margin here would produce a confusing duplicate error.

    Allowable commitment = `total_equity_usd × (1 - CASH_FLOOR)`. The
    cash floor is the equity slice the executor reserves as a
    redemption / re-allocation buffer; commitment must fit in what's
    left.

    Distinct from `check_stable_spend_cap`: that rule screens the
    *live* execute step against the actually-liquid USDC+USDT pool.
    This rule screens the *target state* against total equity, firing
    even when liquid stables happen to be high (e.g. on the cycle
    right after a big redeem) — without it the next live cycle walks
    into the same wall once positions land.

    NET-NEW scoped (`bybit-sandbox`, 2026-06-08): each pick commits only
    `max(0, target − held)`, mirroring `check_min_stake` /
    `check_stable_spend_cap`. The `retCode=170131` this guards is a
    *perp-OPEN* event — a held, already-open hedge poses no new-open risk,
    so re-counting it is wrong. Pre-fix the rule counted held positions
    gross at 2.05×, so an un-redeemable `Processing` non-stable (TON) that
    the prompt instructs the LLM to HOLD was re-counted as fresh
    commitment and rejected the pure hold every cycle (6/25 prod cycles).
    """
    total_book = float(snapshot.wallet.total_equity_usd)
    if total_book <= 0:
        return True, None

    allowable = total_book * (1.0 - CASH_FLOOR)
    perp_market = getattr(snapshot, "perp_market", None) or {}
    snapshot_idx = _snapshot_index(snapshot)
    held_map = _held_usd_by_product(snapshot)

    committed = 0.0
    contributors: list[str] = []

    for v in d.venues:
        if v.weight <= 0 or v.venue_id == "cash_usdc":
            continue
        venue_usd = total_book * float(v.weight)

        if v.venue_id in {"bybit_flex", "bybit_onchain"}:
            category = VENUE_REGISTRY[v.venue_id].snapshot_category
            cat_idx = snapshot_idx.get(category, {}) if category else {}
            for p in v.picks:
                pick_usd = venue_usd * float(p.weight)
                # Only NEW commitment counts — held capital is already
                # deployed (and an un-redeemable Processing stake can't be
                # downsized this cycle anyway), so it can't overrun a fresh
                # perp open. Mirrors `check_min_stake`'s `delta`.
                net_new = pick_usd - (
                    held_map.get((category, p.product_id), 0.0) if category else 0.0
                )
                if net_new < _MIN_ACTION_USDC:
                    continue
                summary = cat_idx.get(p.product_id)
                coin = (summary.coin.upper() if summary else "")
                multiplier = 1.0
                if coin and coin not in _STABLE_COINS:
                    info = (
                        perp_market.get(coin)
                        or perp_market.get(coin.lower())
                    )
                    if info is not None:
                        multiplier = 1.0 + _VALIDATOR_HEDGE_MARGIN_BUFFER
                slice_usd = net_new * multiplier
                committed += slice_usd
                contributors.append(
                    f"{v.venue_id}/{p.product_id}({coin or '?'})"
                    f"=${slice_usd:.2f}"
                )
            continue

        if v.venue_id == CARRY_VENUE_ID:
            # Carry picks are always non-stable and always paired with
            # a perp short — every pick contributes stake + margin. Held
            # carry lives in carry_state (not earn_positions), so held=0
            # here and net_new == pick_usd; a re-stated held carry counts
            # gross, matching pre-net-new behavior for this venue.
            for p in v.picks:
                pick_usd = venue_usd * float(p.weight)
                net_new = pick_usd - held_map.get(
                    (CARRY_CATEGORY, p.product_id), 0.0
                )
                if net_new < _MIN_ACTION_USDC:
                    continue
                slice_usd = net_new * (1.0 + _VALIDATOR_HEDGE_MARGIN_BUFFER)
                committed += slice_usd
                contributors.append(
                    f"{v.venue_id}/{p.product_id}=${slice_usd:.2f}"
                )
            continue

        if v.venue_id == "bybit_lm":
            # LM commits the full deposit as stake (face) PLUS perp margin
            # on the BASE half (`_lm_hedge_targets` shorts half the pick).
            # margin = (pick_usd × 0.5) × buffer, added only when the base
            # is non-stable and has a perp. Held LM lives in lm_positions
            # (not earn_positions), so held=0 here and the stake counts
            # gross — same conservative treatment as carry; LM's 0.30 cap
            # bounds any over-count.
            cat_idx = snapshot_idx.get("LiquidityMining", {})
            for p in v.picks:
                pick_usd = venue_usd * float(p.weight)
                if pick_usd < _MIN_ACTION_USDC:
                    continue
                summary = cat_idx.get(p.product_id)
                base = ""
                if summary is not None:
                    parts = summary.coin.split("/", 1)
                    if len(parts) == 2:
                        base = parts[0].upper()
                margin = 0.0
                if base and base not in _STABLE_COINS:
                    info = perp_market.get(base) or perp_market.get(base.lower())
                    if info is not None:
                        margin = pick_usd * 0.5 * _VALIDATOR_HEDGE_MARGIN_BUFFER
                slice_usd = pick_usd + margin
                committed += slice_usd
                contributors.append(
                    f"{v.venue_id}/{p.product_id}({base or '?'})=${slice_usd:.2f}"
                )
            continue

        if v.venue_id in _FACE_VALUE_VENUES:
            committed += venue_usd
            contributors.append(f"{v.venue_id}=${venue_usd:.2f}")
            continue

        # Unknown venue: be conservative and count at face — keeps the
        # rule safe if a new venue ships without updating this dispatch.
        committed += venue_usd
        contributors.append(f"{v.venue_id}=${venue_usd:.2f}")

    if committed > allowable + 1e-9:
        return False, (
            f"target capital commitment ${committed:.2f} exceeds "
            f"book × (1 - cash_floor) = ${allowable:.2f} "
            f"(book ${total_book:.2f}, cash_floor {CASH_FLOOR:.0%}, "
            f"hedge buffer {_VALIDATOR_HEDGE_MARGIN_BUFFER:.2f}×) — "
            f"downsize: {', '.join(contributors)}"
        )
    return True, None


# ─── Slow-settle over-allocation cap (2026-06-08) ──────────────────────────

# Max fraction of book allowed in SLOW-SETTLE venues (OnChain — ~7-day
# `Processing` redeem lock). OnChain stables out-yield Flex (3.4% vs 0.8%)
# but freeze capital for days; over-allocating starves the liquid budget the
# agent needs to deploy into the high-net hedged picks (ME +60%) the
# net-hedge floor unblocked — the real cause of the chronic ~3% blended yield
# (prod 2026-06-08: 54% of book frozen in OnChain @3.4%, only ~$9 deployable).
# Keeps >= (1 - cap) of book in liquid/fast venues as deployable powder.
SLOW_SETTLE_MAX_WEIGHT = 0.50

# Default-to-liquid-Flex margin for NEW stable yield (2026-06-08). Below the
# 50% slow-settle wall the agent still defaults to OnChain on stables because
# its gross APR reads higher — but the dead-time discount (subscribe warmup +
# ~7d redeem `Processing`) eats much of that edge, and the frozen capital
# can't chase high-net hedged picks. So when a same-coin liquid Flex twin
# exists, NEW OnChain stable yield must beat it by at least this margin on the
# dead-time-discounted rate (`effective_apr_net_holding`) to justify the lock;
# otherwise route the NEW stable yield to Flex (instant redeem).
SLOW_SETTLE_STABLE_PREF_MARGIN = 0.015  # 1.5%/yr absolute

# Margin a NEW non-stable LM pick's net-of-hedge yield must clear over the
# best available stable to justify being taken instead of the stable
# (2026-06-09). The LM `effective_apr_net_hedge` already nets out funding +
# friction on the hedged base half, but it does NOT price the residual
# impermanent loss (the pool rebalances into the falling asset) nor the
# operational cost of running a perp hedge (short-side liquidation tail,
# rebalance slippage, funding variance). So a hedged LM that only ~ties a
# stable is NOT worth the extra risk surface — "max yield AT CONTROLLED
# risk" means the edge has to pay for the risk it adds. Same magnitude and
# net-new posture as `SLOW_SETTLE_STABLE_PREF_MARGIN`: held LM is exempt
# (never force-exited), so this only steers NEW deployment toward stables
# when funding has eaten the LM edge thin.
LM_STABLE_PREF_MARGIN = 0.015  # 1.5%/yr absolute

# Source-quality ladder for high-yield non-stable picks. A non-stable Earn
# pick whose APR is a bare `estimate_apr` (Bybit's quoted base, often a
# transient/mis-quoted promo — e.g. ME 98% gross) may collapse, leaving the
# hedge to bleed funding. Enter it as a PROBE; scale only as the rate confirms
# from a higher-quality source. This is a 3-tier NET-NEW cap keyed on
# `apr_source`, NOT an N-cycle counter — the validator is stateless and reads
# only the current snapshot:
#   estimate_apr   → ESTIMATE_PROBE_CAP        (probe, unconfirmed)
#   measured_yield → MEASURED_YIELD_SCALE_CAP  (confirmed but noisy)
#   apr_history    → exempt here (pool-level, noise-immune) → MAX_EFFECTIVE_PRODUCT
# Caps NEW effective weight; held probes are exempt (growing one past the cap
# is gated, holding it isn't). Enforces the "probe→scale" posture
# deterministically rather than trusting the prompt.
ESTIMATE_PROBE_CAP = 0.07

# Intermediate tier between the 7% probe and the 0.60 product cap.
# `measured_yield` is a CONFIRMED realized rate (our own position) but is
# single-position-noisy on a tiny stake (snapshot.py ~L519-522: sub-precision
# hourly credits annualize spuriously), so it earns an intermediate net-new
# cap — above the bare-estimate probe, below the full product cap. Only
# pool-level `apr_history` (noise-immune) unlocks 0.60 via
# `check_effective_pick_cap`.
MEASURED_YIELD_SCALE_CAP = 0.30


def check_slow_settle_cap(d: Decision, snapshot: Snapshot) -> Check:
    """Reject decisions that lock NEW capital into slow-settle (OnChain) Earn
    once the book is at/over `SLOW_SETTLE_MAX_WEIGHT` there. Net-new scoped:
    held OnChain (un-redeemable `Processing`) is exempt — only fresh spend
    counts, so this never rejects a forced hold; it stops the agent FREEZING
    MORE and keeps liquidity for high-net deployment."""
    total_book = float(snapshot.wallet.total_equity_usd)
    if total_book <= 0:
        return True, None
    detail = _held_earn_detail(snapshot)
    held_slow = sum(
        float(v["usd"]) for k, v in detail.items()
        if k[0] in SLOW_SETTLE_CATEGORIES
    )
    idx = _snapshot_index(snapshot)
    new_slow = 0.0
    contributors: list[str] = []
    for venue_id, category in _AUTO_HEDGE_VENUES:
        if category not in SLOW_SETTLE_CATEGORIES:
            continue
        venue = d.venue(venue_id)  # type: ignore[arg-type]
        if venue is None or venue.weight <= 0 or not venue.picks:
            continue
        cat_idx = idx.get(category, {})
        for pick in venue.picks:
            if cat_idx.get(pick.product_id) is None:
                continue
            target = total_book * float(venue.weight) * float(pick.weight)
            held = float(
                detail.get((category, pick.product_id), {}).get("usd", 0.0)
            )
            net_new = target - held
            if net_new >= _MIN_ACTION_USDC:
                new_slow += net_new
                contributors.append(
                    f"{venue_id}/{pick.product_id}=+${net_new:.2f}"
                )
    if new_slow < _MIN_ACTION_USDC:
        return True, None
    cap_usd = total_book * SLOW_SETTLE_MAX_WEIGHT
    total_after = held_slow + new_slow
    if total_after > cap_usd + 1e-9:
        return False, (
            f"slow-settle (OnChain) exposure would reach ${total_after:.2f} "
            f"({total_after / total_book:.0%} of book) > cap "
            f"{SLOW_SETTLE_MAX_WEIGHT:.0%} (held ${held_slow:.2f} + new "
            f"${new_slow:.2f}) — OnChain locks ~7d, freezing capital you need "
            f"for high-net picks; route NEW stable yield to liquid Flex or a "
            f"high-net hedged pick: {', '.join(contributors)}"
        )
    return True, None


def check_slow_settle_stable_preference(d: Decision, snapshot: Snapshot) -> Check:
    """Default NEW stable yield to liquid Flex over slow-settle OnChain unless
    the OnChain rate clears `SLOW_SETTLE_STABLE_PREF_MARGIN`. Independent of the
    50% slow-settle wall: even with room under the cap, locking stables into a
    ~7d-redeem OnChain pick when a same-coin Flex twin yields ~the same freezes
    capital for no real edge.

    Net-new scoped (mirrors `check_slow_settle_cap`): for each OnChain stable
    pick with NEW spend (`target − held`) >= `_MIN_ACTION_USDC`, look up a
    same-coin stable Flex product; if one exists, REJECT only when the OnChain
    net rate (`effective_apr_net_holding`, else `effective_apr` — the dead-time
    discount is the whole point) beats the Flex twin's by less than the margin.
    Never rejects a forced hold (net-new below the floor) or a coin with no
    Flex twin to route to."""
    total_book = float(snapshot.wallet.total_equity_usd)
    if total_book <= 0:
        return True, None
    detail = _held_earn_detail(snapshot)
    idx = _snapshot_index(snapshot)
    flex_idx = idx.get("FlexibleSaving", {})
    bad: list[str] = []
    for venue_id, category in _AUTO_HEDGE_VENUES:
        if category not in SLOW_SETTLE_CATEGORIES:
            continue
        venue = d.venue(venue_id)  # type: ignore[arg-type]
        if venue is None or venue.weight <= 0 or not venue.picks:
            continue
        cat_idx = idx.get(category, {})
        for pick in venue.picks:
            summary = cat_idx.get(pick.product_id)
            if summary is None or summary.coin.upper() not in _STABLE_COINS:
                continue
            target = total_book * float(venue.weight) * float(pick.weight)
            held = float(
                detail.get((category, pick.product_id), {}).get("usd", 0.0)
            )
            # New spend below the product's min_subscribe can't be placed (the
            # executor skips it) — so it's not a real "route this to Flex"
            # decision, just hold-rounding drift on a held position. Exempt it
            # alongside the forced-hold case, else a $0.56 quantization sliver
            # on a held Processing stake strands the cycle skipped:invalid.
            min_sub = float(summary.min_subscribe_usd or 0.0)
            if target - held < max(_MIN_ACTION_USDC, min_sub):
                continue  # forced hold / sub-min sliver — never rejected
            coin = summary.coin.upper()
            # Compare against the BEST same-coin Flex twin that's actually
            # fundable at this pick size — not an arbitrary first match. A
            # twin whose min_subscribe exceeds the target can't absorb this
            # yield, so it's no real alternative; and routing should target the
            # highest-yielding liquid twin, else a low-APR twin listed first
            # would spuriously clear (or a high one spuriously block) the gate.
            feasible_twins = [
                p
                for p in flex_idx.values()
                if p.coin.upper() == coin
                and (
                    p.min_subscribe_usd is None
                    or float(p.min_subscribe_usd) <= target
                )
            ]
            if not feasible_twins:
                continue  # no fundable liquid twin to route to
            onchain_net = float(
                summary.effective_apr_net_holding
                if summary.effective_apr_net_holding is not None
                else summary.effective_apr
            )
            flex_twin = max(
                feasible_twins,
                key=lambda p: float(
                    p.effective_apr_net_holding
                    if p.effective_apr_net_holding is not None
                    else p.effective_apr
                ),
            )
            flex_net = float(
                flex_twin.effective_apr_net_holding
                if flex_twin.effective_apr_net_holding is not None
                else flex_twin.effective_apr
            )
            if onchain_net - flex_net < SLOW_SETTLE_STABLE_PREF_MARGIN:
                bad.append(
                    f"{venue_id}/{pick.product_id}({coin}): OnChain net "
                    f"{onchain_net:.2%} barely beats same-coin Flex "
                    f"{flex_twin.product_id} {flex_net:.2%} (< "
                    f"{SLOW_SETTLE_STABLE_PREF_MARGIN:.1%} margin) — route NEW "
                    f"{coin} yield to liquid Flex, don't freeze it ~7d OnChain"
                )
    if bad:
        return False, (
            "NEW stable yield should default to liquid Flex: " + " | ".join(bad)
        )
    return True, None


def _best_stable_net_apr(snapshot: Snapshot, max_subscribe_usd: float) -> tuple[float, str] | None:
    """Best realizable stable Earn yield the agent could deploy `max_subscribe_usd`
    into right now — the opportunity cost of any non-stable pick. Scans
    FlexibleSaving + OnChain stable products fundable at this size and returns
    `(net_apr, "venue/product")` for the highest `effective_apr_net_holding`
    (else `effective_apr`), or None when none qualify."""
    idx = _snapshot_index(snapshot)
    best: tuple[float, str] | None = None
    for category in ("FlexibleSaving", "OnChain"):
        for pid, summary in idx.get(category, {}).items():
            if summary.coin.upper() not in _STABLE_COINS:
                continue
            if (
                summary.min_subscribe_usd is not None
                and float(summary.min_subscribe_usd) > max_subscribe_usd
            ):
                continue
            net = float(
                summary.effective_apr_net_holding
                if summary.effective_apr_net_holding is not None
                else summary.effective_apr
            )
            if best is None or net > best[0]:
                best = (net, f"{category}/{pid}")
    return best


def check_lm_stable_preference(d: Decision, snapshot: Snapshot) -> Check:
    """A NEW non-stable LM pick must beat the best available stable yield by
    `LM_STABLE_PREF_MARGIN` on a net-of-hedge basis, else route the capital to
    the stable instead. The hedged-LM net (`effective_apr_net_hedge`) prices
    funding + friction but NOT residual IL or perp-hedge maintenance, so a
    thin edge over a zero-risk stable isn't "max yield at controlled risk" —
    it's extra risk surface for no real return.

    Net-new scoped (mirrors `check_slow_settle_stable_preference`): held LM
    (`target − held < _MIN_ACTION_USDC`) is EXEMPT — this never force-exits a
    standing position (exit is a redeem decision, not a cycle reject), it only
    stops the agent OPENING/GROWING a thin-edge LP. Passes when no fundable
    stable alternative exists or the base is stable (no hedge / no IL)."""
    total_book = float(snapshot.wallet.total_equity_usd)
    if total_book <= 0:
        return True, None
    lm_venue = d.venue("bybit_lm")
    if lm_venue is None or lm_venue.weight <= 0 or not lm_venue.picks:
        return True, None
    lm_idx = _snapshot_index(snapshot).get("LiquidityMining", {})
    held_lm = _held_lm_usd_by_product(snapshot)
    bad: list[str] = []
    for pick in lm_venue.picks:
        summary = lm_idx.get(pick.product_id)
        if summary is None:
            continue
        parts = summary.coin.split("/", 1)
        if len(parts) != 2:
            continue
        base = parts[0].upper()
        if not base or base in _STABLE_COINS:
            continue  # stable-base LP carries no directional/IL premium to justify
        target = total_book * float(lm_venue.weight) * float(pick.weight)
        if target - held_lm.get(pick.product_id, 0.0) < _MIN_ACTION_USDC:
            continue  # hold or reduce — never rejected
        lm_net = float(
            summary.effective_apr_net_hedge
            if summary.effective_apr_net_hedge is not None
            else summary.effective_apr
        )
        best_stable = _best_stable_net_apr(snapshot, target)
        if best_stable is None:
            continue  # no fundable stable alternative to route to
        stable_net, stable_id = best_stable
        if lm_net - stable_net < LM_STABLE_PREF_MARGIN:
            bad.append(
                f"bybit_lm/{pick.product_id}({summary.coin}): hedged net "
                f"{lm_net:.2%} barely beats best stable {stable_id} "
                f"{stable_net:.2%} (< {LM_STABLE_PREF_MARGIN:.1%} margin) — the "
                f"edge doesn't pay for the LP's IL + perp-hedge maintenance; "
                f"route NEW capital to the stable"
            )
    if bad:
        return False, (
            "NEW LM picks must beat stables by a risk margin: " + " | ".join(bad)
        )
    return True, None


def check_estimate_apr_probe_cap(d: Decision, snapshot: Snapshot) -> Check:
    """3-tier SOURCE-QUALITY ladder on the NEW effective weight of a
    high-yield non-stable Earn pick, keyed on `apr_source`:
      • `estimate_apr`   → `ESTIMATE_PROBE_CAP` (7%) — a bare quoted rate may
        be a transient promo; if it collapses the hedge bleeds funding, so
        probe it.
      • `measured_yield` → `MEASURED_YIELD_SCALE_CAP` (30%) — confirmed
        (our own realized rate) but single-position-noisy, so an intermediate
        scale rather than the full product cap.
      • `apr_history` / anything else → exempt here; the pool-level,
        noise-immune `apr_history` unlocks the 0.60 `check_effective_pick_cap`.
    Source-quality, NOT an N-cycle counter — the validator is STATELESS and
    reads only the current snapshot. Net-new scoped (growing a held position
    past the cap is gated; holding isn't)."""
    total_book = float(snapshot.wallet.total_equity_usd)
    if total_book <= 0:
        return True, None
    source_caps = {
        "estimate_apr": ("probe", ESTIMATE_PROBE_CAP),
        "measured_yield": ("measured_yield scale", MEASURED_YIELD_SCALE_CAP),
    }
    detail = _held_earn_detail(snapshot)
    idx = _snapshot_index(snapshot)
    bad: list[str] = []
    for venue_id, category in _AUTO_HEDGE_VENUES:
        venue = d.venue(venue_id)  # type: ignore[arg-type]
        if venue is None or venue.weight <= 0 or not venue.picks:
            continue
        cat_idx = idx.get(category, {})
        for pick in venue.picks:
            summary = cat_idx.get(pick.product_id)
            if summary is None or summary.coin.upper() in _STABLE_COINS:
                continue
            tier = source_caps.get(summary.apr_source)
            if tier is None:
                continue  # apr_history / missing / other → product cap governs
            tier_name, cap = tier
            target = total_book * float(venue.weight) * float(pick.weight)
            held = float(
                detail.get((category, pick.product_id), {}).get("usd", 0.0)
            )
            net_new_frac = max(0.0, (target - held) / total_book)
            if net_new_frac > cap + 1e-9:
                bad.append(
                    f"{venue_id}/{pick.product_id}({summary.coin}): NEW "
                    f"{net_new_frac:.0%} of book > {tier_name} cap "
                    f"{cap:.0%} on a {summary.apr_source} pick — scale only "
                    f"as the source confirms (apr_history unlocks 0.60)"
                )
    if bad:
        return False, (
            "high-yield picks exceed source-quality cap: " + " | ".join(bad)
        )
    return True, None


# ─── Aggregate ─────────────────────────────────────────────────────────────


def validate(decision: Decision, snapshot: Snapshot) -> tuple[bool, list[str]]:
    """Run every rule against `(decision, snapshot)` and aggregate the
    failures. `ok=True` iff `errors` is empty.

    All checks run — we do NOT short-circuit on the first failure
    because the operator wants to see *every* problem in one pass when
    debugging a flaky prompt. This is a cheap, deterministic pipeline;
    no rule depends on another's outcome.
    """
    pure_checks = [
        check_disabled_venues,
        check_venue_caps,
        check_venue_floors,
        check_picks_required,
        check_confidence,
        check_risk_flags,
    ]
    snapshot_checks = [
        check_effective_pick_cap,
        check_peg_stress,
        check_product_ids_in_snapshot,
        check_no_missing_apr_source,
        check_lockup_cap,
        check_lm_leverage_forbidden,
        check_lm_leverage_size_cap,
        check_hedges_for_non_usd_picks,
        check_lm_residual_naked_exposure,
        check_funding_rate_floor,
        check_funding_carry_floor,
        check_no_double_carry_hedge,
        check_stable_spend_cap,
        check_stable_earn_funding,
        check_capital_flow_simulation,
        check_min_stake,
        check_slow_settle_cap,
        check_slow_settle_stable_preference,
        check_lm_stable_preference,
        check_estimate_apr_probe_cap,
    ]
    errors: list[str] = []
    for check in pure_checks:
        ok, msg = check(decision)
        if not ok and msg:
            errors.append(msg)
    for check_with_snap in snapshot_checks:
        ok, msg = check_with_snap(decision, snapshot)
        if not ok and msg:
            errors.append(msg)
    return (not errors), errors


def _weight(d: Decision, venue_id: VenueId) -> float:
    v = d.venue(venue_id)
    return v.weight if v is not None else 0.0
