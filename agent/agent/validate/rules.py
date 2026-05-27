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

# Hedging tolerance: |hedge.notional_usd| must be within ±HEDGE_SIZE_TOL
# of the matching pick's USD-equivalent. Loose enough to absorb mark-
# price drift between snapshot fetch and Earn settlement, tight enough
# to keep the combined position near delta-neutral.
HEDGE_SIZE_TOL = 0.20

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


def check_lm_no_leverage(d: Decision, snapshot: Snapshot) -> Check:
    """LM picks must have `max_leverage=1`. Leverage > 1 introduces
    liquidation risk and breaks the async-redeem SLO. `max_leverage`
    lives in the pick's `notes` as `max_leverage=<N>`."""
    lm = d.venue("bybit_lm")
    if lm is None or not lm.picks:
        return True, None
    lm_idx = _snapshot_index(snapshot).get("LiquidityMining", {})
    violations: list[str] = []
    for pick in lm.picks:
        summary = lm_idx.get(pick.product_id)
        if summary is None:
            continue  # already caught by check_product_ids_in_snapshot
        lev = _extract_max_leverage(summary)
        if lev is not None and lev > 1:
            violations.append(f"{pick.product_id}(max_leverage={lev})")
    if violations:
        return False, (
            f"LM picks with leverage > 1 not allowed: {', '.join(violations)}"
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


def check_hedges_for_non_usd_picks(d: Decision, snapshot: Snapshot) -> Check:
    """Non-stable OnChain picks MUST be paired with a perp hedge on the
    matching coin. LM picks are a paired LP (already self-hedged on the
    quote side), so they don't require a separate `Hedge` entry.
    """
    hedged_coins = {h.coin.upper() for h in d.hedges}
    onchain = d.venue("bybit_onchain")
    if onchain is None or not onchain.picks:
        return True, None
    idx = _snapshot_index(snapshot).get("OnChain", {})
    missing: list[str] = []
    for pick in onchain.picks:
        summary = idx.get(pick.product_id)
        if summary is None:
            continue
        coin = summary.coin.upper()
        if coin in _STABLE_COINS:
            continue
        if coin not in hedged_coins:
            missing.append(f"{pick.product_id}({coin})")
    if missing:
        return False, (
            f"non-USD OnChain picks need a Hedge entry on the matching "
            f"coin: {', '.join(missing)}"
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


def check_hedge_direction(d: Decision) -> Check:
    """Hedges must be SHORT — we hold the underlying via the Earn
    subscription, so the perp leg goes short to neutralize price delta.
    `notional_usd` is signed: negative = short. A non-negative hedge
    notional is either a long-on-long (doubled exposure) or a missing
    sign — the validator catches both.
    """
    bad: list[str] = []
    for h in d.hedges:
        if h.notional_usd >= 0:
            bad.append(f"{h.coin}={h.notional_usd:+.2f}")
    if bad:
        return False, (
            f"hedge notional_usd must be negative (short) to neutralize "
            f"stake exposure; got: {', '.join(bad)}"
        )
    return True, None


def check_hedge_sizing(d: Decision, snapshot: Snapshot) -> Check:
    """|hedge.notional_usd| must be within ±HEDGE_SIZE_TOL of the
    matching pick's USD-equivalent. Over-hedging takes on directional
    risk in the opposite direction; under-hedging leaves the pick
    partially exposed. Both are caught here so the prompt is forced to
    size the hedge against the actual book."""
    bad: list[str] = []
    for h in d.hedges:
        pick_usd = _pick_usd_value(d, snapshot, "OnChain", h.coin)
        if pick_usd <= 0:
            bad.append(f"{h.coin}: no matching non-zero OnChain pick")
            continue
        hedge_usd = abs(h.notional_usd)
        ratio = hedge_usd / pick_usd
        if ratio < 1.0 - HEDGE_SIZE_TOL or ratio > 1.0 + HEDGE_SIZE_TOL:
            bad.append(
                f"{h.coin}: hedge ${hedge_usd:.2f} vs pick ${pick_usd:.2f} "
                f"(ratio {ratio:.2f}, outside ±{HEDGE_SIZE_TOL:.0%})"
            )
    if bad:
        return False, (
            "hedge sizing outside tolerance band: " + " | ".join(bad)
        )
    return True, None


def check_hedge_min_notional(d: Decision, snapshot: Snapshot) -> Check:
    """A hedge below the perp pair's `min_notional_usd` can't actually
    be placed by the executor. Snapshot carries the floor per coin in
    `perp_market[coin].min_notional_usd`; missing entry → can't price
    the hedge → reject (fail-closed). The prompt is responsible for
    detecting these BEFORE submitting the pick (downsize the underlying
    Earn pick OR skip), but the validator catches anything that slips
    through.
    """
    bad: list[str] = []
    perp_market = getattr(snapshot, "perp_market", None) or {}
    for h in d.hedges:
        info = perp_market.get(h.coin) or perp_market.get(h.coin.upper())
        if info is None or info.min_notional_usd is None:
            bad.append(f"{h.coin}: no perp_market entry / min_notional unknown")
            continue
        if abs(h.notional_usd) < float(info.min_notional_usd):
            bad.append(
                f"{h.coin}: hedge ${abs(h.notional_usd):.2f} below "
                f"min_notional ${float(info.min_notional_usd):.2f}"
            )
    if bad:
        return False, (
            "hedges below perp min-notional / unsupported coin: "
            + " | ".join(bad)
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
        check_effective_pick_cap,
        check_confidence,
        check_risk_flags,
        check_hedge_direction,
    ]
    snapshot_checks = [
        check_peg_stress,
        check_product_ids_in_snapshot,
        check_no_missing_apr_source,
        check_lm_no_leverage,
        check_hedges_for_non_usd_picks,
        check_hedge_sizing,
        check_hedge_min_notional,
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
