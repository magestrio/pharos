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

from agent.reason.schema import Decision, VenueAllocation
from agent.reason.venues import VENUE_REGISTRY, VenueId
from agent.sandbox.snapshot import (
    DEFAULT_FUNDING_INTERVAL_HOURS,
    FUNDING_FLOOR_CARRY_ANNUAL,
    HEDGE_MARGIN_BUFFER,
    STABLES,
    ProductSummary,
    Snapshot,
    _annual_funding,
)

MIN_CONFIDENCE = 0.4
# Cap on a single non-stable product as fraction of TOTAL book.
# Tight because non-stables carry directional + funding-rate risk on
# top of counterparty risk — concentration in one volatile coin is
# strictly worse than spreading across two.
MAX_EFFECTIVE_PRODUCT = 0.50
# Stable Earn picks (USD1/USDC/USDT/USDE/...) on FlexibleSaving / OnChain
# get a wider cap. The dominant risk on a stable Earn pick is Bybit's
# Earn-product counterparty risk (custody, smart contract, settlement),
# which is independent of *which* stable is staked. Splitting USD1
# across two products on the same venue doesn't actually reduce that
# risk — it just dilutes APR onto the lower-yielding fallback. So we
# let the LLM concentrate on the highest-APR stable Earn pick up to
# the venue cap (typically 0.70). Raised from 0.50 on 2026-06-03 after
# small-vault $30 idle audit — the prior cap forced LLM into 40% cash
# when a 70% USD1 allocation was structurally safer.
MAX_EFFECTIVE_STABLE_PRODUCT = 0.70

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
                cat in ("FlexibleSaving", "OnChain")
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
    """Reject any pick whose `redeem_lockup_minutes` (from snapshot)
    exceeds `MAX_LOCKUP_MINUTES`. Picks without surfaced lockup are
    treated as instant-redeem and allowed through (e.g. FlexibleSaving
    products typically have `redeem_lockup_minutes=None`)."""
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


def _extract_max_leverage(summary: ProductSummary) -> int | None:
    for note in summary.notes:
        if note.startswith("max_leverage="):
            try:
                return int(note.split("=", 1)[1])
            except (ValueError, IndexError):
                return None
    return None


# Earn venues whose non-stable picks must clear the auto-hedge
# feasibility gate. Mirrors `execute._AUTO_HEDGE_CATEGORIES` — keeping
# the two lists in lockstep is a soft contract; the test suite catches
# drift via `test_aggregate_validate_passes_hedged_non_usd_pick`.
_AUTO_HEDGE_VENUES: tuple[tuple[str, str], ...] = (
    ("bybit_onchain", "OnChain"),
    ("bybit_flex", "FlexibleSaving"),
)


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


def check_funding_rate_floor(d: Decision, snapshot: Snapshot) -> Check:
    """For each non-stable Earn pick (OnChain + FlexibleSaving), reject
    if the perp pair's 7-day average funding rate (annualized) is below
    `FUNDING_FLOOR_HEDGE_ANNUAL`. We're short the perp to hedge; a
    persistently negative funding rate means we PAY funding every
    period — the hedge becomes net cost and erodes the Earn yield over
    time. Operator change 2026-05-29: funding is part of yield, not
    just a soft signal. Picks with missing 7d avg or missing perp data
    pass (no signal); picks with funding at-or-above floor pass.
    Annualization respects each coin's `funding_interval_hours` (4h /
    8h / etc.), restated 2026-06-03 — prior `× 3 × 365` math
    under-stated APR ~2× on 4h pairs and let some negative-funding
    coins slip the floor."""
    perp_market = getattr(snapshot, "perp_market", None) or {}
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
            if info is None or info.funding_rate_7d_avg is None:
                continue
            interval = (
                info.funding_interval_hours or DEFAULT_FUNDING_INTERVAL_HOURS
            )
            annual = _annual_funding(info.funding_rate_7d_avg, interval)
            if annual is None:
                # Annualization fails only when `interval <= 0` — that's a
                # broken snapshot, not a passing signal. Mirror the carry
                # validator's strict handling instead of silently skipping.
                bad.append(
                    f"{venue_id}/{pick.product_id}({coin}): perp_market "
                    f"funding_interval_hours={info.funding_interval_hours!r} "
                    "invalid — cannot annualize funding floor"
                )
                continue
            annual_f = float(annual)
            if annual_f < FUNDING_FLOOR_HEDGE_ANNUAL:
                bad.append(
                    f"{venue_id}/{pick.product_id}({coin}): 7d avg funding "
                    f"{float(info.funding_rate_7d_avg):+.6f}/{interval}h "
                    f"({annual_f * 100:+.3f}% annualized) below floor "
                    f"{FUNDING_FLOOR_HEDGE_ANNUAL * 100:+.3f}%/year — hedge net cost"
                )
    if bad:
        return False, (
            "non-USD Earn picks with negative 7d funding (exit "
            "required): " + " | ".join(bad)
        )
    return True, None


# ─── Funding-carry rules (`bybit-strategy-expansion.4`) ────────────────────


# Carry venue id. The pair `(venue_id, snapshot_category)` mirrors the
# hedge layer's `_AUTO_HEDGE_VENUES`, but carry is intentionally NOT in
# that tuple — its picks don't trigger auto-hedge (they ARE the hedge).
_CARRY_VENUE_ID: str = "bybit_funding_carry"
_CARRY_CATEGORY: str = "FundingCarry"


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
    venue_id = {
        "OnChain": "bybit_onchain",
        "FlexibleSaving": "bybit_flex",
    }.get(category)
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


def check_stable_spend_cap(d: Decision, snapshot: Snapshot) -> Check:
    """Reject decisions whose non-stable spend can't be funded by the
    liquid stable balance. Each non-stable Earn pick triggers two USD
    outflows the executor must satisfy from the stable pool:

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
            if pick_usd <= 0:
                continue
            perp_demand += pick_usd * _VALIDATOR_HEDGE_MARGIN_BUFFER
            spot_demand += pick_usd
            contributors.append(
                f"{venue_id}/{pick.product_id}({coin})=${pick_usd:.2f}"
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
            if pick_usd <= 0:
                continue
            perp_demand += pick_usd * _VALIDATOR_HEDGE_MARGIN_BUFFER
            spot_demand += pick_usd
            contributors.append(
                f"{_CARRY_VENUE_ID}/{pick.product_id}({coin})=${pick_usd:.2f}"
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
        check_lm_leverage_size_cap,
        check_hedges_for_non_usd_picks,
        check_funding_rate_floor,
        check_funding_carry_floor,
        check_no_double_carry_hedge,
        check_stable_spend_cap,
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
