"""Auto-extracted submodule (ah.25 execute split). See package __init__."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from agent.sandbox.execute.budget import (
    _preflight_spend_reserve,
)
from agent.sandbox.execute.common import (
    _FUNDING_SWAP_FEE_FACTOR,
    _STABLE_SWAP_HEADROOM,
    _STABLES,
    MIN_SWAP_USDC,
    _order_link_id,
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


def _swap_actions_for_earn_picks(
    snapshot: Snapshot,
    subscribe_actions: list[Action],
    redeem_actions: list[Action],
    snapshot_ts: str,
    *,
    idx_offset: int,
) -> list[Action]:
    """Plan USDC → pick.coin swaps when SUBSCRIBE_EARN or SUBSCRIBE_LM
    actions target coins the wallet doesn't carry. Bybit Earn stakes the
    product's base coin directly — there's no auto-conversion — so a
    USD1 pick against a USDC-only wallet would 180016 "Balance not
    enough". LM subscribes pay in the quote coin (USDT for most LM
    pairs, USDC for ETH/USDC and BTC/USDC); same problem applies. We
    pre-emptively swap each shortfall via the `USDC<coin>` spot pair
    (Sell USDC base, receive target quote).

    Skips:
      - USDC picks (source coin, no swap needed),
      - non-stable picks (this layer is for stables only; perp margin
        gets its own `_swap_actions_for_hedges`),
      - shortfalls below `MIN_SWAP_USDC` (Bybit pair fees > yield gain).

    Aggregated per coin so a 3-product split like USD1/USDT/FDUSD in
    one venue produces 3 distinct swaps (one per target coin), not
    one per pick.
    """
    # Split the demand by route:
    #   stable_demand_usdc  — USDCx pair (Sell USDC for the target
    #                          stable); qty is the USDC amount.
    #   nonstable_demand_usdt — {coin}USDT pair (Buy {coin} with USDT);
    #                          qty is the USDT amount to spend.
    # Both flow through SWAP_SPOT but with different `side` and a
    # different source coin → kept separate so the USDC budget pass
    # doesn't double-count non-stable spend.
    stable_demand: dict[str, Decimal] = {}
    nonstable_demand_usd: dict[str, Decimal] = {}
    usdc_demand = Decimal(0)
    for a in subscribe_actions:
        if a.kind not in (ActionKind.SUBSCRIBE_EARN, ActionKind.SUBSCRIBE_LM):
            continue
        coin = a.coin
        if coin is None:
            continue
        if coin == "USDC":
            # Funded from idle USDT below — historically skipped because
            # the wallet's base liquid coin was always USDC.
            usdc_demand += a.amount
        elif coin in _STABLES:
            stable_demand[coin] = stable_demand.get(coin, Decimal(0)) + a.amount
        elif a.amount_native is not None and a.amount_native > 0:
            # Non-stable: a.amount is USD, a.amount_native is native
            # coin qty. We'll Buy `coin` via {coin}USDT spending
            # `a.amount` worth of USDT.
            nonstable_demand_usd[coin] = (
                nonstable_demand_usd.get(coin, Decimal(0)) + a.amount
            )
    required_per_coin = stable_demand

    # Pending REDEEM_EARN actions return their coin to the wallet
    # in-cycle, so credit them against the requirement before sizing
    # any swap. Mirrors the `hedge_closes` credit in
    # `_swap_actions_for_hedges`. Without this we'd double-fund a
    # rebalance (e.g. redeem $13 USD1 then swap USDC → USDT to
    # subscribe USDT, while the USD1 just sits idle).
    #
    # `.63`: slow-settling (OnChain ~4d Processing) redeems are EXCLUDED —
    # their freed coin won't credit this cycle, so crediting it would
    # under-size the swap and the dependent subscribe would 180016 at
    # execution. Excluding them sizes the swap from real liquid; an
    # unfundable swap is then dropped by the budget pass, cascading the
    # dependent subscribe to a SKIP (deferred to the cycle the redeem
    # actually settles).
    redeem_credit_per_coin: dict[str, Decimal] = {}
    for a in redeem_actions:
        if a.kind != ActionKind.REDEEM_EARN or not _redeem_settles_in_cycle(a):
            continue
        redeem_credit_per_coin[a.coin] = (
            redeem_credit_per_coin.get(a.coin, Decimal(0)) + a.amount
        )

    swaps: list[Action] = []
    cursor = idx_offset

    # USDC subscribes funded from idle USDT (`bybit-sandbox.68`). Deposits
    # and USDT-coin Earn payouts land as USDT, so a USDC subscribe can
    # outrun the USDC balance. Fund the shortfall via a `USDCUSDT` Buy
    # (spend USDT, receive USDC) emitted FIRST — before the stable-Sell
    # swaps that spend USDC and before the subscribe itself. Pre-fix the
    # only USDT→USDC conversion was the post-subscribe excess sweep, which
    # fired a cycle too late and left the subscribe to 180016
    # (executed_partial). `side="Buy"` folds the USDT spend into
    # `buy_usdt_demand`, so the excess sweep no longer double-converts.
    # `usdc_have` nets out the USDC the same-cycle stable-Sell swaps will
    # consume so the subscribe isn't left a few dollars short. Only the
    # stables whose shortfall actually triggers a USDC→stable Sell count
    # (mirrors the loop below) — subtracting raw demand would over-fund
    # against stables the wallet already holds and could siphon USDT a
    # USDT subscribe needs.
    stable_usdc_spend = Decimal(0)
    for coin, need in stable_demand.items():
        bal = snapshot.wallet.unified_coin_balances.get(coin, Decimal(0))
        sf = need - bal - redeem_credit_per_coin.get(coin, Decimal(0))
        if sf >= MIN_SWAP_USDC:
            stable_usdc_spend += (sf * Decimal("1.01")).quantize(Decimal("0.01"))
    usdc_have = (
        snapshot.wallet.liquid_usdc_usd
        + redeem_credit_per_coin.get("USDC", Decimal(0))
        - stable_usdc_spend
    )
    usdc_shortfall = usdc_demand - usdc_have
    if (
        usdc_demand > 0
        and usdc_shortfall >= MIN_SWAP_USDC
        and snapshot.wallet.liquid_usdt_usd > 0
    ):
        qty_usdt = (usdc_shortfall * Decimal("1.01")).quantize(Decimal("0.01"))
        swaps.append(
            Action(
                kind=ActionKind.SWAP_SPOT,
                category="Spot",
                product_id="USDCUSDT",
                coin="USDC",  # destination coin of the swap
                amount=qty_usdt,  # USDT to spend — Bybit Buy uses quote qty
                side="Buy",
                order_link_id=_order_link_id(snapshot_ts, cursor),
                reason=(
                    f"buy {qty_usdt} USDC via USDCUSDT (Buy) for USDC Earn "
                    f"subscribe coverage (need ${usdc_demand:.2f}, have "
                    f"${usdc_have:.2f} after ${stable_usdc_spend:.2f} stable "
                    f"swaps)"
                ),
            )
        )
        cursor += 1

    for coin, need in required_per_coin.items():
        wallet_balance = snapshot.wallet.unified_coin_balances.get(coin, Decimal(0))
        redeem_inflow = redeem_credit_per_coin.get(coin, Decimal(0))
        available = wallet_balance + redeem_inflow
        shortfall = need - available
        if shortfall < MIN_SWAP_USDC:
            continue
        # 1% buffer for spot pair spread + Bybit lot-size rounding so
        # the SUBSCRIBE that follows has comfortable headroom.
        qty = (shortfall * Decimal("1.01")).quantize(Decimal("0.01"))
        symbol = f"USDC{coin}"
        swaps.append(
            Action(
                kind=ActionKind.SWAP_SPOT,
                category="Spot",
                product_id=symbol,
                coin=coin,  # target coin of the swap
                amount=qty,  # USDC to sell — Bybit Sell uses base-coin qty
                side="Sell",
                order_link_id=_order_link_id(snapshot_ts, cursor),
                reason=(
                    f"swap {qty} USDC → {coin} for Earn/LM subscribe coverage "
                    f"(need ${need:.2f}, have ${available:.2f})"
                ),
            )
        )
        cursor += 1

    # Non-stable Earn/LM picks (TON, ATOM, …) use Bybit's {coin}USDT
    # pair with side=Buy — spend USDT to acquire the target coin.
    # Bybit doesn't expose `USDC{coin}` pairs for these, so the route
    # is two-legged from a USDC accounting view: USDC→USDT via
    # _swap_actions_for_hedges (or transfer_satisfies_swap if FUND
    # already has USDT), then USDT→coin here.
    for coin, need_usd in nonstable_demand_usd.items():
        wallet_balance = snapshot.wallet.unified_coin_balances.get(coin, Decimal(0))
        # Convert wallet native balance to USD for the shortfall calc
        # using the same mark price the planner used.
        perp_info = (snapshot.perp_market or {}).get(coin)
        mark = getattr(perp_info, "mark_price", None) if perp_info else None
        if mark is None or mark <= 0:
            # Without a mark price we can't size the swap — skip,
            # subscribe will 180016 and be visible in cycle log.
            continue
        have_usd = wallet_balance * mark
        shortfall_usd = need_usd - have_usd
        if shortfall_usd < MIN_SWAP_USDC:
            continue
        # USDT to spend, with a 1% buffer for spread/slippage. Bybit
        # market Buy on {coin}USDT uses quote-coin qty (USDT).
        qty_usdt = (shortfall_usd * Decimal("1.01")).quantize(Decimal("0.01"))
        symbol = f"{coin}USDT"
        swaps.append(
            Action(
                kind=ActionKind.SWAP_SPOT,
                category="Spot",
                product_id=symbol,
                coin=coin,  # target coin we're acquiring
                amount=qty_usdt,  # USDT quote qty — side=Buy uses quote
                side="Buy",
                order_link_id=_order_link_id(snapshot_ts, cursor),
                reason=(
                    f"buy {coin} via {symbol} for Earn subscribe coverage "
                    f"(need ${need_usd:.2f} = {need_usd/mark:.4f} {coin} @ "
                    f"${mark:.4f}, have {wallet_balance} {coin})"
                ),
            )
        )
        cursor += 1
    return swaps


def _swap_actions_for_hedges(
    snapshot: Snapshot,
    hedge_opens: list[Action],
    hedge_closes: list[Action],
    snapshot_ts: str,
    *,
    idx_offset: int,
    extra_usdt_demand: Decimal = Decimal(0),
) -> list[Action]:
    """Plan a USDC → USDT spot swap to fund the cycle's USDT consumers
    (.33, extended 2026-06-03 to include non-stable Buy demand).

    Net USDT needed
        = sum(open notional × HEDGE_MARGIN_BUFFER)
          + extra_usdt_demand               # planned Buy swaps on {coin}USDT
          − snapshot.wallet.liquid_usdt_usd # UNIFIED + FUND (auto-transfer
                                            # at execute time)
          − sum(close notional)             # margin released by closes

    A `CLOSE_PERP` releases its IM back to UNIFIED as USDT, so we credit
    it against the requirement before sizing the swap. SKIP_OUT_OF_SCOPE
    hedge actions don't book real margin → excluded from the open side.

    `extra_usdt_demand` lets the planner consolidate perp margin and
    non-stable Buy swap demand into a single USDCUSDT conversion. Before
    this, each Buy swap relied on UNIFIED USDT being topped up
    incidentally by the perp-only hedge swap — but the perp consumed
    it before Buy ran, draining UNIFIED and triggering 170131.

    `liquid_usdt_usd` is used (vs the pre-fix `usdt_available_usd`
    UNIFIED-only) because `_ensure_unified_balance` auto-transfers
    FUND→UNIFIED at OPEN_PERP_SHORT and Buy SWAP_SPOT dispatch time, so
    FUND USDT is functionally available for both consumers.

    The swap uses Bybit's `USDCUSDT` spot pair with `side="Sell"` —
    sell USDC (base) for USDT (quote). `qty` is the USDC amount to
    sell, treated 1:1 with the USDT shortfall (stable pair, bps-level
    spread).

    Returns an empty list when:
      - no real OPEN actions AND no extra_usdt_demand,
      - existing USDT already covers the combined requirement,
      - the residual shortfall is below `MIN_SWAP_USDC`.
    """
    real_opens = [
        a for a in hedge_opens if a.kind == ActionKind.OPEN_PERP_SHORT
    ]
    if not real_opens and extra_usdt_demand <= 0:
        return []

    # `Action.amount` for OPEN_PERP_SHORT is in base coin (qty); the
    # USD notional was burned into `reason` but the cleanest source is
    # to re-derive it: qty × mark from snapshot.perp_market.
    open_notional = Decimal(0)
    for a in real_opens:
        info = snapshot.perp_market.get(a.coin) or snapshot.perp_market.get(
            a.coin.upper()
        )
        if info is None or info.mark_price is None or info.mark_price <= 0:
            # Should not happen — diff would have emitted SKIP, not OPEN.
            # Skip silently; the OPEN itself will fail loudly at execute time.
            continue
        open_notional += a.amount * info.mark_price

    close_notional = Decimal(0)
    for a in hedge_closes:
        info = snapshot.perp_market.get(a.coin) or snapshot.perp_market.get(
            a.coin.upper()
        )
        if info is None or info.mark_price is None or info.mark_price <= 0:
            continue
        close_notional += a.amount * info.mark_price

    required = open_notional * HEDGE_MARGIN_BUFFER + extra_usdt_demand
    # `liquid_usdt_usd` (UNIFIED + FUND) replaces the pre-fix UNIFIED-only
    # `usdt_available_usd`. FUND USDT is functionally available since
    # `_ensure_unified_balance` auto-transfers FUND→UNIFIED at dispatch
    # for both OPEN_PERP_SHORT (margin) and SWAP_SPOT Buy (quote spend).
    available = snapshot.wallet.liquid_usdt_usd + close_notional
    shortfall = required - available

    if shortfall < MIN_SWAP_USDC:
        return []

    # Size so the NET USDT (after the ~0.1% spot fee) covers the shortfall PLUS
    # the `.2` pre-flight spend reserve, then round UP. A flat 0.5% headroom
    # (`_STABLE_SWAP_HEADROOM`) is SMALLER than that reserve, so a pick funded
    # mostly by this swap cleared the budget cap but was then atomically skipped
    # by the pre-flight for leaving < reserve in UNIFIED — nuking exactly the
    # high-net hedged picks the book needs. Gross `shortfall + reserve` up by
    # the fee factor so a genuinely-fundable swap-funded pick survives; the
    # pre-flight then fires only when USDC itself can't cover this swap (true
    # insufficiency). Keep the old headroom as a floor so the prior sub-cent
    # over-convert is never undercut. The excess (~reserve) is < MIN_SWAP_USDC
    # so the USDT-excess sweep won't churn it back.
    reserve = _preflight_spend_reserve(required)
    qty = max(
        (shortfall + reserve) / _FUNDING_SWAP_FEE_FACTOR,
        shortfall * _STABLE_SWAP_HEADROOM,
    ).quantize(Decimal("0.01"), rounding=ROUND_UP)
    perp_part = open_notional * HEDGE_MARGIN_BUFFER
    return [
        Action(
            kind=ActionKind.SWAP_SPOT,
            category="Spot",
            product_id="USDCUSDT",
            coin="USDT",  # target coin of the swap
            amount=qty,  # USDC to sell — Bybit Sell uses base-coin qty
            order_link_id=_order_link_id(snapshot_ts, idx_offset),
            reason=(
                f"swap {qty} USDC → USDT: USDT demand "
                f"${required:.2f} (perp margin ${perp_part:.2f} with "
                f"{HEDGE_MARGIN_BUFFER:.0%} buffer + non-stable Buy "
                f"${extra_usdt_demand:.2f}) - liquid USDT "
                f"${snapshot.wallet.liquid_usdt_usd:.2f} - closes "
                f"${close_notional:.2f}"
            ),
        )
    ]


def _swap_actions_for_usdt_excess(
    snapshot: Snapshot,
    hedge_opens: list[Action],
    hedge_closes: list[Action],
    subscribes: list[Action],
    snapshot_ts: str,
    *,
    idx_offset: int,
    extra_usdt_demand: Decimal = Decimal(0),
) -> list[Action]:
    """Mirror of `_swap_actions_for_hedges` (`.60`): when post-cycle USDT
    supply exceeds demand by ≥ `MIN_SWAP_USDC`, sweep the excess back to
    USDC via a single `USDCUSDT` Buy (qty_quote=USDT amount).

    USDT accumulates on the sub-account after USDT-denominated Earn
    payouts (FlexibleSaving USDT, advance-Earn USDT settlements that
    didn't knockout) and sits idle unless manually swept. `.49` covered
    BTC/ETH/SOL → USDC direct routing for orphan spot post-DiscountBuy;
    this is the symmetric stable-side case.

    Excess
        = snapshot.wallet.liquid_usdt_usd
          + sum(close notional)               # margin released by closes
          − sum(open notional × HEDGE_MARGIN_BUFFER)   # perp margin demand
          − extra_usdt_demand                 # planned Buy swaps on {coin}USDT
          − sum(USDT-stable subscribe amount) # USDT consumed by stable subs

    By construction this is the negation of `_swap_actions_for_hedges`'s
    shortfall: when that function emits a swap, excess is ≤ 0 and we
    no-op; when it returns empty AND there's leftover USDT, we sweep.

    The sweep uses Bybit's `USDCUSDT` pair with `side="Buy"`. Bybit Spot
    Buy uses quote-coin qty, so `amount` is the USDT amount to spend
    (treated 1:1 with USDC received — stable pair, bps-level spread).
    Same pair as the hedge swap so liquidity is known good.

    Returns an empty list when:
      - the residual excess is below `MIN_SWAP_USDC`,
      - excess is non-positive (demand ≥ supply).

    Not accounted for (deliberate MVP scope; under-sweep is safe, the
    next cycle re-evaluates):
      - `_orphan_spot_sell_actions` USDT inflow for non-`.49`-whitelist
        coins (TON, LIT, …) → under-sweep.
      - Funding-carry OPEN consuming USDT margin → could over-sweep if
        carry runs in the same cycle. Rare co-occurrence; tracked.
    """
    open_notional = Decimal(0)
    for a in hedge_opens:
        if a.kind != ActionKind.OPEN_PERP_SHORT:
            continue
        info = snapshot.perp_market.get(a.coin) or snapshot.perp_market.get(
            a.coin.upper()
        )
        if info is None or info.mark_price is None or info.mark_price <= 0:
            continue
        open_notional += a.amount * info.mark_price

    close_notional = Decimal(0)
    for a in hedge_closes:
        if a.kind != ActionKind.CLOSE_PERP:
            continue
        info = snapshot.perp_market.get(a.coin) or snapshot.perp_market.get(
            a.coin.upper()
        )
        if info is None or info.mark_price is None or info.mark_price <= 0:
            continue
        close_notional += a.amount * info.mark_price

    usdt_subscribe_demand = sum(
        (
            a.amount
            for a in subscribes
            if a.kind in (ActionKind.SUBSCRIBE_EARN, ActionKind.SUBSCRIBE_LM)
            and (a.coin or "").upper() == "USDT"
        ),
        Decimal(0),
    )

    perp_margin = open_notional * HEDGE_MARGIN_BUFFER
    required = perp_margin + extra_usdt_demand + usdt_subscribe_demand
    available = snapshot.wallet.liquid_usdt_usd + close_notional
    excess = available - required

    if excess < MIN_SWAP_USDC:
        return []

    # Under-size the sweep so it never over-reaches the USDT actually
    # spendable in UNIFIED at dispatch. The snapshot's `liquid_usdt` (and a
    # same-cycle close release) can read a hair above dispatch-time UNIFIED —
    # FUND dust that can't transfer, freshly-released margin not yet settled,
    # balance precision. Over-sweeping 170131s the Buy and strands the cycle
    # as executed_partial (live 2026-06-08: sized $13.11 vs $13.01 in UNIFIED).
    # Under-sweeping is SAFE — the residual is swept next cycle. Reserve
    # max(1%, $0.20) and ROUND_DOWN; skip if the reserved amount drops below
    # the pair's min order.
    reserve = max(excess * Decimal("0.01"), Decimal("0.20"))
    qty = (excess - reserve).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if qty < MIN_SWAP_USDC:
        return []
    return [
        Action(
            kind=ActionKind.SWAP_SPOT,
            category="Spot",
            product_id="USDCUSDT",
            coin="USDC",  # destination coin of the swap
            amount=qty,  # USDT to spend — Bybit Buy uses quote-coin qty
            side="Buy",
            order_link_id=_order_link_id(snapshot_ts, idx_offset),
            reason=(
                f"sweep {qty} USDT → USDC: liquid USDT "
                f"${snapshot.wallet.liquid_usdt_usd:.2f} + closes "
                f"${close_notional:.2f} - perp margin "
                f"${perp_margin:.2f} (with {HEDGE_MARGIN_BUFFER:.0%} "
                f"buffer) - non-stable Buy ${extra_usdt_demand:.2f} - "
                f"USDT subscribes ${usdt_subscribe_demand:.2f}"
            ),
        )
    ]
