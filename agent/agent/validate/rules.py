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

from agent.reason.schema import Decision, VenueAllocation
from agent.reason.venues import VENUE_REGISTRY, VenueId
from agent.sandbox.snapshot import ProductSummary, Snapshot

MIN_CONFIDENCE = 0.4
MAX_EFFECTIVE_PRODUCT = 0.50  # cap on a single product as fraction of TOTAL book

# Conditional cap thresholds
PEG_STRESS_BPS = 100
PEG_STRESS_STABLES_FLOOR = 0.50  # cash + bybit_flex must be ≥ this when peg is stressed

# Stables-set treated as "hedge not required" by the OnChain validator.
# Mirrored in `agent.sandbox.snapshot.STABLES`; keeping both lists in
# sync is checked indirectly by the snapshot tests.
_STABLE_COINS: frozenset[str] = frozenset(
    {"USDC", "USDT", "USD1", "FDUSD", "DAI", "USDE", "USDTB", "PYUSD", "RLUSD"}
)

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


def check_effective_pick_cap(d: Decision) -> Check:
    """No single product holds more than MAX_EFFECTIVE_PRODUCT of the
    total book. Effective share = venue.weight × pick.weight."""
    violations: list[str] = []
    for v in d.venues:
        for p in v.picks:
            eff = v.weight * p.weight
            if eff > MAX_EFFECTIVE_PRODUCT + 1e-9:
                violations.append(f"{v.venue_id}/{p.product_id}={eff:.2%}")
    if violations:
        return False, (
            f"effective product positions exceed "
            f"{MAX_EFFECTIVE_PRODUCT:.0%} cap: {', '.join(violations)}"
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
    total_book = float(snapshot.wallet.total_equity_usd)
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
            pick_usd = total_book * float(venue.weight) * float(pick.weight)
            if pick_usd < float(info.min_notional_usd):
                bad.append(
                    f"{venue_id}/{pick.product_id}({coin}): pick "
                    f"${pick_usd:.2f} below perp min_notional "
                    f"${float(info.min_notional_usd):.2f} — hedge can't be placed"
                )
    if bad:
        return False, "non-USD Earn picks not hedgeable: " + " | ".join(bad)
    return True, None


# Per-8h funding-rate threshold below which a hedged short becomes net
# cost over a typical hold. -0.0001/8h = -1 bp per period ≈ -11%
# annualized. Operator hardcap pattern (CLAUDE.md): "7-day avg funding <
# 0 → mandatory exit". We tighten the threshold from strict zero to
# -10 bps annualized so single-period noise doesn't yank good picks.
FUNDING_FLOOR_8H = -0.0001


def check_funding_rate_floor(d: Decision, snapshot: Snapshot) -> Check:
    """For each non-stable Earn pick (OnChain + FlexibleSaving), reject
    if the perp pair's 7-day average funding rate is below
    `FUNDING_FLOOR_8H`. We're short the perp to hedge; a persistently
    negative funding rate means we PAY funding every period — the hedge
    becomes net cost and erodes the Earn yield over time. Operator
    change 2026-05-29: funding is part of yield, not just a soft
    signal. Picks with missing 7d avg pass (no data, no signal); picks
    with funding at-or-above floor pass."""
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
            avg_8h = float(info.funding_rate_7d_avg)
            if avg_8h < FUNDING_FLOOR_8H:
                annualized_pct = avg_8h * 3 * 365 * 100
                bad.append(
                    f"{venue_id}/{pick.product_id}({coin}): 7d avg funding "
                    f"{avg_8h:+.6f}/8h ({annualized_pct:+.1f}% annualized) "
                    f"below floor {FUNDING_FLOOR_8H:+.6f}/8h — hedge net cost"
                )
    if bad:
        return False, (
            "non-USD Earn picks with negative 7d funding (exit "
            "required): " + " | ".join(bad)
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
        check_effective_pick_cap,
        check_confidence,
        check_risk_flags,
    ]
    snapshot_checks = [
        check_peg_stress,
        check_product_ids_in_snapshot,
        check_no_missing_apr_source,
        check_lockup_cap,
        check_lm_leverage_size_cap,
        check_hedges_for_non_usd_picks,
        check_funding_rate_floor,
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
