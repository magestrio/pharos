"""Auto-extracted submodule (ah.25 execute split). See package __init__."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from agent.reason.schema import Decision
from agent.reason.venues import (
    CARRY_CATEGORY,
    LM_BASE_LEG_FRACTION,
    VENUE_REGISTRY,
)
from agent.sandbox.carry_state import (
    CarryPositionRecord,
    CarryState,
)
from agent.sandbox.execute.budget import (
    _funding_carry_targets,
)
from agent.sandbox.execute.common import (
    _ADVANCE_EARN_AMOUNT_FIELDS,
    _ALPHA_CATEGORY,
    _ALPHA_PAY_TOKEN_CODE,
    _AUTO_HEDGE_CATEGORIES,
    _LM_CATEGORY,
    _OFFER_PREFIX,
    _STABLES,
    ALPHA_EXEC_ENABLED,
    HEDGE_NOTIONAL_REBALANCE_THRESHOLD,
    MAX_CARRY_CLOSE_ATTEMPTS,
    MIN_ACTION_USDC,
    _coin_from_perp_symbol,
    _current_lm_position,
    _earn_product_lookup,
    _lm_principal_usd,
    _lm_product_from_snapshot,
    _notional_drifts,
    _order_link_id,
    _position_notional_usd,
    _round_to_qty_step,
    _safe_decimal,
)
from agent.sandbox.execute.types import (
    Action,
    ActionKind,
    ActionResult,
    _CurrentPos,
    _TargetPos,
)
from agent.sandbox.snapshot import (
    Snapshot,
)

log = logging.getLogger(__name__)


def _auto_hedge_targets(
    decision: Decision,
    snapshot: Snapshot,
    total_book_usd: Decimal,
) -> dict[str, Decimal]:
    """Derive `{coin: notional_usd_positive}` automatically from non-stable
    picks in `_AUTO_HEDGE_CATEGORIES` (OnChain + FlexibleSaving). Hedge
    notional = `pick_usd_value` (positive magnitude; the executor opens a
    short, the sign convention lives in the action).

    Replaces the prior pattern of reading `decision.hedges[].notional_usd`
    directly — Claude is bad at the arithmetic and validator rejects
    ratios outside ±20%, churning cycles on a math problem the system can
    solve deterministically. Operator change 2026-05-29: hedge intent is
    implicit (any non-stable Earn pick), hedge size is system-derived,
    `decision.hedges` is no longer authoritative for sizing.
    """
    targets: dict[str, Decimal] = {}
    for v in decision.venues:
        meta = VENUE_REGISTRY[v.venue_id]
        cat = meta.snapshot_category
        if cat not in _AUTO_HEDGE_CATEGORIES or not v.picks:
            continue
        product_coin = {
            p.product_id: p.coin
            for p in snapshot.products.get(cat, [])
        }
        for pick in v.picks:
            coin = product_coin.get(pick.product_id, "")
            if not coin or coin.upper() in _STABLES:
                continue
            pick_usd = total_book_usd * Decimal(str(v.weight)) * Decimal(str(pick.weight))
            if pick_usd <= 0:
                continue
            targets[coin.upper()] = targets.get(coin.upper(), Decimal(0)) + pick_usd
    return targets


def _lm_hedge_targets(
    decision: Decision,
    snapshot: Snapshot,
    total_book_usd: Decimal,
) -> dict[str, Decimal]:
    """Derive `{base_coin: half_notional_usd}` for `bybit_lm` picks with a
    non-stable base. A single-sided USDC/USDT LM deposit auto-rebalances
    to 50/50, so half the pick notional becomes a directional long on the
    base coin (ETH in ETH/USDC). That leg is hedged by a paired perp short
    sized at the base-leg notional — closing the naked delta the venue
    used to carry (`bybit_lm` was historically exempt from the hedge gate;
    the 'quote side hedges base' premise was false — a 50/50 LP holds half
    the base long).

    Merged into the same `targets_by_coin` as `_auto_hedge_targets` so a
    coin held via both an OnChain/Flex pick AND an LM base leg gets one
    summed short, and the existing open/close/resize diff applies
    uniformly. Stable-base pairs (none in practice) contribute nothing.
    """
    targets: dict[str, Decimal] = {}
    lm_venue = decision.venue("bybit_lm")
    if lm_venue is None or not lm_venue.picks:
        return targets
    for pick in lm_venue.picks:
        product = _lm_product_from_snapshot(snapshot, pick.product_id)
        if product is None:
            continue
        parts = product.coin.split("/", 1)
        if len(parts) != 2:
            continue
        base_coin = parts[0].upper()
        if not base_coin or base_coin in _STABLES:
            continue
        pick_usd = (
            total_book_usd
            * Decimal(str(lm_venue.weight))
            * Decimal(str(pick.weight))
        )
        base_leg = pick_usd * LM_BASE_LEG_FRACTION
        if base_leg <= 0:
            continue
        targets[base_coin] = targets.get(base_coin, Decimal(0)) + base_leg
    return targets


def _invalidate_for_coin(
    decision: Decision, snapshot: Snapshot, coin: str
) -> dict[str, Any]:
    """Return `Pick.invalidate_at` (as dict) for the FIRST non-stable
    Earn pick on `coin` across the decision, or `{}` when none set.
    Used by the hedge planner to attach Bybit-side stop / take-profit
    levels to OPEN_PERP_SHORT actions — operator-set thresholds get
    mirrored to Bybit so a tripped stop closes the perp on Bybit's
    side without waiting on the watcher poll."""
    coin_u = coin.upper()
    for v in decision.venues:
        meta = VENUE_REGISTRY[v.venue_id]
        cat = getattr(meta, "snapshot_category", None)
        if cat not in _AUTO_HEDGE_CATEGORIES or not v.picks:
            continue
        product_coin = {
            p.product_id: p.coin
            for p in snapshot.products.get(cat, [])
        }
        for pick in v.picks:
            if product_coin.get(pick.product_id, "").upper() != coin_u:
                continue
            inv = getattr(pick, "invalidate_at", None)
            if inv is None:
                return {}
            return inv.model_dump(mode="python") if hasattr(inv, "model_dump") else dict(inv)
    return {}


def _hedge_diff_actions(
    snapshot: Snapshot,
    decision: Decision,
    snapshot_ts: str,
    *,
    idx_offset: int,
    total_book_usd: Decimal,
    carry_coins: set[str] | None = None,
) -> tuple[list[Action], list[Action]]:
    """Compute `(closes, opens)` for the perp hedge layer. Target hedges
    are auto-derived from non-stable OnChain picks (see
    `_auto_hedge_targets`) — `decision.hedges` is informational only and
    NOT used for sizing here.

    `carry_coins` (`bybit-strategy-expansion.5`) lists coins owned by
    the funding-carry layer's persistent state — their open perp shorts
    are NOT Earn-hedges and MUST NOT be reconciled here. None / empty
    set preserves pre-`.5` behavior (every short is treated as a hedge).
    """
    carry_coins = {c.upper() for c in (carry_coins or set())}
    closes: list[Action] = []
    opens: list[Action] = []

    # Index current open shorts by base coin. Long positions in the
    # sandbox are not expected — surface as out-of-scope rather than
    # touching them (the executor is hedge-only). Carry-owned coins
    # are skipped: their perp shorts will be reconciled by the carry
    # diff via `_funding_carry_diff`, not here.
    current_by_coin: dict[str, Any] = {}
    for pos in snapshot.perp_positions:
        if not pos.symbol.endswith("USDT"):
            continue
        coin = _coin_from_perp_symbol(pos.symbol)
        if coin.upper() in carry_coins:
            continue
        if pos.side != "Sell":
            # Long perp — not something the hedge layer produced. Skip
            # in plan; operator can deal with it manually.
            continue
        current_by_coin[coin] = pos

    targets_by_coin: dict[str, Decimal] = _auto_hedge_targets(
        decision, snapshot, total_book_usd
    )
    # LM base legs hedge into the SAME per-coin short (summed), so the
    # open/close/resize diff below treats an ETH OnChain pick and an
    # ETH/USDC LM base leg as one combined ETH short.
    for coin, notional in _lm_hedge_targets(
        decision, snapshot, total_book_usd
    ).items():
        targets_by_coin[coin] = targets_by_coin.get(coin, Decimal(0)) + notional

    # Coins still HELD as a non-stable OnChain Earn position right now
    # (incl. a redeem that's placed but unbonding/settling — Bybit keeps
    # the row until funds clear). The hedge must track the actual
    # underlying, not the LLM's intent: closing the perp the moment the
    # LLM drops the pick would leave the still-staked/unbonding coin a
    # naked directional long for the whole settlement window. So we keep
    # the hedge while the coin is still held and only close once the Earn
    # position has actually cleared.
    held_earn_coins: set[str] = set()
    for p in snapshot.earn_positions:
        data = p.model_dump(mode="python") if hasattr(p, "model_dump") else p
        if (data.get("category") or "") not in _AUTO_HEDGE_CATEGORIES:
            continue
        c = (data.get("coin") or "").upper()
        if not c or c in _STABLES:
            continue
        try:
            amt = Decimal(str(data.get("amount", "0") or "0"))
        except (InvalidOperation, TypeError):
            amt = Decimal(0)
        if amt > 0:
            held_earn_coins.add(c)

    # Same persistence rule for held LM base legs: while an LP is still
    # open (or redeeming), its base half is a live directional long, so
    # keep the short even after the LLM drops the pick. The position's
    # base coin comes from the catalog pair (`BASE/QUOTE`) keyed by
    # productId; positions whose product left the snapshot are skipped
    # (the LP redeem path addresses them by positionId anyway).
    for pos in snapshot.lm_positions:
        product = _lm_product_from_snapshot(snapshot, str(pos.get("productId") or ""))
        if product is None:
            continue
        parts = product.coin.split("/", 1)
        if len(parts) != 2:
            continue
        base = parts[0].upper()
        if not base or base in _STABLES:
            continue
        if _lm_principal_usd(pos) > 0:
            held_earn_coins.add(base)

    all_coins = sorted(set(current_by_coin) | set(targets_by_coin))
    cursor = idx_offset

    for coin in all_coins:
        pos = current_by_coin.get(coin)
        target = targets_by_coin.get(coin)
        info = snapshot.perp_market.get(coin) or snapshot.perp_market.get(coin.upper())

        # Current size & USD notional (server-computed if available, else
        # derived from mark price as a fallback for the close-only path).
        current_size = _safe_decimal(pos.size) if pos else Decimal(0)
        current_notional = _position_notional_usd(pos, info)
        target_notional = target if target is not None else Decimal(0)

        # CLOSE: current exists, and either target absent OR notional
        # drift exceeds the rebalance threshold. EXCEPTION: when the LLM
        # dropped the pick (target is None) but the coin is still held as
        # an OnChain Earn position (redeem unbonding/settling), keep the
        # hedge — closing now would unhedge the in-flight redeem. The
        # close fires on a later cycle once the Earn position clears.
        still_held = target is None and coin.upper() in held_earn_coins
        needs_close = (
            pos is not None
            and not still_held
            and (
                target is None
                or _notional_drifts(current_notional, target_notional)
            )
        )
        # OPEN: target exists, and either current absent OR we're about
        # to close-and-reopen.
        needs_open = target is not None and (pos is None or needs_close)

        if needs_close:
            order_link_id = _order_link_id(snapshot_ts, cursor)
            cursor += 1
            closes.append(
                Action(
                    kind=ActionKind.CLOSE_PERP,
                    category="Perp",
                    product_id=pos.symbol,
                    coin=coin,
                    amount=current_size,  # base-coin qty to buy back
                    order_link_id=order_link_id,
                    reason=(
                        f"close {coin} short: "
                        + (
                            f"hedge removed (was ${current_notional:.2f})"
                            if target is None
                            else (
                                f"resize ${current_notional:.2f} → "
                                f"${target_notional:.2f} (drift exceeds "
                                f"{HEDGE_NOTIONAL_REBALANCE_THRESHOLD:.0%})"
                            )
                        )
                    ),
                )
            )

        if needs_open:
            if info is None or info.mark_price is None or info.mark_price <= 0:
                order_link_id = _order_link_id(snapshot_ts, cursor)
                cursor += 1
                opens.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category="Perp",
                        product_id=f"{coin}USDT",
                        coin=coin,
                        amount=target_notional,
                        order_link_id=order_link_id,
                        reason=(
                            f"hedge {coin}: missing perp_market entry — "
                            "cannot price qty; skipping"
                        ),
                    )
                )
                continue
            raw_qty = target_notional / info.mark_price
            qty = _round_to_qty_step(raw_qty, info.qty_step, info.min_order_qty)
            if qty is None or qty <= 0:
                # Position too small to fit one lot — surface a skip so
                # the cycle log records why no hedge fired (vs silently
                # opening unprotected exposure).
                opens.append(
                    Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category="Perp",
                        product_id=info.symbol,
                        coin=coin,
                        amount=target_notional,
                        order_link_id=_order_link_id(snapshot_ts, cursor),
                        reason=(
                            f"hedge {coin}: target qty {raw_qty} rounds to <{info.qty_step}, "
                            f"below min_order_qty={info.min_order_qty}; skip hedge"
                        ),
                    )
                )
                cursor += 1
                continue
            order_link_id = _order_link_id(snapshot_ts, cursor)
            cursor += 1
            # Mirror LLM-set invalidate_at levels onto the perp as
            # Bybit-side stop / take-profit so a tripped threshold
            # closes the position on Bybit's side without waiting on
            # the watcher poll. For a SHORT:
            #   price_above → stopLoss (short loses as mark rises)
            #   price_below → takeProfit (short wins as mark falls,
            #                  user wants out anyway when this fires)
            invalidate = _invalidate_for_coin(decision, snapshot, coin)
            extra: dict[str, Any] = {}
            sl = invalidate.get("price_above") if invalidate else None
            tp = invalidate.get("price_below") if invalidate else None
            if sl is not None:
                extra["stop_loss"] = str(sl)
            if tp is not None:
                extra["take_profit"] = str(tp)
            opens.append(
                Action(
                    kind=ActionKind.OPEN_PERP_SHORT,
                    category="Perp",
                    product_id=info.symbol,
                    coin=coin,
                    amount=qty,
                    order_link_id=order_link_id,
                    extra=extra,
                    reason=(
                        f"short {coin} ${target_notional:.2f} notional "
                        f"({qty} {coin}, step={info.qty_step}) @ mark ${info.mark_price:.4f}"
                        + (f" SL=${sl}" if sl is not None else "")
                        + (f" TP=${tp}" if tp is not None else "")
                    ),
                )
            )

    return closes, opens


def _lm_action_for_target(
    snapshot: Snapshot,
    product_id: str,
    target_amount_usd: Decimal,
    order_link_id: str,
) -> Action | None:
    """Plan one LM action for a `(product_id, target_usd)` pair (`.47`).

    Returns:
      - `SUBSCRIBE_LM` when target > MIN_ACTION_USDC and the wallet has
        no open position on this product. The action's `amount` is the
        USDC (quote) deposit size; Bybit auto-balances to 50/50 at spot.
      - `REDEEM_LM` when there's an existing position and the target
        dropped to ~zero. Full exit (removeRate=100, removeType=Normal).
      - `SKIP_OUT_OF_SCOPE` when:
          * the LM product isn't in the snapshot (LLM hallucinated id)
          * the pair isn't quoteCoin=USDC (we only know how to fund
            single-sided USDC deposits)
          * the existing position resists targeting (e.g. rebalance-to-
            non-zero — partial scaling not modeled in MVP)
      - `None` when no action is needed (target ≈ current, both > 0
        but within threshold).

    MVP scope: subscribe and full exit only. Partial drawdown (target >
    0 but smaller than current) emits SKIP with a reason — Bybit's LM
    `removeRate` accepts percent but the diff would need to convert
    USD delta → percent against `principalLiquidityValue`, which adds
    rounding edge cases not worth tackling before `.14` smoke.
    """
    product = _lm_product_from_snapshot(snapshot, product_id)
    if product is None:
        # A HELD position whose product is no longer in the snapshot must
        # still be REDEEMABLE — redemption addresses the position by its
        # `positionId` from `lm_positions` and needs no catalog row. This is
        # the legacy-leveraged-LP exit path (`bybit-sandbox.66`): the `.66`
        # filter drops `max_leverage>1` products from the choice set, so a
        # position opened before the filter would otherwise be stuck
        # un-redeemable here. Full-exit when the LLM dropped it to ~0.
        held = _current_lm_position(snapshot.lm_positions, product_id)
        if held is not None and target_amount_usd <= MIN_ACTION_USDC:
            position_id, held_usd = held
            return Action(
                kind=ActionKind.REDEEM_LM,
                category=_LM_CATEGORY,
                product_id=product_id,
                coin="?",
                amount=held_usd,
                order_link_id=order_link_id,
                reason=(
                    f"redeem LM/{product_id}: product no longer pickable "
                    f"(dropped from choice set) but position held "
                    f"${held_usd:.2f} → full exit (removeRate=100)"
                ),
                position_id=position_id,
            )
        return Action(
            kind=ActionKind.SKIP_OUT_OF_SCOPE,
            category=_LM_CATEGORY,
            product_id=product_id,
            coin="?",
            amount=target_amount_usd,
            order_link_id=order_link_id,
            reason=(
                f"LiquidityMining/{product_id}: product not in snapshot — "
                "LLM may have hallucinated the id; pick is unactionable"
            ),
        )
    parts = product.coin.split("/", 1)
    if len(parts) != 2:
        return Action(
            kind=ActionKind.SKIP_OUT_OF_SCOPE,
            category=_LM_CATEGORY,
            product_id=product_id,
            coin=product.coin,
            amount=target_amount_usd,
            order_link_id=order_link_id,
            reason=(
                f"LiquidityMining/{product_id}: malformed pair {product.coin!r} "
                "(expected `BASE/QUOTE`)"
            ),
        )
    base_coin, quote_coin = parts
    # Non-stable quote coins (hypothetical — Bybit LM is stable-quoted in
    # practice) aren't sized against USD reliably without mark prices on
    # the quote side; skip with a clear reason. USDC-quote and USDT-quote
    # both pass; USDT-quote subscribes get a USDC→USDT swap leg emitted
    # later in `_swap_actions_for_earn_picks`.
    if quote_coin not in _STABLES:
        return Action(
            kind=ActionKind.SKIP_OUT_OF_SCOPE,
            category=_LM_CATEGORY,
            product_id=product_id,
            coin=quote_coin,
            amount=target_amount_usd,
            order_link_id=order_link_id,
            reason=(
                f"LiquidityMining/{product_id} ({base_coin}/{quote_coin}): "
                f"quote coin {quote_coin!r} is not a recognized stable — "
                "USD sizing not reliable without quote-side mark price"
            ),
        )

    current = _current_lm_position(snapshot.lm_positions, product_id)
    current_usd = current[1] if current else Decimal(0)

    # Fresh subscribe path.
    if current is None:
        if target_amount_usd <= MIN_ACTION_USDC:
            return None
        # Bybit enforces a per-product floor (e.g. 50 USDC for ETH/USDC).
        # Trying to subscribe below it returns `retCode=180005` / similar;
        # SKIP at diff time with a clear message so the operator can
        # either scale up the LLM's allocation or top up the wallet.
        if (
            product.min_subscribe_usd is not None
            and target_amount_usd < product.min_subscribe_usd
        ):
            return Action(
                kind=ActionKind.SKIP_OUT_OF_SCOPE,
                category=_LM_CATEGORY,
                product_id=product_id,
                coin=quote_coin,
                amount=target_amount_usd,
                order_link_id=order_link_id,
                reason=(
                    f"LiquidityMining/{product_id} ({base_coin}/{quote_coin}): "
                    f"target ${target_amount_usd:.2f} below Bybit min "
                    f"${product.min_subscribe_usd} — Bybit would reject; "
                    f"either scale up the LM allocation or top up {quote_coin}"
                ),
            )
        return Action(
            kind=ActionKind.SUBSCRIBE_LM,
            category=_LM_CATEGORY,
            product_id=product_id,
            coin=quote_coin,
            amount=target_amount_usd,
            order_link_id=order_link_id,
            reason=(
                f"subscribe LM/{product_id} ({base_coin}/{quote_coin}) "
                f"${target_amount_usd:.2f} single-sided {quote_coin}, leverage=1; "
                f"Bybit pool rebalances to 50/50 internally"
            ),
        )

    position_id, _ = current
    # Existing position. Full exit when LLM dropped below threshold.
    if target_amount_usd <= MIN_ACTION_USDC:
        return Action(
            kind=ActionKind.REDEEM_LM,
            category=_LM_CATEGORY,
            product_id=product_id,
            coin=quote_coin,
            amount=current_usd,
            order_link_id=order_link_id,
            reason=(
                f"redeem LM/{product_id} ({base_coin}/{quote_coin}): "
                f"current ${current_usd:.2f} → target $0 (full exit, "
                f"removeRate=100, removeType=Normal)"
            ),
            position_id=position_id,
        )
    # Position roughly matches target — no-op.
    delta = abs(target_amount_usd - current_usd)
    if delta < MIN_ACTION_USDC:
        return None
    # Partial redemption when target < current (de-risk path). Bybit's
    # `removeRate` accepts integer 1-100; we round DOWN so we never
    # redeem more than intended. Sub-1% deltas would round to 0 and
    # Bybit rejects — collapse to no-op for those.
    if target_amount_usd < current_usd:
        redeem_usd = current_usd - target_amount_usd
        if current_usd <= 0:
            return None
        rate_pct = int(
            (redeem_usd / current_usd * Decimal(100)).quantize(Decimal("1"))
        )
        if rate_pct < 1:
            return None
        rate_pct = min(rate_pct, 99)  # full exit goes through the branch above
        return Action(
            kind=ActionKind.REDEEM_LM,
            category=_LM_CATEGORY,
            product_id=product_id,
            coin=quote_coin,
            amount=redeem_usd,
            order_link_id=order_link_id,
            reason=(
                f"redeem LM/{product_id} ({base_coin}/{quote_coin}) "
                f"partial: current ${current_usd:.2f} → target "
                f"${target_amount_usd:.2f} (removeRate={rate_pct}%, "
                f"removeType=Normal)"
            ),
            position_id=position_id,
            extra={"remove_rate": rate_pct},
        )
    # Partial INCREASE (target > current). Bybit add-liquidity opens a
    # SECOND position on the same product rather than topping up — would
    # leave two position_ids to track at next redeem. SKIP with a reason
    # telling the operator to wait a cycle for full exit + resubscribe.
    return Action(
        kind=ActionKind.SKIP_OUT_OF_SCOPE,
        category=_LM_CATEGORY,
        product_id=product_id,
        coin=quote_coin,
        amount=target_amount_usd,
        order_link_id=order_link_id,
        reason=(
            f"LiquidityMining/{product_id}: partial increase not wired "
            f"(current ${current_usd:.2f}, target ${target_amount_usd:.2f}); "
            "Bybit add-liquidity would open a second position. Hold this "
            "cycle; if Claude still wants more next cycle, full-exit then "
            "resubscribe at the new size."
        ),
    )


def _alpha_action_for_target(
    snapshot: Snapshot,
    token_code: str,
    target: _TargetPos | None,
    current_pos: _CurrentPos | None,
    order_link_id: str,
) -> Action | None:
    """Plan one Alpha Farm action: PURCHASE on net-new or top-up,
    REDEEM on dropped pick, SKIP when the gate is off (`.14` safety) or
    the venue isn't actionable this cycle.

    Decision matrix (no quote fetched here — execute time re-quotes):
      - No current, no target          → no-op (returns None)
      - No current, target > MIN_ACTION_USDC, GATE on → ALPHA_PURCHASE
      - Current, no target             → ALPHA_REDEEM (full exit)
      - Current, target ≈ current      → no-op (within MIN_ACTION_USDC)
      - Anything else with GATE off    → SKIP_OUT_OF_SCOPE

    `current_pos.amount_usd` comes from `snapshot.alpha_positions[*]
    .tokenAmountUsd` (set by `_current_positions_by_pid`). Native-coin
    `amount` for REDEEM is reconstructed from the alpha-position row's
    `tokenAmount` so we pass Bybit the exact base-units it expects in
    `fromTokenAmount` — the USD figure is informational only.

    `coin` on the action carries the alpha token's `tokenSymbol` for log
    readability; the dispatch always uses `token_code` (DEX_<id>) on the
    wire.
    """
    target_usd = target.amount_usd if target else Decimal(0)
    current_usd = current_pos.amount_usd if current_pos else Decimal(0)
    symbol = (
        (target.coin if target else None)
        or (current_pos.coin if current_pos else None)
        or token_code
    )

    delta = target_usd - current_usd
    if abs(delta) < MIN_ACTION_USDC:
        return None

    if not ALPHA_EXEC_ENABLED:
        # Gate is off — emit SKIP so the plan shows the intent without
        # firing a live API call. Operator flips VAULT8004_ALPHA_EXEC_ENABLED
        # to enable. Per `.54` safety: this guards the `.14` smoke test.
        verb = "purchase" if delta > 0 else "redeem"
        return Action(
            kind=ActionKind.SKIP_OUT_OF_SCOPE,
            category=_ALPHA_CATEGORY,
            product_id=token_code,
            coin=symbol,
            amount=abs(delta),
            order_link_id=order_link_id,
            reason=(
                f"AlphaFarm/{token_code}: would {verb} ${abs(delta):.2f} "
                f"(current ${current_usd:.2f} → target ${target_usd:.2f}); "
                "skipped because VAULT8004_ALPHA_EXEC_ENABLED is off (`.54` "
                "safety: live-probe + `.14` smoke close required first)"
            ),
        )

    if delta > 0:
        # Purchase. `amount` carries the USD-equivalent payment size; the
        # dispatch translates this into `fromTokenAmount` (USDT base
        # units) after fetching a fresh quote. We do NOT carry quote
        # data through the action — `expireTime` is short enough that
        # diff-time → dispatch-time delay would frequently invalidate.
        return Action(
            kind=ActionKind.ALPHA_PURCHASE,
            category=_ALPHA_CATEGORY,
            product_id=token_code,
            coin=symbol,
            amount=delta,
            order_link_id=order_link_id,
            reason=(
                f"alpha_purchase {token_code} ({symbol}) "
                f"${delta:.2f} via {_ALPHA_PAY_TOKEN_CODE}: "
                f"current ${current_usd:.2f} → target ${target_usd:.2f}"
            ),
        )

    # REDEEM. For partial reductions Bybit Alpha would require keeping
    # the position open at a smaller size, but `tokenAmount` precision
    # doesn't always permit clean fractional exits. MVP: only full exits
    # (current → 0). Partial scaling SKIPs with a reason.
    if target_usd > MIN_ACTION_USDC:
        return Action(
            kind=ActionKind.SKIP_OUT_OF_SCOPE,
            category=_ALPHA_CATEGORY,
            product_id=token_code,
            coin=symbol,
            amount=target_usd,
            order_link_id=order_link_id,
            reason=(
                f"AlphaFarm/{token_code}: partial reduction not wired "
                f"(current ${current_usd:.2f}, target ${target_usd:.2f}); "
                "Alpha MVP only supports full exit on dropped picks. "
                "If Claude wants a smaller size, drop the pick this cycle "
                "and resubscribe at the new size next cycle."
            ),
        )

    # Full exit. We need the native token amount, not USD — Bybit's
    # `/v5/alpha/trade/redeem` takes `fromTokenAmount` in base units.
    # Pull from the alpha-position row by `tokenCode` match.
    token_amount_native = "0"
    for pos in snapshot.alpha_positions:
        if str(pos.get("tokenCode") or "") == token_code:
            raw = pos.get("tokenAmount")
            if raw is not None:
                token_amount_native = str(raw)
            break
    if token_amount_native == "0":
        return Action(
            kind=ActionKind.SKIP_OUT_OF_SCOPE,
            category=_ALPHA_CATEGORY,
            product_id=token_code,
            coin=symbol,
            amount=current_usd,
            order_link_id=order_link_id,
            reason=(
                f"AlphaFarm/{token_code}: redeem requested but no "
                "tokenAmount in snapshot.alpha_positions — degraded "
                "position fetch this cycle"
            ),
        )
    return Action(
        kind=ActionKind.ALPHA_REDEEM,
        category=_ALPHA_CATEGORY,
        product_id=token_code,
        coin=symbol,
        amount=current_usd,  # USD-equivalent for log readability
        order_link_id=order_link_id,
        reason=(
            f"alpha_redeem {token_code} ({symbol}) "
            f"${current_usd:.2f} → {_ALPHA_PAY_TOKEN_CODE}: "
            f"full exit (dropped pick)"
        ),
        extra={"token_amount_native": token_amount_native},
    )


def _advance_earn_positions_held(
    rows: list[dict[str, Any]] | None,
) -> Decimal:
    """Sum the active stake across a list of advance-Earn position rows
    (`.48`). Returns the total in the position's stake currency.

    A row counts as "active" when it has a positive amount in any of
    the per-category amount fields and no obviously-terminal status.
    Bybit's `/v5/earn/advance/position` endpoint already filters to
    open positions in practice — the status check is a belt-and-braces
    guard in case Bybit echoes a settled row during the brief window
    between settlement and the row being purged.

    Returns `Decimal(0)` for missing/empty input — that's also what the
    diff branch treats as "not held, safe to subscribe".
    """
    if not rows:
        return Decimal(0)
    terminal = {"settled", "completed", "expired", "cancelled", "closed"}
    total = Decimal(0)
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        if status in terminal:
            continue
        for field in _ADVANCE_EARN_AMOUNT_FIELDS:
            raw = row.get(field)
            if raw is None:
                continue
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if value > 0:
                total += value
                break
    return total


def _advance_earn_subscribe_action(
    snapshot: Snapshot,
    category: str,
    product_id: str,
    target_amount_usd: Decimal,
    order_link_id: str,
) -> Action:
    """Build the SUBSCRIBE_ADVANCE_EARN action for a DualAssets or
    DiscountBuy pick.

    Two layers of offer data:
    - **Diff-time best-effort**: pick a fresh offer from the cached quote
      and encode it in `Action.reason` as a fallback. If the cached
      quote has no usable (non-expired) offer, encode an empty stub —
      the execute branch will refresh anyway.
    - **Execute-time refresh**: the executor re-fetches the quote
      immediately before dispatch (see `_execute_one`), so the offer
      used on the wire reflects the latest Bybit rotation rather than
      whatever the snapshot saw 30-60s ago. The diff-time offer is the
      last-ditch fallback when the refresh call fails.

    Returns SKIP_OUT_OF_SCOPE only when the pick is fundamentally
    unactionable — quote entirely missing (product fell outside top-K
    fan-out OR per-product call failed) OR the coin cannot be resolved
    even from the product list. Stale-at-diff is NOT a SKIP — operator
    change 2026-05-29: `.35` follow-up to fix DiscountBuy/DualAssets
    silently skipping every cycle because their offers rotate faster
    than the snapshot→decide→validate→diff path takes.
    """
    key = f"{category}/{product_id}"
    quote = snapshot.advance_earn_quotes.get(key)
    if not quote:
        return Action(
            kind=ActionKind.SKIP_OUT_OF_SCOPE,
            category=category,
            product_id=product_id,
            coin="?",
            amount=target_amount_usd,
            order_link_id=order_link_id,
            reason=(
                f"{category}/{product_id}: no cached quote in snapshot — "
                "product fell outside the top-K quote window or the quote "
                "call failed; pick is unactionable this cycle"
            ),
        )

    offer, coin, reason_detail = _pick_advance_offer(
        category, quote, snapshot, product_id
    )
    # `coin == "?"` means we couldn't even resolve the staking coin
    # (product missing from `snapshot.products`). That's unrecoverable
    # at execute time — SKIP. But `offer is None` with a known coin is
    # fine — execute will refresh the quote.
    if not coin or coin == "?":
        return Action(
            kind=ActionKind.SKIP_OUT_OF_SCOPE,
            category=category,
            product_id=product_id,
            coin="?",
            amount=target_amount_usd,
            order_link_id=order_link_id,
            reason=(
                f"{category}/{product_id}: cannot resolve stake coin "
                f"({reason_detail}); pick is unactionable"
            ),
        )

    # Per-product min-stake gate (mirrors the SUBSCRIBE_EARN gate).
    # DualAssets/DiscountBuy carry their own floors ($10-$20) which
    # often exceed a small-vault per-pick allocation — Bybit rejects
    # sub-floor stakes with retCode=180012 'Purchase share is invalid:
    # Amount out of range'. SKIP at diff time so the cycle log is
    # readable and the live executor doesn't burn rate-limit quota on
    # known-failing calls.
    product_sum = _earn_product_lookup(snapshot, category, product_id)
    if (
        product_sum is not None
        and product_sum.min_subscribe_usd is not None
        and product_sum.min_subscribe_usd > 0
        and target_amount_usd < product_sum.min_subscribe_usd
    ):
        return Action(
            kind=ActionKind.SKIP_OUT_OF_SCOPE,
            category=category,
            product_id=product_id,
            coin=coin,
            amount=target_amount_usd,
            order_link_id=order_link_id,
            reason=(
                f"{category}/{product_id} ({coin}): subscribe "
                f"${target_amount_usd:.2f} below Bybit min "
                f"${product_sum.min_subscribe_usd} — would retCode=180012; "
                f"concentrate the venue or drop pick"
            ),
        )

    # Encode the per-category offer details into the action's reason so
    # the dispatch has a fallback if the execute-time refresh fails. May
    # be empty `{}` when the diff-time quote had no fresh offers — the
    # dispatch handles that case by erroring out cleanly if the refresh
    # also fails.
    serialized_offer = json.dumps(offer or {}, sort_keys=True, default=str)
    if offer is None:
        descriptor = f"stale-at-diff ({reason_detail}); execute will refresh"
    else:
        descriptor = reason_detail
    return Action(
        kind=ActionKind.SUBSCRIBE_ADVANCE_EARN,
        category=category,
        product_id=product_id,
        coin=coin,
        amount=target_amount_usd,
        order_link_id=order_link_id,
        reason=(
            f"subscribe {category}/{product_id} ({coin}) ${target_amount_usd:.2f}: "
            f"{descriptor} offer={serialized_offer}"
        ),
    )


def _pick_advance_offer(
    category: str,
    quote: dict[str, Any],
    snapshot: Snapshot,
    product_id: str,
) -> tuple[dict[str, Any] | None, str, str]:
    """Return `(offer_dict_or_None, subscription_coin, reason_detail)`
    for the best actionable offer in `quote` per category-specific shape.

    DualAssets quote shape (verified against live capture 2026-05-28):
        {category, list: [{productId, currentPrice,
            buyLowPrice:  [{selectPrice, apyE8, maxInvestmentAmount, expiredAt}, ...],
            sellHighPrice:[{...}, ...]}]}

    Notes vs original docs:
      - `expiredAt` lives on EACH offer row, not at the parent payload.
      - `baseCoin`/`quoteCoin` are NOT echoed in the quote — they only
        live in `/v5/earn/advance/product` (cached as
        `snapshot.products["DualAssets"][i].coin = "BASE/QUOTE"`), so we
        pull the pair from the snapshot's product list to know the
        stake currency.

    We pick the highest-APR non-expired `buyLowPrice` offer (strike
    below current → commits us to *buying* the base coin at a discount
    if price drops; stake is the quote coin).

    DiscountBuy quote shape (verified against live capture 2026-05-28):
        {offers: [{productId, currentPrice, purchasePrice, knockoutPrice,
                   knockoutCouponE8, maxInvestmentAmount, instUid,
                   expiredAt, category}]}

    Notes vs original docs:
      - Top-level key is `offers`, NOT `list` — different from DualAssets.
      - The offer row doesn't carry `coin`; stake currency is on the
        product list (`snapshot.products["DiscountBuy"][i].coin`),
        usually USDT.

    `expiredAt` is unix-ms; past = unusable.
    """
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    if category == "DualAssets":
        items = quote.get("list") or []
        if not items or not isinstance(items[0], dict):
            return None, "?", "empty quote list"
        payload = items[0]
        pair = _advance_product_pair(snapshot, "DualAssets", product_id)
        if pair is None:
            return None, "?", (
                "DualAssets product missing from snapshot.products "
                "(can't determine stake coin)"
            )
        base, quote_coin = pair
        coin = quote_coin  # buyLowPrice stake currency is the quote coin
        best: tuple[Decimal, dict[str, Any]] | None = None
        expired_count = 0
        for offer in payload.get("buyLowPrice") or []:
            # Per-offer expiry — Bybit's quote endpoint rotates offers
            # roughly every cycle, so some rows in a multi-offer payload
            # may already be past their TTL while others are fresh.
            if _offer_expired(offer.get("expiredAt"), now_ms):
                expired_count += 1
                continue
            raw = offer.get("apyE8")
            if raw is None:
                continue
            try:
                apy = Decimal(str(raw)) / Decimal("1e8")
            except (InvalidOperation, TypeError):
                continue
            if best is None or apy > best[0]:
                best = (apy, offer)
        if best is None:
            return None, coin, (
                f"no usable buyLowPrice offers "
                f"(expired={expired_count}, missing/invalid apyE8 on rest)"
            )
        apy, offer = best
        return offer, coin, (
            f"DualAssets {base}/{quote_coin} buyLowPrice strike="
            f"{offer.get('selectPrice')} apy={apy:.4f}"
        )

    if category == "DiscountBuy":
        # NB: live shape uses `offers` at top-level (verified 2026-05-28),
        # not `list` as the changelog implied.
        items = quote.get("offers") or quote.get("list") or []
        if not items or not isinstance(items[0], dict):
            return None, "?", "empty offers list"
        offer = items[0]
        coin = (
            offer.get("coin")
            or _advance_product_coin(snapshot, "DiscountBuy", product_id)
            or "USDT"
        )
        expired = offer.get("expiredAt") or offer.get("expiredTime")
        if _offer_expired(expired, now_ms):
            return None, coin, f"offer past expiredAt={expired}"
        if not offer.get("instUid"):
            return None, coin, "offer missing instUid"
        return offer, coin, (
            f"DiscountBuy instUid={offer.get('instUid')} "
            f"purchase={offer.get('purchasePrice')} "
            f"knockout={offer.get('knockoutPrice')}"
        )

    return None, "?", f"unsupported advance-Earn category {category}"


def _advance_product_coin(
    snapshot: Snapshot, category: str, product_id: str
) -> str | None:
    """Return the `ProductSummary.coin` field for the advance-Earn
    product matching `(category, product_id)`. Used as a stake-coin
    source when the quote endpoint doesn't echo it (DiscountBuy)."""
    for p in snapshot.products.get(category, []):
        if p.product_id == product_id:
            return p.coin
    return None


def _advance_product_pair(
    snapshot: Snapshot, category: str, product_id: str
) -> tuple[str, str] | None:
    """For DualAssets, the snapshot stores `coin="BASE/QUOTE"`. Split
    and return `(base, quote)`. Returns None when product missing or
    the coin field doesn't carry a pair."""
    coin = _advance_product_coin(snapshot, category, product_id)
    if coin is None:
        return None
    parts = coin.split("/", 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def _pick_offer_for_execute(
    category: str, quote: dict[str, Any]
) -> dict[str, Any] | None:
    """Pick the freshest valid offer from a quote payload at execute
    time, returning the raw offer dict (or None). Unlike diff-time
    `_pick_advance_offer`, this function takes no `snapshot` argument —
    coin resolution already happened at diff time and was encoded in
    `Action.coin`. We only need the offer for the `*Extra` block.

    Mirror of the per-category logic in `_pick_advance_offer` minus the
    coin lookup. `.35` follow-up 2026-05-29.
    """
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    if category == "DualAssets":
        items = quote.get("list") or []
        if not items or not isinstance(items[0], dict):
            return None
        payload = items[0]
        best: tuple[Decimal, dict[str, Any]] | None = None
        for offer in payload.get("buyLowPrice") or []:
            if _offer_expired(offer.get("expiredAt"), now_ms):
                continue
            raw_apy = offer.get("apyE8")
            if raw_apy is None:
                continue
            try:
                apy = Decimal(str(raw_apy)) / Decimal("1e8")
            except (InvalidOperation, TypeError):
                continue
            if best is None or apy > best[0]:
                best = (apy, offer)
        if best is None:
            return None
        # Tag direction so `_build_advance_extra` can write the
        # orderDirection field without re-deriving it.
        return {**best[1], "orderDirection": "BuyLow"}
    if category == "DiscountBuy":
        items = quote.get("offers") or quote.get("list") or []
        if not items or not isinstance(items[0], dict):
            return None
        offer = items[0]
        expired = offer.get("expiredAt") or offer.get("expiredTime")
        if _offer_expired(expired, now_ms):
            return None
        if not offer.get("instUid"):
            return None
        return offer
    return None


def _offer_expired(expired_raw: Any, now_ms: int) -> bool:
    """True when `expired_raw` (unix-ms, string or int) is in the past
    relative to `now_ms`. Missing / unparseable → True (fail-closed:
    don't subscribe to an offer of unknown lifetime)."""
    if expired_raw in (None, ""):
        return True
    try:
        return int(str(expired_raw)) <= now_ms
    except (ValueError, TypeError):
        return True


def apply_carry_results_to_state(
    state: CarryState, results: list[ActionResult]
) -> CarryState:
    """Roll a fresh `CarryState` forward by walking dispatch results.

    Successful `OPEN_FUNDING_CARRY` (status="ok") → insert a position
    record sized from the ACTUAL spot fill (cumExecQty + realized price),
    falling back to the planned action only when the response lacks fills
    (dispatch-3). Successful `CLOSE_FUNDING_CARRY` → drop the matching coin's
    record. OPEN orphans write NO record: the dispatch already atomically
    unwinds the half-open (sells the spot back), and any residual naked spot is
    swept by `_orphan_spot_sell_actions` next cycle — a perp_qty=0 record here
    would instead collide with that sweep (state-3). CLOSE orphans KEEP the
    record (partial unwind, next cycle's CLOSE retries) AND bump the
    `close_attempts` counter so `_funding_carry_diff` can stop emitting
    after `MAX_CARRY_CLOSE_ATTEMPTS` cycles — protects against
    unbounded retry when the perp leg fails identically every cycle.

    Dry-run / skip / error results don't move state.
    """
    next_state = state
    for r in results:
        a = r.action
        if a.kind == ActionKind.OPEN_FUNDING_CARRY and r.status == "ok":
            # Size the record from the ACTUAL spot fill (cumExecQty + realized
            # price), not the planned amount_native / mark — the CLOSE later
            # sizes both legs off this record, and a mark-vs-fill gap would
            # leave a residual (dispatch-3). Both legs use the same base qty
            # (the dispatch sizes the perp from the spot fill), so the record
            # stays delta-neutral. Fall back to planned when the response
            # lacks fills (older actions / partial response shapes).
            spot_leg = (r.response or {}).get("legs", {}).get("spot", {})
            try:
                filled_qty = Decimal(str(spot_leg.get("cumExecQty")))
            except (InvalidOperation, TypeError):
                filled_qty = None
            base_qty = (
                filled_qty
                if filled_qty is not None and filled_qty > 0
                else a.amount_native
            )
            if base_qty is None or base_qty <= 0:
                continue
            try:
                filled_value = Decimal(str(spot_leg.get("cumExecValue")))
            except (InvalidOperation, TypeError):
                filled_value = None
            if filled_value is not None and filled_value > 0:
                realized_mark = filled_value / base_qty
            else:
                try:
                    realized_mark = Decimal(str(a.extra.get("mark_price") or "0"))
                except (InvalidOperation, TypeError):
                    realized_mark = Decimal(0)
            next_state = next_state.upsert(
                CarryPositionRecord(
                    coin=a.coin.upper(),
                    opened_at=datetime.now(UTC),
                    target_pick_usd=a.amount,
                    spot_qty_base=base_qty,
                    perp_qty_base=base_qty,
                    mark_price_at_open=realized_mark,
                    spot_order_link_id=(
                        a.extra.get("spot_order_link_id")
                        or f"{a.order_link_id}_spot"
                    ),
                    perp_order_link_id=(
                        a.extra.get("perp_order_link_id")
                        or f"{a.order_link_id}_perp"
                    ),
                )
            )
        elif a.kind == ActionKind.CLOSE_FUNDING_CARRY and r.status == "ok":
            next_state = next_state.remove(a.coin)
        elif a.kind == ActionKind.CLOSE_FUNDING_CARRY and r.status == "orphan":
            existing = next_state.get(a.coin)
            if existing is not None:
                bumped = existing.model_copy(
                    update={"close_attempts": existing.close_attempts + 1}
                )
                next_state = next_state.upsert(bumped)
    return next_state


def _funding_carry_diff(
    snapshot: Snapshot,
    decision: Decision,
    carry_state: CarryState,
    snapshot_ts: str,
    *,
    idx_offset: int,
    total_book_usd: Decimal,
) -> tuple[list[Action], list[Action]]:
    """Produce `(closes, opens)` carry actions.

    Branches per coin in (targets ∪ carry_state):
      - target only AND target ≥ MIN_ACTION_USDC → OPEN_FUNDING_CARRY
      - state only (target=0 or coin dropped from picks) → CLOSE_FUNDING_CARRY
      - both → no-op (MVP holds existing position; ADJUST deferred)

    Sizing for OPEN: `pick_usd / mark_price` → rounded down to
    `qty_step` → both legs use the same base-qty so they're
    delta-neutral by construction. Coins lacking perp_market data are
    skipped (caller has already validated this via
    `check_funding_carry_floor`, but this is defensive).

    `idx_offset` slots `orderLinkId`s after the pre-existing planning
    blocks so collisions with REDEEM/SUBSCRIBE/hedge IDs are
    impossible.
    """
    targets = _funding_carry_targets(decision, snapshot, total_book_usd)
    state_by_coin = {p.coin.upper(): p for p in carry_state.positions}
    perp_market = getattr(snapshot, "perp_market", None) or {}

    coins = sorted(set(targets.keys()) | set(state_by_coin.keys()))
    opens: list[Action] = []
    closes: list[Action] = []

    for offset, coin in enumerate(coins):
        order_link_id = _order_link_id(snapshot_ts, idx_offset + offset)
        target = targets.get(coin, Decimal(0))
        existing = state_by_coin.get(coin)

        if target >= MIN_ACTION_USDC and existing is None:
            info = perp_market.get(coin) or perp_market.get(coin.lower())
            if info is None or info.mark_price is None or info.mark_price <= 0:
                continue
            raw_qty = target / info.mark_price
            qty = _round_to_qty_step(
                raw_qty, info.qty_step, info.min_order_qty
            )
            if qty is None or qty <= 0:
                continue
            opens.append(
                Action(
                    kind=ActionKind.OPEN_FUNDING_CARRY,
                    category=CARRY_CATEGORY,
                    product_id=info.symbol,
                    coin=coin,
                    amount=target,
                    amount_native=qty,
                    order_link_id=order_link_id,
                    reason=(
                        f"open funding-carry {coin}: pick_usd=${target:.2f} "
                        f"@ mark={info.mark_price} → qty={qty}"
                    ),
                    extra={
                        "mark_price": str(info.mark_price),
                        "spot_order_link_id": f"{order_link_id}_spot",
                        "perp_order_link_id": f"{order_link_id}_perp",
                    },
                )
            )
            continue

        if existing is not None and target < MIN_ACTION_USDC:
            if existing.close_attempts >= MAX_CARRY_CLOSE_ATTEMPTS:
                # Persistent CLOSE failure (typically perp leg failing
                # on margin / symbol issue) — stop auto-retry, surface
                # for operator action. The state record stays so the
                # operator can inspect it; once they unwind manually
                # they can `read_carry_state` → bump counter back to 0
                # or remove the record.
                log.warning(
                    "carry CLOSE skipped for %s: close_attempts=%d "
                    "exceeded MAX_CARRY_CLOSE_ATTEMPTS=%d — needs "
                    "operator review (state file holds the position)",
                    coin, existing.close_attempts, MAX_CARRY_CLOSE_ATTEMPTS,
                )
                continue
            symbol = f"{coin}USDT"
            closes.append(
                Action(
                    kind=ActionKind.CLOSE_FUNDING_CARRY,
                    category=CARRY_CATEGORY,
                    product_id=symbol,
                    coin=coin,
                    amount=existing.target_pick_usd,
                    amount_native=existing.spot_qty_base,
                    order_link_id=order_link_id,
                    reason=(
                        f"close funding-carry {coin}: LLM dropped pick, "
                        f"close spot qty={existing.spot_qty_base} + perp "
                        f"qty={existing.perp_qty_base}"
                    ),
                    extra={
                        "spot_order_link_id": f"{order_link_id}_spot",
                        "perp_order_link_id": f"{order_link_id}_perp",
                    },
                )
            )
            continue
        # Both → MVP holds, no-op. ADJUST (resize up/down) lands in a
        # follow-up subtask.

    return closes, opens


def _decode_offer_from_reason(reason: str) -> dict[str, Any]:
    """Pull the JSON-encoded offer dict back out of the action's `reason`
    field. We store it there at diff time so the action is self-contained
    — no need for the dispatch layer to re-look-up the snapshot, and the
    operator gets the same blob in plan logs and post-mortem JSONL.
    Returns `{}` when the reason doesn't carry an offer (e.g. SKIP)."""
    marker = _OFFER_PREFIX
    idx = reason.find(marker)
    if idx < 0:
        return {}
    try:
        return json.loads(reason[idx + len(marker):])
    except json.JSONDecodeError:
        return {}


def _build_advance_extra(category: str, offer: dict[str, Any]) -> dict[str, Any]:
    """Translate the cached offer dict into the per-category `*Extra`
    block `place_advance_earn_order` merges into the request body. Keys
    mirror the Bybit V5 docs verbatim
    (https://bybit-exchange.github.io/docs/v5/finance/advanced-earn).

    Field shape was updated 2026-06-03 after a live retCode=180001
    (`Invalid parameter: initial_price` / `order_direction`) — Bybit
    deprecated the older `side` / `currentPrice` / `expiredAt` keys.
    Spec now requires:
      DualAssets   → orderDirection (BuyLow|SellHigh), selectPrice, apyE8
      DiscountBuy  → initialPrice, purchasePrice, knockoutPrice,
                     knockoutCouponE8, settleType (Base|Quote), instUid
    """
    if category == "DualAssets":
        # Planner only emits buy-low picks (see `_pick_offer_for_execute`),
        # so orderDirection is hardcoded. SellHigh would need a different
        # diff-layer signal anyway.
        return {
            "dualAssetsExtra": {
                "orderDirection": offer.get("orderDirection", "BuyLow"),
                "selectPrice": offer.get("selectPrice"),
                "apyE8": offer.get("apyE8"),
            }
        }
    if category == "DiscountBuy":
        # `initialPrice` was named `currentPrice` in older docs; the field
        # in the quote response is still `currentPrice`, but the order
        # body expects `initialPrice`. We accept either source key for
        # forward/backward compat.
        return {
            "discountBuyExtra": {
                "initialPrice": offer.get("initialPrice")
                or offer.get("currentPrice"),
                "purchasePrice": offer.get("purchasePrice"),
                "knockoutPrice": offer.get("knockoutPrice"),
                "knockoutCouponE8": offer.get("knockoutCouponE8"),
                # Settle in base (underlying asset) when knockout doesn't
                # fire; settle back in quote stable otherwise. We default
                # to Base since our use-case is "buy BTC/ETH at discount"
                # — settleType=Quote turns it into a flat-yield product
                # which isn't why we pick DiscountBuy.
                "settleType": offer.get("settleType", "Base"),
                "instUid": offer.get("instUid"),
            }
        }
    return {}
