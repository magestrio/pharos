"""Auto-extracted submodule (ah.25 execute split). See package __init__."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from agent.reason.schema import Decision
from agent.sandbox.carry_state import (
    CarryState,
)
from agent.sandbox.execute.budget import (
    _buy_usdt_demand,
    _carry_open_usdc_reserve,
    _enforce_usdc_budget,
    _enforce_usdt_budget,
    _hedged_pick_underfunded_coins,
    _unfunded_nonstable_subscribe_coins,
)
from agent.sandbox.execute.builders import (
    _advance_earn_positions_held,
    _advance_earn_subscribe_action,
    _alpha_action_for_target,
    _funding_carry_diff,
    _hedge_diff_actions,
    _lm_action_for_target,
)
from agent.sandbox.execute.common import (
    _ADVANCE_EARN_CATEGORIES,
    _ALPHA_CATEGORY,
    _BASIC_EARN_CATEGORIES,
    _LM_CATEGORY,
    _STABLES,
    MIN_ACTION_USDC,
    _earn_product_lookup,
    _is_fully_processing,
    _liquid_for_coin,
    _order_link_id,
    _redeem_settles_in_cycle,
)
from agent.sandbox.execute.positions import (
    _alpha_current_positions,
    _current_positions_by_pid,
    _target_usd_by_pid,
)
from agent.sandbox.execute.swaps import (
    _swap_actions_for_earn_picks,
    _swap_actions_for_hedges,
    _swap_actions_for_usdt_excess,
)
from agent.sandbox.execute.sweep import (
    _close_naked_perp_actions,
    _orphan_spot_sell_actions,
    _reconcile_hedge_to_earn_actions,
)
from agent.sandbox.execute.types import (
    Action,
    ActionKind,
)
from agent.sandbox.snapshot import (
    Snapshot,
)


def _defer_subscribes_awaiting_slow_redeem(
    snapshot: Snapshot,
    subscribes: list[Action],
    redeems: list[Action],
    earn_swaps: list[Action],
) -> tuple[list[Action], set[str]]:
    """`bybit-sandbox.63`: defer (→ SKIP) the SUBSCRIBE_EARNs that can only
    be funded by a SLOW-settling same-coin redeem (OnChain ~4d Processing),
    whose freed coin won't credit this cycle.

    Fires ONLY for coins that have a slow pending redeem — so a normal cycle
    (no OnChain redeem) is completely untouched. Per coin, fund the net-new
    subscribes from `liquid + in-cycle (fast) redeems + funding-swap inflow`
    smallest-first; the overflow is deferred (it re-appears next cycle once
    the redeem credits) instead of 180016'ing at execution and leaving the
    capital suspended. The cross-coin swap-funded case is already handled by
    the budget cascade (slow redeems are excluded from `redeem_credit` when
    sizing swaps); this is the same-coin direct-funding complement.

    Returns `(subscribes, deferred_coins)`.
    """
    slow_by_coin: dict[str, Decimal] = {}
    fast_by_coin: dict[str, Decimal] = {}
    for a in redeems:
        if a.kind != ActionKind.REDEEM_EARN or not a.coin:
            continue
        bucket = fast_by_coin if _redeem_settles_in_cycle(a) else slow_by_coin
        bucket[a.coin] = bucket.get(a.coin, Decimal(0)) + a.amount
    if not slow_by_coin:
        return subscribes, set()

    # Funding-swap inflow per target coin (swaps were sized excluding slow
    # redeems, so this is real liquid-backed supply). Treat the swap amount
    # as ~USD inflow of its target coin — exact for stables, ≈ for the
    # USDT-quoted non-stable Buy (USD value of coin received).
    swap_in_by_coin: dict[str, Decimal] = {}
    for a in earn_swaps:
        if a.kind == ActionKind.SWAP_SPOT and a.coin:
            swap_in_by_coin[a.coin] = (
                swap_in_by_coin.get(a.coin, Decimal(0)) + a.amount
            )

    # Only coins with a slow pending redeem are at risk.
    subs_by_coin: dict[str, list[Action]] = {}
    for a in subscribes:
        if (
            a.kind == ActionKind.SUBSCRIBE_EARN
            and a.coin in slow_by_coin
            and a.amount > 0
        ):
            subs_by_coin.setdefault(a.coin, []).append(a)
    if not subs_by_coin:
        return subscribes, set()

    to_defer: set[int] = set()
    deferred_coins: set[str] = set()
    for coin, subs in subs_by_coin.items():
        avail = (
            _liquid_for_coin(snapshot.wallet, coin)
            + fast_by_coin.get(coin, Decimal(0))
            + swap_in_by_coin.get(coin, Decimal(0))
        )
        # Smallest-first: fund what the available pool covers, defer the
        # rest (those are the ones waiting on the slow redeem).
        for a in sorted(subs, key=lambda x: x.amount):
            if a.amount <= avail:
                avail -= a.amount
            else:
                to_defer.add(id(a))
                deferred_coins.add(coin)
    if not to_defer:
        return subscribes, set()

    out: list[Action] = []
    for a in subscribes:
        if id(a) in to_defer:
            slow_amt = slow_by_coin.get(a.coin, Decimal(0))
            out.append(
                Action(
                    kind=ActionKind.SKIP_OUT_OF_SCOPE,
                    category=a.category,
                    product_id=a.product_id,
                    coin=a.coin,
                    amount=a.amount,
                    order_link_id=a.order_link_id,
                    reason=(
                        f"{a.category}/{a.product_id} ({a.coin}): deferred — "
                        f"funded by a slow OnChain redeem (${slow_amt:.2f} "
                        f"settling ~4d), not creditable this cycle; re-picks "
                        f"once it credits (avoids 180016 + suspended capital)"
                    ),
                )
            )
        else:
            out.append(a)
    return out, deferred_coins


def diff_to_actions(
    snapshot: Snapshot,
    decision: Decision,
    snapshot_ts: str,
    total_book_usd: Decimal | None = None,
    carry_state: CarryState | None = None,
) -> list[Action]:
    """Plan the action list. Redeems first (free USD), then subscribes,
    then out-of-scope skips for visibility.

    `total_book_usd` lets the caller override the sizing baseline; by
    default we read `snapshot.wallet.total_equity_usd`. The validator
    is responsible for vetoing the decision shape — this function
    trusts the decision and just translates it into orders.

    `carry_state` (`bybit-strategy-expansion.5`) lets the caller pass
    the persistent funding-carry state so the hedge reconciliation
    knows which existing perp shorts belong to carry (and must not be
    auto-closed) and so a fresh carry diff can be planned alongside
    the Earn / hedge / swap layers. None → empty state (no carry
    positions known; hedge layer behaves as pre-`.5`).
    """
    if total_book_usd is None:
        total_book_usd = snapshot.wallet.total_equity_usd
    if total_book_usd <= 0:
        return []
    if carry_state is None:
        carry_state = CarryState()

    current = _current_positions_by_pid(
        snapshot.earn_positions, snapshot.perp_market
    )
    # Merge in current Alpha holdings (`.54`) — same (category, product_id)
    # keyspace so the diff loop sees them as "current" for REDEEM logic.
    current.update(_alpha_current_positions(snapshot.alpha_positions))
    targets = _target_usd_by_pid(decision, total_book_usd, snapshot)

    redeems: list[Action] = []
    subscribes: list[Action] = []
    skips: list[Action] = []

    # All product_ids touched by current OR target — both sides matter:
    # currents not in target should be fully redeemed.
    all_pids: set[tuple[str, str]] = set(current.keys()) | set(targets.keys())
    # LM positions don't live in `current` (which only tracks Earn
    # positions) — fold them in so dropped LM picks trigger REDEEM_LM
    # via the LM branch. Without this, a position the LLM stopped picking
    # would silently stay open and accrue IL without supervision.
    for lm_pos in snapshot.lm_positions:
        lm_pid = str(lm_pos.get("productId") or "")
        if lm_pid:
            all_pids.add((_LM_CATEGORY, lm_pid))
    # Same treatment for Alpha holdings (`.54`): tokens we currently hold
    # but the LLM dropped from picks need redeeming. Alpha positions
    # carry `tokenCode` (DEX_<id>) as the product id.
    for alpha_pos in snapshot.alpha_positions:
        alpha_pid = str(alpha_pos.get("tokenCode") or "")
        if alpha_pid:
            all_pids.add((_ALPHA_CATEGORY, alpha_pid))

    for idx, key in enumerate(sorted(all_pids)):
        category, product_id = key
        current_pos = current.get(key)
        target = targets.get(key)
        order_link_id = _order_link_id(snapshot_ts, idx)

        if category in _ADVANCE_EARN_CATEGORIES:
            # Advance-Earn subscribe path (`.35`). Redeem not wired —
            # DualAssets / DiscountBuy settle automatically at expiry.
            if target and target.amount_usd > MIN_ACTION_USDC:
                # `.48` dedup: Bybit's `orderLinkId` server-side dedup
                # window is ~30min, but advance-Earn positions can stay
                # open for days (DualAssets/DiscountBuy settle at
                # `expiredTime`). Re-subscribing within an open
                # position's lifecycle opens a SECOND position rather
                # than no-oping, double-locking capital. SKIP when any
                # position exists for this (category, product_id).
                # Missing key in `advance_earn_positions` means the
                # snapshot didn't fetch positions for this product
                # (outside the top-K quote/position window) — treat as
                # "not held" because such a product also isn't pickable.
                pos_key = f"{category}/{product_id}"
                held = _advance_earn_positions_held(
                    snapshot.advance_earn_positions.get(pos_key)
                )
                if held > 0:
                    skips.append(Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=category,
                        product_id=product_id,
                        coin="?",
                        amount=target.amount_usd,
                        order_link_id=order_link_id,
                        reason=(
                            f"{category}/{product_id}: existing position "
                            f"with amount={held} already open — skip "
                            f"re-subscribe (advance-Earn settles at "
                            f"expiry; re-staking opens a 2nd position "
                            f"and double-locks capital, Bybit "
                            f"orderLinkId dedup only spans ~30min)"
                        ),
                    ))
                    continue

                action = _advance_earn_subscribe_action(
                    snapshot,
                    category,
                    product_id,
                    target.amount_usd,
                    order_link_id,
                )
                # Helper returns either SUBSCRIBE_ADVANCE_EARN or a
                # SKIP_OUT_OF_SCOPE explaining what's missing — both
                # surface in the plan so the operator can diagnose.
                if action.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN:
                    subscribes.append(action)
                else:
                    skips.append(action)
            continue

        if category == _LM_CATEGORY:
            # Liquidity Mining lifecycle (`.47`). Single-sided deposit on
            # the USDC (quote) side; pool internally rebalances to 50/50
            # at leverage=1. Three branches mirror Earn subscribe/redeem,
            # but address LP positions by `positionId` rather than
            # productId on the redeem path (one product may carry many
            # positions across cycles).
            lm_action = _lm_action_for_target(
                snapshot,
                product_id,
                target.amount_usd if target else Decimal(0),
                order_link_id,
            )
            if lm_action is None:
                continue
            if lm_action.kind == ActionKind.SUBSCRIBE_LM:
                subscribes.append(lm_action)
            elif lm_action.kind == ActionKind.REDEEM_LM:
                redeems.append(lm_action)
            else:
                skips.append(lm_action)
            continue

        if category == _ALPHA_CATEGORY:
            # Alpha Farm lifecycle (`.54`). Distinct from Earn: every
            # purchase/redeem requires a fresh quote (`quoteData` +
            # `correctingCode` + `gas`) — we don't carry quote into the
            # diff-time action (would be stale by execute time given
            # `expireTime` is ~5 minutes). Dispatch re-quotes immediately
            # before sending.
            alpha_action = _alpha_action_for_target(
                snapshot,
                product_id,
                target,
                current_pos,
                order_link_id,
            )
            if alpha_action is None:
                continue
            if alpha_action.kind in (
                ActionKind.ALPHA_PURCHASE, ActionKind.ALPHA_REDEEM
            ):
                if alpha_action.kind == ActionKind.ALPHA_REDEEM:
                    redeems.append(alpha_action)
                else:
                    subscribes.append(alpha_action)
            else:
                skips.append(alpha_action)
            continue

        if category not in _BASIC_EARN_CATEGORIES:
            if target and target.amount_usd > MIN_ACTION_USDC:
                skips.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=category,
                        product_id=product_id,
                        coin=target.coin,
                        amount=target.amount_usd,
                        order_link_id=order_link_id,
                        reason=(
                            f"{category} execution not wired — "
                            "follow-up needed for LM / SmartLeverage / "
                            "DoubleWin lifecycle"
                        ),
                    )
                )
            continue

        target_amt = target.amount_usd if target else Decimal(0)
        current_amt = current_pos.amount_usd if current_pos else Decimal(0)
        coin = (target.coin if target else (current_pos.coin if current_pos else "USDC"))

        # Defensive REDEEM (2026-06-03): a held non-stable Earn position
        # whose perp mark went missing collapses to amount_usd=0; the
        # USD-delta gate below would silently skip the redeem and leave
        # naked spot exposure when the LLM dropped the pick. If we have
        # a current native balance but the LLM dropped this product
        # (target is None), force REDEEM using the native amount as
        # ground truth. Bybit's `/v5/earn/place-order` for Redeem accepts
        # native qty via `amount_native` (the dispatch path already
        # prefers amount_native over amount when set).
        if (
            target is None
            and current_pos is not None
            and current_pos.amount_native > 0
            and category in _BASIC_EARN_CATEGORIES
        ):
            if _is_fully_processing(current_pos):
                # Entire balance still settling — Redeem would revert
                # retCode=180020. Hold; the hedge stays open via the
                # atomic-pair guard. Re-evaluated once Bybit clears it.
                skips.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=category,
                        product_id=product_id,
                        coin=coin,
                        amount=(
                            current_amt if current_amt > 0
                            else current_pos.amount_native
                        ),
                        order_link_id=order_link_id,
                        reason=(
                            f"{category}/{product_id} ({coin}): position "
                            "Processing (un-redeemable) — holding until "
                            "settled instead of a doomed REDEEM (180020)"
                        ),
                    )
                )
                continue
            # USD amount best-effort: if we have a mark, use it; else
            # fall back to native qty (executor's send_amount prefers
            # amount_native anyway). Reason string captures the gap.
            redeems.append(
                Action(
                    kind=ActionKind.REDEEM_EARN,
                    category=category,
                    product_id=product_id,
                    coin=coin,
                    amount=current_amt if current_amt > 0 else current_pos.amount_native,
                    amount_native=current_pos.amount_native,
                    order_link_id=order_link_id,
                    reason=(
                        f"redeem {category}/{product_id} ({coin}): LLM dropped "
                        f"pick, native qty {current_pos.amount_native} "
                        + (
                            f"(~${current_amt:.2f})"
                            if current_amt > 0
                            else "(USD value unknown — perp mark missing)"
                        )
                    ),
                )
            )
            continue

        delta = target_amt - current_amt
        if abs(delta) < MIN_ACTION_USDC:
            continue

        if delta > 0:
            # Per-product min_stake gate. Bybit rejects subscribes below
            # `minStakeAmount` with retCode=180012 (Purchase share is
            # invalid). Surfaced via `ProductSummary.min_subscribe_usd`
            # for FlexibleSaving + OnChain; for stables coin units ≈
            # USD so a direct compare works. Non-stables: skip when
            # available (avoids the live rejection); when not surfaced
            # the executor still hits Bybit and logs 180012.
            min_stake = None
            product_sum = _earn_product_lookup(snapshot, category, product_id)
            if product_sum is not None:
                min_stake = product_sum.min_subscribe_usd
            if (
                min_stake is not None
                and min_stake > 0
                and delta < min_stake
            ):
                skips.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=category,
                        product_id=product_id,
                        coin=coin,
                        amount=delta,
                        order_link_id=order_link_id,
                        reason=(
                            f"{category}/{product_id} ({coin}): subscribe "
                            f"${delta:.4f} below Bybit min ${min_stake} — "
                            f"would retCode=180012; scale up or drop pick"
                        ),
                    )
                )
                continue
            # For non-stables, compute native-coin units (USD / mark
            # price) so the dispatch can pass the right qty to Bybit.
            # Earn endpoints always expect native units, never USD.
            amount_native: Decimal | None = None
            if coin and coin != "USDC" and coin not in _STABLES:
                perp_info = (snapshot.perp_market or {}).get(coin)
                mark = getattr(perp_info, "mark_price", None) if perp_info else None
                if mark and mark > 0:
                    amount_native = (delta / mark).quantize(Decimal("0.0001"))
            # Bybit V5 Earn `/place-order` rejects amounts that exceed
            # the product's `precision` with retCode=180001 (live hit
            # 2026-06-03 on USDT Flex product 1, amount=10.69056). The
            # snapshot now carries `stake_precision`; quantize the
            # native unit that goes on the wire (amount_native for
            # non-stables, delta for stables) down to that precision so
            # we never out-precision the product. ROUND_DOWN avoids
            # ever rounding past `delta` (which would trip the min-stake
            # gate above retroactively or 180016 'balance not enough').
            precision = getattr(product_sum, "stake_precision", None)
            if precision is not None and precision >= 0:
                step = Decimal(1).scaleb(-precision)
                delta = delta.quantize(step, rounding=ROUND_DOWN)
                if amount_native is not None:
                    amount_native = amount_native.quantize(
                        step, rounding=ROUND_DOWN
                    )
                if delta < MIN_ACTION_USDC:
                    continue
            subscribes.append(
                Action(
                    kind=ActionKind.SUBSCRIBE_EARN,
                    category=category,
                    product_id=product_id,
                    coin=coin,
                    amount=delta,
                    amount_native=amount_native,
                    order_link_id=order_link_id,
                    reason=(
                        f"subscribe to {category}/{product_id} ({coin}): "
                        f"target ${target_amt:.2f} - current ${current_amt:.2f}"
                        + (f" ({amount_native} {coin} native)" if amount_native else "")
                    ),
                )
            )
        else:
            # Processing guard: a position whose entire balance is still
            # settling can't be redeemed (place-order Redeem reverts
            # retCode=180020). Skip rather than emit a doomed call — the
            # LLM's intended reduction waits until Bybit clears it; the
            # paired hedge stays open via the atomic-pair guard.
            if _is_fully_processing(current_pos):
                skips.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=category,
                        product_id=product_id,
                        coin=coin,
                        amount=-delta,
                        order_link_id=order_link_id,
                        reason=(
                            f"{category}/{product_id} ({coin}): position "
                            "Processing (un-redeemable) — cannot reduce yet, "
                            "holding until settled (avoids 180020)"
                        ),
                    )
                )
                continue
            # Earn redeem expects NATIVE coin units, not USD. For
            # non-stables, size the redeem from the held native qty
            # (ground truth) scaled by the USD reduction — a full exit
            # redeems the whole native balance. Without this the executor
            # falls back to the USD `amount` and sends it as the native
            # qty, so Bybit redeems the wrong amount / rejects — the exact
            # "exit TON didn't actually redeem from staking" desync.
            # Mirror of the subscribe + defensive-redeem paths above.
            # Size off the HELD position's coin, not the target's — the
            # target coin can default to USDC when the dropped product
            # isn't in the snapshot product index, but we redeem the
            # actual staked asset (e.g. TON).
            redeem_native: Decimal | None = None
            pos_coin = (current_pos.coin if current_pos else coin) or coin
            if pos_coin and pos_coin != "USDC" and pos_coin not in _STABLES:
                cur_native = (
                    current_pos.amount_native if current_pos else Decimal(0)
                )
                if cur_native > 0 and current_amt > 0:
                    frac = min(Decimal(1), (-delta) / current_amt)
                    redeem_native = cur_native * frac
                elif cur_native > 0:
                    redeem_native = cur_native
                if redeem_native is not None:
                    product_sum = _earn_product_lookup(
                        snapshot, category, product_id
                    )
                    precision = getattr(product_sum, "stake_precision", None)
                    if precision is not None and precision >= 0:
                        step = Decimal(1).scaleb(-precision)
                        redeem_native = redeem_native.quantize(
                            step, rounding=ROUND_DOWN
                        )
                    else:
                        redeem_native = redeem_native.quantize(
                            Decimal("0.0001"), rounding=ROUND_DOWN
                        )
            redeems.append(
                Action(
                    kind=ActionKind.REDEEM_EARN,
                    category=category,
                    product_id=product_id,
                    coin=coin,
                    amount=-delta,
                    amount_native=redeem_native,
                    order_link_id=order_link_id,
                    reason=(
                        f"redeem from {category}/{product_id} ({coin}): "
                        f"current ${current_amt:.2f} - target ${target_amt:.2f}"
                        + (
                            f" ({redeem_native} {coin} native)"
                            if redeem_native is not None
                            else ""
                        )
                    ),
                )
            )

    # Hedge dif: reconcile current open perp shorts against
    # `decision.hedges` (.32). Three branches per coin:
    #   - target only            → OPEN_PERP_SHORT
    #   - current only           → CLOSE_PERP (frees margin)
    #   - both, notional matches → no-op
    #   - both, notional drifts  → CLOSE + reopen at target size
    # Order in the returned list: redeems → closes → swaps → opens →
    # subscribes → skips. Closes happen BEFORE opens so freed margin is
    # available for the new shorts in the same cycle; swaps fill any
    # remaining USDT-margin gap before opens (`.33`).
    hedge_closes, hedge_opens = _hedge_diff_actions(
        snapshot,
        decision,
        snapshot_ts,
        idx_offset=len(all_pids),
        total_book_usd=total_book_usd,
        carry_coins=carry_state.active_coins(),
    )
    # Earn swaps planned FIRST so the hedge-swap sizer can see total
    # USDT demand (perp margin + non-stable Buy demand) and produce a
    # single USDC→USDT swap that funds BOTH. Pre-fix the hedge swap
    # covered only perp margin shortfall against UNIFIED USDT, leaving
    # Buy swaps to find USDT on their own — when none was left in
    # UNIFIED, the USDT budget cap dropped the Buy and cascaded the
    # whole non-stable pick.
    #
    # NB: the FINAL action list still runs `hedge_swaps → earn_swaps →
    # hedge_opens`, so the planning order swap here doesn't change the
    # dispatch contract. We size earn_swaps at a provisional offset
    # block and let hedge_swaps slot in after when we know its count.
    earn_swaps = _swap_actions_for_earn_picks(
        snapshot,
        subscribes,
        redeems,
        snapshot_ts,
        # Provisional offset; we reserve a count-of-1 hedge swap slot
        # at the front of the swap block. Hedge swaps in practice are
        # always 0 or 1 (single USDC→USDT consolidation), so this
        # avoids any orderLinkId collision.
        idx_offset=(
            len(all_pids) + len(hedge_closes) + len(hedge_opens) + 1
        ),
    )
    buy_usdt_demand = _buy_usdt_demand(earn_swaps)
    hedge_swaps = _swap_actions_for_hedges(
        snapshot,
        hedge_opens,
        hedge_closes,
        snapshot_ts,
        idx_offset=len(all_pids) + len(hedge_closes) + len(hedge_opens),
        extra_usdt_demand=buy_usdt_demand,
    )
    # USDC budget enforcement (2026-06-03). Hedge swaps and earn swaps
    # are planned independently — both spend USDC. On a small vault
    # they can collectively demand more USDC than the wallet holds,
    # producing a chain of retCode=170131 'Insufficient balance' as
    # the second swap finds USDC already drained by the first. Cap
    # total swap demand at `wallet.liquid_usdc_usd` (UNIFIED+FUND
    # combined, since `_transfer_satisfies_swap` can pull from either).
    # Hedge swaps take priority (perp margin is risk-critical); any
    # earn-side swap that overflows the budget is dropped along with
    # its dependent SUBSCRIBE (else the subscribe 180016's at execute
    # time).
    # A USDT→USDC funding Buy (`.68`) lands USDC the Sell-side stable
    # swaps can then spend — credit it toward the USDC budget so they
    # aren't dropped against the (pre-swap) start-of-cycle USDC balance.
    usdc_buy_inflow = sum(
        (
            a.amount
            for a in earn_swaps
            if a.product_id == "USDCUSDT" and a.side == "Buy"
        ),
        Decimal(0),
    )
    # Withhold USDC a NEW funding-carry open will mint into USDT at dispatch
    # (`dispatch-1` / ah.6) so the earn Sell-swaps don't over-allocate the
    # shared USDC pool and 170131 the carry swap.
    carry_usdc_reserve = _carry_open_usdc_reserve(
        decision, snapshot, carry_state, total_book_usd
    )
    earn_swaps, dropped_coins = _enforce_usdc_budget(
        snapshot.wallet.liquid_usdc_usd + usdc_buy_inflow - carry_usdc_reserve,
        hedge_swaps,
        earn_swaps,
    )

    # USDC-budget drops cascade to subscribes AND their paired perps.
    # When a stable's swap is dropped because USDC ran out, the
    # subscribe will 180016 at execute time and the perp would open
    # naked — convert both to SKIPs.
    #
    # The non-stable USD→native Buy path IS wired (`_swap_actions_for_earn_picks`
    # nonstable_demand_usd → {coin}USDT Buy). The non-stable analogue of this
    # USDC cascade is the `.3` planner guard below (`_unfunded_nonstable_subscribe_coins`),
    # which drops a non-stable subscribe + paired perp when no funded spot
    # path exists (mark missing, or a non-budget Buy shortfall). The runtime
    # `subscribe_failed_coins` guard in `execute_actions` stays as
    # defense-in-depth for whatever the plan-time guards can't foresee.
    if dropped_coins:
        new_subscribes: list[Action] = []
        for a in subscribes:
            if (
                a.kind in (ActionKind.SUBSCRIBE_EARN, ActionKind.SUBSCRIBE_LM)
                and a.coin in dropped_coins
            ):
                new_subscribes.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=a.category,
                        product_id=a.product_id,
                        coin=a.coin,
                        amount=a.amount,
                        order_link_id=a.order_link_id,
                        reason=(
                            f"{a.category}/{a.product_id} ({a.coin}): swap "
                            f"USDC→{a.coin} dropped (USDC budget exceeded); "
                            f"subscribe would 180016 — skip"
                        ),
                    )
                )
            else:
                new_subscribes.append(a)
        subscribes = new_subscribes

        new_hedge_opens: list[Action] = []
        for a in hedge_opens:
            if a.kind == ActionKind.OPEN_PERP_SHORT and a.coin in dropped_coins:
                new_hedge_opens.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=a.category,
                        product_id=a.product_id,
                        coin=a.coin,
                        amount=a.amount,
                        order_link_id=a.order_link_id,
                        reason=(
                            f"hedge {a.coin}: paired Earn subscribe dropped "
                            f"(USDC budget exceeded); skipping perp open to "
                            f"avoid naked short"
                        ),
                    )
                )
            else:
                new_hedge_opens.append(a)
        hedge_opens = new_hedge_opens
        # Re-size the consolidated USDT swap after the cascade — Buy
        # swaps may have been dropped, perp opens too, so demand changed.
        buy_usdt_demand = _buy_usdt_demand(earn_swaps)
        hedge_swaps = _swap_actions_for_hedges(
            snapshot,
            hedge_opens,
            hedge_closes,
            snapshot_ts,
            idx_offset=len(all_pids) + len(hedge_closes) + len(hedge_opens),
            extra_usdt_demand=buy_usdt_demand,
        )

    # USDT budget enforcement (2026-06-03). Mirror of the USDC pass but
    # for the USDT side of the swap graph: non-stable Earn Buy swaps on
    # {coin}USDT pairs spend USDT directly, and OPEN_PERP_SHORT consumes
    # UNIFIED USDT for margin. The hedge USDC→USDT swap topped up USDT
    # supply, but on a small vault the combined demand (perp margin +
    # multiple non-stable Buy swaps) can still exceed liquid_usdt and
    # chain 170131 'Insufficient balance' across Buy legs. Perp margin
    # is priority-1 (risk-critical); tail Buy swaps are dropped, along
    # with their dependent subscribe (the perp itself is unrelated to
    # the Buy swap — it pairs with the SUBSCRIBE_EARN, not the Buy swap
    # leg — but a dropped subscribe still cascades the perp to avoid
    # naked-short, same as the USDC pass).
    earn_swaps, usdt_dropped = _enforce_usdt_budget(
        snapshot.wallet.liquid_usdt_usd,
        hedge_swaps,
        hedge_opens,
        hedge_closes,
        earn_swaps,
        snapshot,
    )
    if usdt_dropped:
        new_subscribes: list[Action] = []
        for a in subscribes:
            if (
                a.kind in (ActionKind.SUBSCRIBE_EARN, ActionKind.SUBSCRIBE_LM)
                and a.coin in usdt_dropped
            ):
                new_subscribes.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=a.category,
                        product_id=a.product_id,
                        coin=a.coin,
                        amount=a.amount,
                        order_link_id=a.order_link_id,
                        reason=(
                            f"{a.category}/{a.product_id} ({a.coin}): Buy "
                            f"swap USDT→{a.coin} dropped (USDT budget "
                            f"exceeded); subscribe would 180016 — skip"
                        ),
                    )
                )
            else:
                new_subscribes.append(a)
        subscribes = new_subscribes

        new_hedge_opens: list[Action] = []
        for a in hedge_opens:
            if a.kind == ActionKind.OPEN_PERP_SHORT and a.coin in usdt_dropped:
                new_hedge_opens.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=a.category,
                        product_id=a.product_id,
                        coin=a.coin,
                        amount=a.amount,
                        order_link_id=a.order_link_id,
                        reason=(
                            f"hedge {a.coin}: paired Earn subscribe dropped "
                            f"(USDT budget exceeded); skipping perp open to "
                            f"avoid naked short"
                        ),
                    )
                )
            else:
                new_hedge_opens.append(a)
        hedge_opens = new_hedge_opens
        # Re-size the consolidated USDT swap after the cascade — Buy
        # swaps may have been dropped, perp opens too, so demand changed.
        buy_usdt_demand = _buy_usdt_demand(earn_swaps)
        hedge_swaps = _swap_actions_for_hedges(
            snapshot,
            hedge_opens,
            hedge_closes,
            snapshot_ts,
            idx_offset=len(all_pids) + len(hedge_closes) + len(hedge_opens),
            extra_usdt_demand=buy_usdt_demand,
        )

    # `.2` pre-flight: per-coin fully-fund-or-skip for hedged non-stable
    # picks. The two per-currency budget passes above can drop just the Buy
    # leg of a hedged pick (sizing supply off the optimistic snapshot
    # `liquid_usdt` with no fee/precision haircut), leaving the perp + the
    # funding swap → 170131 on the funding leg, executed_partial. This runs
    # against the reserved UNIFIED USDT (`_usdt_supply` less a fee/dust/round
    # reserve, symmetric with the `.60` sweep reserve) and BINARY-skips any
    # coin whose perp margin + Buy leg don't both fit — the whole pick goes
    # to cash atomically (no perp, no Buy, no subscribe survives).
    underfunded = _hedged_pick_underfunded_coins(
        snapshot, hedge_opens, hedge_closes, hedge_swaps, earn_swaps
    )
    if underfunded:
        new_subscribes = []
        for a in subscribes:
            if (
                a.kind in (ActionKind.SUBSCRIBE_EARN, ActionKind.SUBSCRIBE_LM)
                and a.coin in underfunded
            ):
                new_subscribes.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=a.category,
                        product_id=a.product_id,
                        coin=a.coin,
                        amount=a.amount,
                        order_link_id=a.order_link_id,
                        reason=(
                            f"{a.category}/{a.product_id} ({a.coin}): "
                            f"retCode=170131 pre-flight — reliably-spendable "
                            f"UNIFIED USDT can't fund both perp margin and "
                            f"the USDT→{a.coin} Buy after the spend reserve; "
                            f"skipping the whole hedged pick (no partial exec)"
                        ),
                    )
                )
            else:
                new_subscribes.append(a)
        subscribes = new_subscribes

        new_earn_swaps = []
        for a in earn_swaps:
            if a.side == "Buy" and a.coin in underfunded:
                new_earn_swaps.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=a.category,
                        product_id=a.product_id,
                        coin=a.coin,
                        amount=a.amount,
                        order_link_id=a.order_link_id,
                        reason=(
                            f"Buy USDT→{a.coin} dropped: paired hedged pick "
                            f"under-funded (170131 pre-flight) — skip"
                        ),
                    )
                )
            else:
                new_earn_swaps.append(a)
        earn_swaps = new_earn_swaps

        new_hedge_opens = []
        for a in hedge_opens:
            if (
                a.kind == ActionKind.OPEN_PERP_SHORT
                and a.coin in underfunded
            ):
                new_hedge_opens.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=a.category,
                        product_id=a.product_id,
                        coin=a.coin,
                        amount=a.amount,
                        order_link_id=a.order_link_id,
                        reason=(
                            f"hedge {a.coin}: paired Earn subscribe under-"
                            f"funded (170131 pre-flight); skipping perp open "
                            f"to avoid naked short"
                        ),
                    )
                )
            else:
                new_hedge_opens.append(a)
        hedge_opens = new_hedge_opens
        # Re-size the consolidated USDT swap after the cascade — Buy swaps
        # and perp opens were dropped, so the funding demand shrank (to 0
        # when this was the only hedged pick).
        buy_usdt_demand = _buy_usdt_demand(earn_swaps)
        hedge_swaps = _swap_actions_for_hedges(
            snapshot,
            hedge_opens,
            hedge_closes,
            snapshot_ts,
            idx_offset=len(all_pids) + len(hedge_closes) + len(hedge_opens),
            extra_usdt_demand=buy_usdt_demand,
        )

    # `.63` defer pass: same-coin subscribes that can only be funded by a
    # slow OnChain redeem (won't credit this cycle) are converted to SKIP
    # so we don't burn a doomed 180016 and leave capital suspended — the
    # pick re-appears next cycle once the redeem settles. Fires only when a
    # slow redeem of that coin is pending, so normal cycles are untouched.
    # The cross-coin swap-funded case is handled by the budget cascade
    # (slow redeems excluded from `redeem_credit`); this is the complement.
    subscribes, deferred_coins = _defer_subscribes_awaiting_slow_redeem(
        snapshot, subscribes, redeems, earn_swaps
    )
    if deferred_coins:
        # Cascade paired perps: a deferred non-stable subscribe would leave
        # its OPEN_PERP_SHORT naked, same as the budget-cascade guards.
        new_hedge_opens = []
        for a in hedge_opens:
            if a.kind == ActionKind.OPEN_PERP_SHORT and a.coin in deferred_coins:
                new_hedge_opens.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=a.category,
                        product_id=a.product_id,
                        coin=a.coin,
                        amount=a.amount,
                        order_link_id=a.order_link_id,
                        reason=(
                            f"hedge {a.coin}: paired Earn subscribe deferred "
                            f"(awaiting slow OnChain redeem, .63); skipping "
                            f"perp open to avoid naked short"
                        ),
                    )
                )
            else:
                new_hedge_opens.append(a)
        hedge_opens = new_hedge_opens

    # `.3` planner guard: a non-stable subscribe with NO funded spot path
    # (mark missing → no Buy swap emitted; or the emitted Buy + native
    # balance + in-cycle redeem still fall short of `need` — including a small
    # top-up whose sub-MIN_SWAP gap never triggers a Buy) would 180016 live
    # and strand its paired perp as a
    # naked short. Cascade both to SKIP. Shares the per-coin funded-coverage
    # notion with the `.2` pre-flight (`_buy_usd_for_coin` / `_coin_mark`).
    # The runtime `subscribe_failed_coins` guard (`execute_actions`) stays as
    # defense-in-depth for the residual cases this plan-time guard can't see.
    unfunded_subs = _unfunded_nonstable_subscribe_coins(
        snapshot, subscribes, earn_swaps, redeems
    )
    if unfunded_subs:
        new_subscribes = []
        for a in subscribes:
            if (
                a.kind in (ActionKind.SUBSCRIBE_EARN, ActionKind.SUBSCRIBE_LM)
                and a.coin in unfunded_subs
            ):
                new_subscribes.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=a.category,
                        product_id=a.product_id,
                        coin=a.coin,
                        amount=a.amount,
                        order_link_id=a.order_link_id,
                        reason=(
                            f"{a.category}/{a.product_id} ({a.coin}): no "
                            f"funded spot path; would 180016 — skip"
                        ),
                    )
                )
            else:
                new_subscribes.append(a)
        subscribes = new_subscribes

        # Drop any {coin}USDT Buy for an unfunded coin too. `.3` provably only
        # fires when no covering Buy exists (a real planner Buy would have
        # closed the coverage gap), so this is normally a no-op — but mirroring
        # the `.2` cascade removes the implicit invariant: an orphan Buy can
        # never leak regardless of how the Buy was sized. A SKIP carries no
        # `side`, so it drops out of the re-size's Buy-demand sum below.
        new_earn_swaps = []
        for a in earn_swaps:
            if a.side == "Buy" and a.coin in unfunded_subs:
                new_earn_swaps.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=a.category,
                        product_id=a.product_id,
                        coin=a.coin,
                        amount=a.amount,
                        order_link_id=a.order_link_id,
                        reason=(
                            f"Buy USDT→{a.coin} dropped: paired subscribe has "
                            f"no funded spot path (180016 pre-empt) — skip"
                        ),
                    )
                )
            else:
                new_earn_swaps.append(a)
        earn_swaps = new_earn_swaps

        new_hedge_opens = []
        for a in hedge_opens:
            if (
                a.kind == ActionKind.OPEN_PERP_SHORT
                and a.coin in unfunded_subs
            ):
                new_hedge_opens.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=a.category,
                        product_id=a.product_id,
                        coin=a.coin,
                        amount=a.amount,
                        order_link_id=a.order_link_id,
                        reason=(
                            f"hedge {a.coin}: paired Earn subscribe unfunded "
                            f"— skipping perp to avoid naked short"
                        ),
                    )
                )
            else:
                new_hedge_opens.append(a)
        hedge_opens = new_hedge_opens
        # Re-size the consolidated USDT swap after the cascade — perp opens
        # may have dropped, shrinking the funding demand.
        buy_usdt_demand = _buy_usdt_demand(earn_swaps)
        hedge_swaps = _swap_actions_for_hedges(
            snapshot,
            hedge_opens,
            hedge_closes,
            snapshot_ts,
            idx_offset=len(all_pids) + len(hedge_closes) + len(hedge_opens),
            extra_usdt_demand=buy_usdt_demand,
        )

    # Hedge→Earn reconciliation: drive every hedged-Earn coin to
    # `perp_short == earn_staked, wallet_spot == 0` via a paired CLOSE_PERP(over-
    # hedge) + SELL(orphan spot). Fixes the deadlock where a failed/partial
    # SUBSCRIBE_EARN left orphan spot that the two sweeps below mutually protect
    # (one sees the short as needing the spot, the other sees the spot as backing
    # the short). Runs FIRST and its `reconciled_coins` make the sweeps skip those
    # coins so they don't double-emit off the same pre-cycle snapshot.
    reconcile_actions, reconciled_coins = _reconcile_hedge_to_earn_actions(
        snapshot,
        subscribes,
        redeems,
        hedge_closes,
        hedge_opens,
        snapshot_ts,
        idx_offset=(
            len(all_pids)
            + len(hedge_closes)
            + len(hedge_opens)
            + len(hedge_swaps)
            + len(earn_swaps)
        ),
        carry_coins=carry_state.active_coins(),
    )
    reconciled_frozen = frozenset(reconciled_coins)

    # Defensive orphan-cleanup: sells UNIFIED-wallet non-stable balance
    # that EXCEEDS the post-cycle perp short coverage. Critically does
    # NOT sell the spot leg of an active hedge — pre-2026-06-03 it did,
    # which severed delta-neutrality and produced naked shorts.
    orphan_sells = _orphan_spot_sell_actions(
        snapshot,
        subscribes,
        redeems,
        hedge_closes,
        hedge_opens,
        snapshot_ts,
        idx_offset=(
            len(all_pids)
            + len(hedge_closes)
            + len(hedge_opens)
            + len(hedge_swaps)
            + len(earn_swaps)
            + len(reconcile_actions)
        ),
        reconciled_coins=reconciled_frozen,
    )
    # Safety net: any perp short whose post-cycle long backing comes up
    # short (UNIFIED + Earn(staked) + subscribes - redeems < perp_short)
    # gets a paired CLOSE_PERP to trim only the naked portion. Handles
    # naked shorts that survived prior cycles or future sequencing
    # mistakes — never overrides explicit LLM-planned closes/opens.
    naked_closes = _close_naked_perp_actions(
        snapshot,
        hedge_closes,
        hedge_opens,
        redeems,
        subscribes,
        snapshot_ts,
        idx_offset=(
            len(all_pids)
            + len(hedge_closes)
            + len(hedge_opens)
            + len(hedge_swaps)
            + len(earn_swaps)
            + len(reconcile_actions)
            + len(orphan_sells)
        ),
        reconciled_coins=reconciled_frozen,
    )

    # Funding-carry plan (`.5`). Sits in its own offset block past
    # every preceding action so `orderLinkId`s never collide. Carry
    # CLOSEs free both spot principal AND perp margin — they slot in
    # with the close group; carry OPENs consume USDT so they go in
    # the open group after the Earn-hedge perps (hedges are risk-
    # critical and must clear first).
    carry_offset = (
        len(all_pids)
        + len(hedge_closes)
        + len(hedge_opens)
        + len(hedge_swaps)
        + len(earn_swaps)
        + len(reconcile_actions)
        + len(orphan_sells)
        + len(naked_closes)
    )
    carry_closes, carry_opens = _funding_carry_diff(
        snapshot,
        decision,
        carry_state,
        snapshot_ts,
        idx_offset=carry_offset,
        total_book_usd=total_book_usd,
    )

    # `.60` — USDT-excess sweep. After all USDT consumers are sized
    # (hedge margin, non-stable Buy swaps, USDT-stable subscribes), if
    # liquid USDT + close-released margin still exceeds demand by ≥
    # MIN_SWAP_USDC, convert the residue back to USDC. Symmetric to the
    # `.33` shortfall path; pairs with `.49`'s orphan-spot → USDC routing
    # to keep the vault re-rebased to USDC each cycle.
    usdt_excess_swaps = _swap_actions_for_usdt_excess(
        snapshot,
        hedge_opens,
        hedge_closes,
        subscribes,
        snapshot_ts,
        idx_offset=carry_offset + len(carry_closes) + len(carry_opens),
        extra_usdt_demand=buy_usdt_demand,
    )

    actions = (
        redeems
        + carry_closes
        + hedge_closes
        + naked_closes
        + reconcile_actions
        + hedge_swaps
        + earn_swaps
        + orphan_sells
        + hedge_opens
        + carry_opens
        + subscribes
        + usdt_excess_swaps
        + skips
    )
    # Final pass: reassign `orderLinkId`s by final position so Bybit-side
    # idempotency keys are unique regardless of how the per-block
    # `idx_offset` arithmetic lined up. The block offsets above reserve a
    # single hedge-swap slot before `earn_swaps`, but downstream offsets
    # count the *actual* `len(hedge_swaps)` — so when no hedge swap is
    # emitted, an `earn_swaps` entry and the `usdt_excess` sweep collided
    # on the same id (`bybit-sandbox.68`: retCode 170141 Duplicate
    # clientOrderId).
    _reindex_order_link_ids(actions, snapshot_ts)
    return actions


def _reindex_order_link_ids(actions: list[Action], snapshot_ts: str) -> None:
    """Reassign every action's `orderLinkId` by final position (in place) so
    Bybit-side idempotency keys are unique regardless of how the per-block
    `idx_offset` arithmetic lined up. Deterministic (same actions → same ids),
    so the read-only crash-replay scan still matches.

    Carry legs store their own spot/perp ids in `extra`, DERIVED from the
    action id at construction (`{order_link_id}_spot`/`_perp`). Rewrite those
    from the NEW id too (executor-4) — otherwise they keep the stale id, which
    shadows the correct `{order_link_id}_*` fallback at execute time and the
    legs dispatch under an id that doesn't match the reindexed parent. Shared
    by `diff_to_actions` and the de-risk sweep (executor-3) so the two paths
    can't drift."""
    for i, a in enumerate(actions):
        new_id = _order_link_id(snapshot_ts, i)
        a.order_link_id = new_id
        if a.extra:
            if "spot_order_link_id" in a.extra:
                a.extra["spot_order_link_id"] = f"{new_id}_spot"
            if "perp_order_link_id" in a.extra:
                a.extra["perp_order_link_id"] = f"{new_id}_perp"
