"""Auto-extracted submodule (ah.25 execute split). See package __init__."""

from __future__ import annotations

import logging
from decimal import Decimal

from agent.reason.schema import Decision
from agent.reason.venues import (
    CARRY_CATEGORY,
    CARRY_VENUE_ID,
)
from agent.sandbox.carry_state import (
    CarryState,
)
from agent.sandbox.execute.common import (
    _CARRY_OPEN_USDT_FACTOR,
    _FUNDING_SWAP_FEE_FACTOR,
    _STABLES,
    _UNIFIED_SPEND_RESERVE_FACTOR,
    _UNIFIED_SPEND_RESERVE_FLOOR,
    MIN_ACTION_USDC,
    MIN_SWAP_USDC,
    _coin_mark,
    _redeem_settles_in_cycle,
)
from agent.sandbox.execute.types import (
    Action,
    ActionKind,
)
from agent.sandbox.snapshot import (
    HEDGE_MARGIN_BUFFER,
    Snapshot,
)

log = logging.getLogger(__name__)




def _buy_usdt_demand(earn_swaps: list[Action]) -> Decimal:
    """USDT consumed by the non-stable Earn Buy legs ({coin}USDT Buy).

    Single source of truth for the consolidated USDT-swap sizer and the
    USDT-budget cap (`dispatch-1` root cause — pre-ah.25 this `sum()` was
    re-derived inline at every cascade pass inside `diff_to_actions`).
    """
    return sum((a.amount for a in earn_swaps if a.side == "Buy"), Decimal(0))


def _funding_carry_targets(
    decision: Decision,
    snapshot: Snapshot,
    total_book_usd: Decimal,
) -> dict[str, Decimal]:
    """Derive `{coin: target_pick_usd}` from `bybit_funding_carry` picks.
    Mirrors `_auto_hedge_targets` but reads the FundingCarry category
    instead of OnChain/Flex. Empty when the venue isn't picked or has
    no picks; coin keys are uppercase per executor convention.
    """
    venue = decision.venue(CARRY_VENUE_ID)  # type: ignore[arg-type]
    if venue is None or venue.weight <= 0 or not venue.picks:
        return {}
    carry_products = {
        p.product_id: p
        for p in snapshot.products.get(CARRY_CATEGORY, [])
    }
    targets: dict[str, Decimal] = {}
    for pick in venue.picks:
        summary = carry_products.get(pick.product_id)
        if summary is None:
            continue
        coin = summary.coin.upper()
        if not coin:
            continue
        pick_usd = total_book_usd * Decimal(str(venue.weight)) * Decimal(str(pick.weight))
        if pick_usd <= 0:
            continue
        targets[coin] = targets.get(coin, Decimal(0)) + pick_usd
    return targets


def _carry_open_usdc_reserve(
    decision: Decision,
    snapshot: Snapshot,
    carry_state: CarryState,
    total_book_usd: Decimal,
) -> Decimal:
    """USDC the USDC-budget pass must withhold for NEW funding-carry opens.

    A NEW carry OPEN funds both legs from USDT it mints via a `USDCUSDT` Sell
    at dispatch time (`_fund_carry_open_usdt`, sized `pick_usd *
    _CARRY_OPEN_USDT_FACTOR`). That Sell draws the SAME liquid-USDC pool the
    earn Sell-swaps spend, but `_enforce_usdc_budget` only saw hedge + earn
    swaps — so on a tight USDC-only vault the earn swaps over-allocate and the
    carry swap 170131s at dispatch (`dispatch-1` / ah.6 residual).

    Reserve an upper bound: every NEW open's full two-leg USDT need, treating
    USDC≈USDT at peg. Mirrors `_funding_carry_diff`'s NEW-open predicate
    (target ≥ MIN_ACTION_USDC, coin not already held, valid perp mark) so it
    never reserves for a carry that won't actually open.
    """
    targets = _funding_carry_targets(decision, snapshot, total_book_usd)
    if not targets:
        return Decimal(0)
    held = {p.coin.upper() for p in carry_state.positions}
    perp_market = getattr(snapshot, "perp_market", None) or {}
    reserve = Decimal(0)
    for coin, target in targets.items():
        if coin in held or target < MIN_ACTION_USDC:
            continue
        info = perp_market.get(coin) or perp_market.get(coin.lower())
        if info is None or info.mark_price is None or info.mark_price <= 0:
            continue
        reserve += target * _CARRY_OPEN_USDT_FACTOR
    return reserve


def _enforce_usdc_budget(
    liquid_usdc: Decimal,
    hedge_swaps: list[Action],
    earn_swaps: list[Action],
) -> tuple[list[Action], set[str]]:
    """Cap USDC-spending swap demand at `liquid_usdc`. Only Sell swaps
    on the USDC-base pairs (USDCUSDT, USDCUSD1, …) charge USDC; Buy
    swaps on {coin}USDT pairs charge USDT and are sized off the
    separate USDT budget elsewhere — they don't compete with the USDC
    cap. Hedge swaps are priority-1 (perp margin is risk-critical);
    earn swaps that overflow get dropped from the tail. Returns the
    (possibly pruned) earn_swaps list plus the set of target coins
    whose USDC-side swap was dropped."""
    if liquid_usdc <= 0:
        return earn_swaps, set()
    # Buy swaps spend USDT, not USDC — let them through regardless of
    # USDC budget. They keep their slot in the returned earn_swaps
    # list so the dispatch order is preserved.
    buy_swaps = [a for a in earn_swaps if a.side == "Buy"]
    sell_swaps = [a for a in earn_swaps if a.side != "Buy"]

    hedge_demand = sum(
        (a.amount for a in hedge_swaps if a.side != "Buy"), Decimal(0)
    )
    remaining = liquid_usdc - hedge_demand
    if remaining <= 0:
        dropped = {a.coin for a in sell_swaps}
        if sell_swaps:
            log.warning(
                "usdc_budget: hedge demand $%s ≥ liquid USDC $%s — "
                "dropping all %d USDC-side earn swap(s) for: %s",
                hedge_demand, liquid_usdc, len(sell_swaps),
                ", ".join(sorted(dropped)),
            )
        return buy_swaps, dropped
    kept_sell: list[Action] = []
    dropped: set[str] = set()
    spent = Decimal(0)
    for a in sell_swaps:
        if spent + a.amount <= remaining:
            kept_sell.append(a)
            spent += a.amount
        else:
            dropped.add(a.coin)
            log.warning(
                "usdc_budget: drop swap USDC→%s ($%s) — would exceed "
                "remaining budget $%s (already spent $%s on earn, "
                "$%s on hedges, of $%s liquid)",
                a.coin, a.amount, remaining - spent, spent, hedge_demand,
                liquid_usdc,
            )
    return kept_sell + buy_swaps, dropped


def _preflight_spend_reserve(demand: Decimal) -> Decimal:
    """USDT buffer the `.2` pre-flight withholds (and the funding swap
    pre-funds) to absorb snapshot-vs-dispatch UNIFIED drift — spot fee, FUND
    dust, unsettled margin, 2dp rounding: `max(1%, $0.20)` of the two-leg
    `demand`. Computed on the SAME demand `_swap_actions_for_hedges` sizes the
    funding swap against, so a genuinely-fundable swap-funded pick clears the
    pre-flight instead of being skipped for leaving < reserve in UNIFIED."""
    return max(
        demand * _UNIFIED_SPEND_RESERVE_FACTOR, _UNIFIED_SPEND_RESERVE_FLOOR
    )


def _usdt_supply(
    liquid_usdt: Decimal,
    hedge_swaps: list[Action],
    hedge_closes: list[Action],
    snapshot: Snapshot,
    *,
    liquid_usdc: Decimal | None = None,
) -> Decimal:
    """Spendable UNIFIED USDT this cycle = existing liquid USDT + fee-haircut
    USDC→USDT hedge swap inflow + CLOSE_PERP margin releases. Single source
    of the USDT-supply notion shared by `_enforce_usdt_budget` (the budget
    cap) and `_hedged_pick_underfunded_coins` (`.2` pre-flight) so the two
    can never drift on what counts as available USDT.

    `liquid_usdc` (pre-flight only) bounds the hedge-swap inflow by the USDC
    actually available to sell: the funding swap is never capped by the USDC
    budget (`_enforce_usdc_budget` treats it as priority-1 and short-circuits
    when `liquid_usdc<=0`), so without this bound the pre-flight would count
    USDT from a swap that can't execute for lack of USDC — passing a pick that
    then 170131s on the swap leg. The hedge swap has first claim on USDC (perp
    margin is risk-critical), so it gets the full `min(swap, liquid_usdc)`.
    `_enforce_usdt_budget` passes `None` (looser, USDC-unaware); the pre-flight
    is the strict final gate."""
    hedge_swap_inflow = sum(
        (
            s.amount
            for s in hedge_swaps
            if s.kind == ActionKind.SWAP_SPOT
            and s.product_id == "USDCUSDT"
            and s.side != "Buy"
        ),
        Decimal(0),
    )
    if liquid_usdc is not None:
        hedge_swap_inflow = min(hedge_swap_inflow, liquid_usdc)
    close_release = Decimal(0)
    for a in hedge_closes:
        info = snapshot.perp_market.get(a.coin) or snapshot.perp_market.get(
            a.coin.upper()
        )
        if info is None or info.mark_price is None or info.mark_price <= 0:
            continue
        close_release += a.amount * info.mark_price
    return (
        liquid_usdt
        + hedge_swap_inflow * _FUNDING_SWAP_FEE_FACTOR
        + close_release
    )


def _enforce_usdt_budget(
    liquid_usdt: Decimal,
    hedge_swaps: list[Action],
    hedge_opens: list[Action],
    hedge_closes: list[Action],
    earn_swaps: list[Action],
    snapshot: Snapshot,
) -> tuple[list[Action], set[str]]:
    """Cap total USDT-spending demand at `liquid_usdt` (UNIFIED+FUND).
    USDT is consumed by:
      - OPEN_PERP_SHORT margin (UNIFIED USDT) — priority-1, risk-critical
      - SWAP_SPOT Buy on {coin}USDT pairs (non-stable Earn picks) — drop-tail
    USDT is supplied by:
      - existing wallet (`liquid_usdt`)
      - USDC→USDT hedge swap inflow (USDCUSDT Sell, side != "Buy")
      - CLOSE_PERP releases (margin returns as USDT)
    Returns the (possibly pruned) earn_swaps list + set of target coins
    whose Buy swap was dropped (so caller cascades to subscribes/perps).
    Sell swaps on USDCx pairs are left untouched — they spend USDC, not
    USDT, and were already capped by `_enforce_usdc_budget`."""
    # Mirror of `_enforce_usdc_budget`: when the snapshot didn't populate
    # liquid_usdt (legacy callers / tests / pre-pivot fixtures), skip the
    # cap and fall back to the pre-budget behavior of letting the Buy
    # swap 170131 at runtime. Production always populates the field.
    if liquid_usdt <= 0:
        return earn_swaps, set()

    # Supply: existing USDT + fee-haircut hedge USDC→USDT swap inflow +
    # close releases. Shared with the `.2` pre-flight via `_usdt_supply`.
    supply = _usdt_supply(liquid_usdt, hedge_swaps, hedge_closes, snapshot)

    # Demand: perp margin (with buffer).
    perp_demand = Decimal(0)
    for a in hedge_opens:
        if a.kind != ActionKind.OPEN_PERP_SHORT:
            continue
        info = snapshot.perp_market.get(a.coin) or snapshot.perp_market.get(
            a.coin.upper()
        )
        if info is None or info.mark_price is None or info.mark_price <= 0:
            continue
        perp_demand += a.amount * info.mark_price * HEDGE_MARGIN_BUFFER

    buy_swaps = [a for a in earn_swaps if a.side == "Buy"]
    other_swaps = [a for a in earn_swaps if a.side != "Buy"]

    if not buy_swaps:
        return earn_swaps, set()

    remaining = supply - perp_demand
    if remaining <= 0:
        dropped = {a.coin for a in buy_swaps}
        log.warning(
            "usdt_budget: perp margin demand $%s ≥ USDT supply $%s "
            "(liquid $%s + fee-haircut hedge swap + close release) — "
            "dropping all %d non-stable Buy swap(s) for: %s",
            perp_demand, supply, liquid_usdt,
            len(buy_swaps), ", ".join(sorted(dropped)),
        )
        return other_swaps, dropped

    kept_buy: list[Action] = []
    dropped: set[str] = set()
    spent = Decimal(0)
    for a in buy_swaps:
        if spent + a.amount <= remaining:
            kept_buy.append(a)
            spent += a.amount
        else:
            dropped.add(a.coin)
            log.warning(
                "usdt_budget: drop Buy swap %s ($%s) — would exceed "
                "remaining USDT budget $%s (already spent $%s on Buy, "
                "$%s on perp margin, of $%s supply)",
                a.product_id, a.amount, remaining - spent, spent,
                perp_demand, supply,
            )
    return other_swaps + kept_buy, dropped


def _buy_usd_for_coin(earn_swaps: list[Action], coin: str) -> Decimal:
    """USD (≈USDT) value the cycle's emitted `{coin}USDT` Buy swap(s) deliver
    for `coin`. The Buy `amount` is the USDT quote spend, which is the
    coin's funded coverage from the spot leg. Shared between the `.2`
    pre-flight (Buy demand against the USDT pool) and the `.3` planner guard
    (Buy contribution to per-coin coverage) so both read the same number."""
    return sum(
        (
            a.amount
            for a in earn_swaps
            if a.kind == ActionKind.SWAP_SPOT
            and a.side == "Buy"
            and a.coin == coin
            and a.product_id == f"{coin}USDT"
        ),
        Decimal(0),
    )


def _hedged_pick_underfunded_coins(
    snapshot: Snapshot,
    hedge_opens: list[Action],
    hedge_closes: list[Action],
    hedge_swaps: list[Action],
    earn_swaps: list[Action],
) -> set[str]:
    """`.2` pre-flight: per-coin fully-fund-or-skip guarantee for hedged
    non-stable picks. After the USDC+USDT budget cascades have run, the
    spendable UNIFIED USDT (`_usdt_supply`, less a reserve) must cover BOTH
    legs of every hedged pick — the perp margin AND the paired `{coin}USDT`
    Buy. The two per-currency budget passes can drop just the Buy leg while
    leaving the perp + funding swap, stranding the funding swap on a
    retCode=170131 (43% of prod cycles went executed_partial this way).

    Allocation is priority-ordered against `reserved_supply`: ALL perp
    margins first (risk-critical), then the Buy legs. A coin whose margin
    fits but whose Buy can not be fully covered afterward is BINARY-skipped
    (the whole coin), since a half-funded hedged pick is the partial-exec
    failure mode we're eliminating. A missing / non-positive mark makes the
    coin unfundable (conservative — can't size the margin).

    Returns the set of coins to drop (subscribe + Buy + perp). Pure: caller
    converts the actions and re-sizes the funding swap.
    """
    open_coins = [
        a.coin for a in hedge_opens if a.kind == ActionKind.OPEN_PERP_SHORT
    ]
    if not open_coins:
        return set()
    # Mirror the `_enforce_usdt_budget` early-out: an unpopulated
    # `liquid_usdt` (legacy callers / tests / pre-pivot fixtures) means the
    # budget pass was a no-op, so the pre-flight can't reason about UNIFIED
    # either — fall back to the optimistic let-it-ride behavior. Production
    # always populates the field.
    if snapshot.wallet.liquid_usdt_usd <= 0:
        return set()

    supply = _usdt_supply(
        snapshot.wallet.liquid_usdt_usd,
        hedge_swaps,
        hedge_closes,
        snapshot,
        liquid_usdc=snapshot.wallet.liquid_usdc_usd,
    )

    # Per-coin two-leg demand. Mark missing → unfundable up front.
    margins: dict[str, Decimal] = {}
    buys: dict[str, Decimal] = {}
    unfunded: set[str] = set()
    for coin in open_coins:
        mark = _coin_mark(snapshot, coin)
        if mark is None:
            unfunded.add(coin)
            continue
        # Native qty for the coin's open(s) × mark × buffer.
        qty = sum(
            (
                a.amount
                for a in hedge_opens
                if a.kind == ActionKind.OPEN_PERP_SHORT and a.coin == coin
            ),
            Decimal(0),
        )
        margins[coin] = qty * mark * HEDGE_MARGIN_BUFFER
        buys[coin] = _buy_usd_for_coin(earn_swaps, coin)

    # Reserve relative to the two-leg demand being funded (NOT raw supply): the
    # funding swap pre-funds exactly this buffer on the same basis
    # (`_swap_actions_for_hedges`), so a fundable swap-funded pick clears the
    # gate. Reserving against supply instead would skip every pick funded
    # mostly by the swap (supply ≈ demand there), nuking the high-net hedged
    # picks the book exists to harvest.
    total_demand = sum(margins.values(), Decimal(0)) + sum(
        buys.values(), Decimal(0)
    )
    reserved_supply = supply - _preflight_spend_reserve(total_demand)

    # Priority pass 1: reserve every perp margin (risk-critical) before any
    # Buy leg. A coin whose margin alone overflows is unfundable.
    avail = reserved_supply
    for coin, margin in margins.items():
        if margin <= avail:
            avail -= margin
        else:
            unfunded.add(coin)
    # Priority pass 2: fund the Buy legs of coins whose margin cleared, from
    # the residual. A Buy that can't be fully covered drops its whole coin.
    for coin, buy in buys.items():
        if coin in unfunded:
            continue
        if buy <= avail:
            avail -= buy
        else:
            unfunded.add(coin)
    return unfunded


def _unfunded_nonstable_subscribe_coins(
    snapshot: Snapshot,
    subscribes: list[Action],
    earn_swaps: list[Action],
    redeems: list[Action],
) -> set[str]:
    """`.3` planner guard: per-coin coverage for non-stable Earn/LM
    subscribes that have NO funded spot path. A non-stable subscribe whose
    funded coverage falls short of the required amount (or whose mark is
    missing) would 180016 live, leaving the paired perp a naked short.

    Funded coverage (USD) per coin
        = current native wallet balance × mark
          + emitted `{coin}USDT` Buy in `earn_swaps`
          + in-cycle (fast-settling) REDEEM_EARN credit

    Mirrors `_redeem_settles_in_cycle`: slow OnChain redeems don't credit
    this cycle. A coin is unfunded when `required − coverage ≥ MIN_SWAP_USDC`
    (the same gap the swap planner needs before it would emit a Buy, so a
    shortfall too small to swap is NOT flagged — avoids over-skipping a pick
    that the existing native balance already covers) OR the mark is missing.

    Returns the set of non-stable coins to drop. Pure: caller cascades the
    subscribe + paired perp.
    """
    redeem_credit: dict[str, Decimal] = {}
    for a in redeems:
        if a.kind != ActionKind.REDEEM_EARN or not _redeem_settles_in_cycle(a):
            continue
        redeem_credit[a.coin] = redeem_credit.get(a.coin, Decimal(0)) + a.amount

    required: dict[str, Decimal] = {}
    for a in subscribes:
        if a.kind not in (ActionKind.SUBSCRIBE_EARN, ActionKind.SUBSCRIBE_LM):
            continue
        coin = a.coin
        if not coin or coin == "USDC" or coin in _STABLES:
            continue
        required[coin] = required.get(coin, Decimal(0)) + a.amount

    unfunded: set[str] = set()
    for coin, need in required.items():
        mark = _coin_mark(snapshot, coin)
        if mark is None:
            unfunded.add(coin)
            continue
        native = snapshot.wallet.unified_coin_balances.get(coin, Decimal(0))
        coverage = (
            native * mark
            + _buy_usd_for_coin(earn_swaps, coin)
            + redeem_credit.get(coin, Decimal(0))
        )
        if need - coverage >= MIN_SWAP_USDC:
            unfunded.add(coin)
    return unfunded
