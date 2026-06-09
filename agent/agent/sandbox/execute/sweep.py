"""Auto-extracted submodule (ah.25 execute split). See package __init__."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from agent.reason.venues import (
    CARRY_CATEGORY,
    LM_BASE_LEG_FRACTION,
    LM_RESIDUAL_NAKED_MAX,
)
from agent.sandbox.carry_state import (
    CarryState,
)
from agent.sandbox.execute.common import (
    _LM_CATEGORY,
    _STABLE_CONSOLIDATE_PAIRS,
    _STABLE_LOT,
    _STABLES,
    MAX_CARRY_CLOSE_ATTEMPTS,
    MIN_ACTION_USDC,
    MIN_SWAP_USDC,
    _coin_from_perp_symbol,
    _coin_to_long_exposure,
    _coin_to_perp_short_size,
    _coin_wallet_native,
    _lm_base_leg_native,
    _lm_principal_usd,
    _lm_product_from_snapshot,
    _order_link_id,
    _orphan_sell_quote,
    _round_to_qty_step,
    _safe_decimal,
)
from agent.sandbox.execute.types import (
    Action,
    ActionKind,
    ActionResult,
)
from agent.sandbox.redeem_intent import RedeemExitIntent
from agent.sandbox.snapshot import (
    Snapshot,
)

log = logging.getLogger(__name__)


def build_redeem_exit_intents(
    snapshot: Snapshot,
    results: list[ActionResult],
) -> list[RedeemExitIntent]:
    """Record one `RedeemExitIntent` per successful `REDEEM_EARN` on a
    non-stable coin (a hedged-Earn exit). Captures the pre-redeem wallet
    balance (`snapshot` is the cycle's pre-execute snapshot) so the watcher
    can detect arrival by delta, and the paired short so the exit closes the
    exact size. Pure: caller persists into the durable store.

    Stables are skipped (no hedge, no swap-back). A non-stable redeem without
    a live paired short still records (`paired_perp_symbol=None`) — the
    swap-back is wanted regardless; the handler just skips the perp close."""
    shorts = _coin_to_perp_short_size(snapshot)
    opened_at = datetime.now(UTC)
    intents: list[RedeemExitIntent] = []
    for r in results:
        if getattr(r, "status", None) != "ok":
            continue
        a = getattr(r, "action", None)
        if a is None or a.kind != ActionKind.REDEEM_EARN:
            continue
        coin = (a.coin or "").upper()
        if not coin or coin in _STABLES:
            continue
        short = shorts.get(coin, Decimal(0))
        expected = a.amount_native if a.amount_native is not None else a.amount
        intents.append(
            RedeemExitIntent(
                coin=coin,
                product_id=a.product_id,
                category=a.category,
                opened_at=opened_at,
                expected_redeem_native=expected,
                baseline_wallet_native=_coin_wallet_native(snapshot, coin),
                redeem_order_link_id=a.order_link_id,
                paired_perp_symbol=f"{coin}USDT" if short > 0 else None,
                perp_qty_base=short,
            )
        )
    return intents


def exit_actions_from_intent(
    snapshot: Snapshot,
    intent: RedeemExitIntent,
    *,
    snapshot_ts: str,
    idx_offset: int,
) -> list[Action]:
    """Build the deterministic exit for ONE settled hedged-Earn redeem: close
    the paired perp short, then swap the freed coin to a stable — both in the
    same cycle, sized from the RECORDED redeem (capped at the live wallet so we
    never over-sell). CLOSE first (a momentary naked-long between the two legs
    is safer than a naked-short, matching `closes→swaps` ordering).

    Returns [] when nothing is actionable (coin not yet arrived / already
    flat) — the caller treats an empty result on a vanished Earn row as
    "fully unwound, drop the intent"."""
    actions: list[Action] = []
    cursor = idx_offset
    coin_u = intent.coin.upper()
    info = (snapshot.perp_market or {}).get(coin_u) or (
        snapshot.perp_market or {}
    ).get(coin_u.lower())
    qty_step = getattr(info, "qty_step", None) if info else None
    min_qty = getattr(info, "min_order_qty", None) if info else None

    # 1. Close the paired short (only the size still open, capped at recorded).
    if intent.paired_perp_symbol:
        live_short = _coin_to_perp_short_size(snapshot).get(coin_u, Decimal(0))
        if live_short > 0:
            want = (
                min(intent.perp_qty_base, live_short)
                if intent.perp_qty_base > 0
                else live_short
            )
            qty = _round_to_qty_step(want, qty_step, min_qty)
            if qty is not None and qty > 0:
                actions.append(
                    Action(
                        kind=ActionKind.CLOSE_PERP,
                        category="linear",
                        product_id=intent.paired_perp_symbol,
                        coin=coin_u,
                        amount=qty,
                        order_link_id=_order_link_id(snapshot_ts, cursor),
                        reason=(
                            f"exit hedged Earn {coin_u}: close paired short "
                            f"{qty} on redeem settle (no LLM)"
                        ),
                    )
                )
                cursor += 1

    # 2. Swap the freed coin to a stable — exactly the redeemed native, capped
    #    at the live wallet balance (UNIFIED+FUND).
    wallet_native = _coin_wallet_native(snapshot, coin_u)
    sell_native = min(intent.expected_redeem_native, wallet_native)
    mark = getattr(info, "mark_price", None) if info else None
    if sell_native > 0 and mark and mark > 0 and sell_native * mark >= MIN_SWAP_USDC:
        # Spot-sell qty rounds to the SPOT lot at dispatch (validate_qty),
        # NOT the perp qty_step — flooring to the coarse perp step strands
        # up to ~1 perp lot of the freed coin (see `_orphan_spot_sell_actions`).
        qty = sell_native
        if qty > 0:
            symbol, dest_coin = _orphan_sell_quote(coin_u)
            actions.append(
                Action(
                    kind=ActionKind.SWAP_SPOT,
                    category="Spot",
                    product_id=symbol,
                    coin=dest_coin,
                    amount=qty,
                    side="Sell",
                    order_link_id=_order_link_id(snapshot_ts, cursor),
                    reason=(
                        f"exit hedged Earn {coin_u}: swap {qty} freed → "
                        f"{dest_coin} on redeem settle (no LLM)"
                    ),
                    # Disposal sell — see `_orphan_spot_sell_actions`: the goal
                    # is to SELL the freed coin, not acquire the dest stable, so
                    # bypass the FUND-transfer optimization that would no-op it.
                    extra={"skip_fund_transfer": True},
                )
            )
    return actions


def _orphan_perp_close_actions(
    snapshot: Snapshot,
    snapshot_ts: str,
    *,
    idx_offset: int,
    carry_coins: set[str] | None = None,
) -> list[Action]:
    """Close perp SHORTS whose coin has NO backing yield position — held
    Earn (incl. settling `Processing`), held LM base leg, or funding-carry.
    Such a short is an orphan: its underlying was redeemed/closed, so it's
    a naked directional short that only bleeds funding. Closing it is pure
    risk reduction.

    Pure / state-only (mirrors `_orphan_spot_sell_actions`): reads wallet +
    position state, NOT the decision, so the safety de-risk sweep can run
    it on low-confidence / rejected cycles where the normal hedge-diff
    close is gated to dry-run and never fires (the BERA dust-short bug:
    Earn redeemed, hedge-diff plans the close every cycle but conf<0.60
    dry-runs it, leaving a 1-BERA short matched against 1-BERA spot that
    the orphan-spot-sell treats as delta-neutral backing → neither clears).

    Closes the FULL short size (rounded to qty_step); a sub-min-order
    remainder is genuine untradeable dust and stays. The matched spot then
    becomes naked and is swept by `_orphan_spot_sell_actions` (modulo its
    own sub-min floor)."""
    carry_coins = {c.upper() for c in (carry_coins or set())}
    backed: set[str] = set(carry_coins)
    for p in snapshot.earn_positions or []:
        data = p.model_dump(mode="python") if hasattr(p, "model_dump") else p
        c = (data.get("coin") or "").upper()
        if not c or c in _STABLES:
            continue
        try:
            if Decimal(str(data.get("amount", "0") or "0")) > 0:
                backed.add(c)
        except (InvalidOperation, TypeError):
            continue
    for pos in snapshot.lm_positions or []:
        product = _lm_product_from_snapshot(snapshot, str(pos.get("productId") or ""))
        if product is None:
            continue
        parts = product.coin.split("/", 1)
        if len(parts) != 2:
            continue
        base = parts[0].upper()
        if base and base not in _STABLES and _lm_principal_usd(pos) > 0:
            backed.add(base)

    closes: list[Action] = []
    cursor = idx_offset
    for pos in snapshot.perp_positions:
        if not pos.symbol.endswith("USDT") or pos.side != "Sell":
            continue
        coin_u = _coin_from_perp_symbol(pos.symbol).upper()
        if coin_u in backed:
            continue
        size = _safe_decimal(pos.size)
        if size <= 0:
            continue
        info = (snapshot.perp_market or {}).get(coin_u) or (
            snapshot.perp_market or {}
        ).get(coin_u.lower())
        qty_step = getattr(info, "qty_step", None) if info else None
        min_qty = getattr(info, "min_order_qty", None) if info else None
        qty = _round_to_qty_step(size, qty_step, min_qty)
        if qty is None or qty <= 0:
            continue
        mark = getattr(info, "mark_price", None) if info else None
        note = f" ~${(qty * mark):.2f}" if mark and mark > 0 else ""
        closes.append(
            Action(
                kind=ActionKind.CLOSE_PERP,
                category="linear",
                product_id=pos.symbol,
                coin=coin_u,
                amount=qty,
                order_link_id=_order_link_id(snapshot_ts, cursor),
                reason=(
                    f"close orphan perp {coin_u} short {qty}{note}: no backing "
                    f"Earn/LM/carry position (de-risk sweep)"
                ),
            )
        )
        cursor += 1
    return closes


def _orphan_spot_sell_actions(
    snapshot: Snapshot,
    subscribes: list[Action],
    redeems: list[Action],
    hedge_closes: list[Action],
    hedge_opens: list[Action],
    snapshot_ts: str,
    *,
    idx_offset: int,
) -> list[Action]:
    """Defensive cleanup: sell to USDT the wallet portion (UNIFIED + FUND)
    of a non-stable coin that exceeds what's needed to balance an open perp
    short on the same coin. FUND is included because LM/LP-redeem principal
    settles there (`bybit-sandbox`, 2026-06-08: TIA); the SWAP_SPOT Sell
    dispatch transfers the base coin FUND→UNIFIED before placing the order.

    Delta-neutral accounting (2026-06-03 fix after live naked-short bug):
        total_long  = wallet_balance(UNIFIED+FUND) + earn_staked_native
        perp_short  = abs(open Sell perp size on {coin}USDT)
                      after this cycle's planned closes / opens
        excess_long = max(0, total_long - perp_short)
        sellable    = min(wallet_balance, excess_long)

    Only the `sellable` portion goes to a `SWAP_SPOT Sell` — never the
    spot leg that's currently hedging an open short. Pre-fix the function
    sold any UNIFIED non-stable balance unconditionally; on TON that
    severed the hedge and produced a naked short (worse than the LIT
    orphan-long it was meant to fix).

    Skips:
      - Stables (USDC/USDT/...) — destination, not source.
      - Coins being subscribed this cycle — let subscribe consume them.
      - Coins with no perp mark — can't price the swap.
      - Sub-MIN_SWAP_USDC notional or sub-min_order_qty after qty_step
        rounding — fees > recovery / Bybit reject.

    Emits one SWAP_SPOT Sell per coin with excess wallet long. Runs after
    the subscribe planner so it sees the post-cascade subscribes set.
    """
    pending_subscribe_coins = {
        (a.coin or "").upper()
        for a in subscribes
        if a.kind in (ActionKind.SUBSCRIBE_EARN, ActionKind.SUBSCRIBE_LM)
        and a.coin
    }
    earn_long = _coin_to_long_exposure(snapshot)
    perp_short = _coin_to_perp_short_size(snapshot)
    # Adjust perp_short by this cycle's planned hedge_closes / hedge_opens
    # so we don't keep spot to back a short that's about to close, and
    # we DO keep spot for a short that's about to open. Each amount is
    # native qty (see `_hedge_diff_actions`).
    for a in hedge_closes:
        if a.kind != ActionKind.CLOSE_PERP or not a.coin:
            continue
        coin_u = a.coin.upper()
        perp_short[coin_u] = max(
            Decimal(0), perp_short.get(coin_u, Decimal(0)) - a.amount
        )
    for a in hedge_opens:
        if a.kind != ActionKind.OPEN_PERP_SHORT or not a.coin:
            continue
        coin_u = a.coin.upper()
        perp_short[coin_u] = perp_short.get(coin_u, Decimal(0)) + a.amount

    swaps: list[Action] = []
    cursor = idx_offset
    # Merge UNIFIED + FUND non-stable balances per coin. A coin freed by an
    # LM/LP redeem lands in FUND (`bybit-sandbox`, 2026-06-08: TIA), which a
    # UNIFIED-only scan never sees → naked spot forever. Both are sellable:
    # the SWAP_SPOT Sell dispatch auto-transfers the base coin FUND→UNIFIED
    # (`_ensure_unified_balance`) before placing the spot order.
    balances: dict[str, Decimal] = {}
    for src in (
        snapshot.wallet.unified_coin_balances or {},
        getattr(snapshot.wallet, "fund_coin_balances", None) or {},
    ):
        for coin, raw in src.items():
            if not coin:
                continue
            try:
                bal = raw if isinstance(raw, Decimal) else Decimal(str(raw))
            except (InvalidOperation, TypeError):
                continue
            cu = coin.upper()
            balances[cu] = balances.get(cu, Decimal(0)) + bal
    for coin_u, balance in balances.items():
        if coin_u in _STABLES or coin_u == "USDC":
            continue
        if balance <= 0:
            continue
        if coin_u in pending_subscribe_coins:
            continue
        perp_info = (snapshot.perp_market or {}).get(coin_u) or (
            snapshot.perp_market or {}
        ).get(coin_u.lower())
        mark = getattr(perp_info, "mark_price", None) if perp_info else None
        if not mark or mark <= 0:
            continue
        # Delta-aware excess: total long minus current perp short coverage.
        total_long = balance + earn_long.get(coin_u, Decimal(0))
        short = perp_short.get(coin_u, Decimal(0))
        excess_long = total_long - short
        if excess_long <= 0:
            # Hedge is balanced or perp is over-sized — selling spot
            # would create / worsen a naked short. Skip.
            continue
        sellable = min(balance, excess_long)
        if sellable <= 0:
            continue
        usd = sellable * mark
        if usd < MIN_SWAP_USDC:
            continue
        # Spot-sell qty is rounded to the SPOT pair's lot at dispatch
        # (place_spot_order → validate_qty: basePrecision + spot min). Do
        # NOT pre-floor to the PERP qty_step here — the perp step is far
        # coarser than the spot lot (ETH perp 0.01 vs spot 0.00001), so
        # flooring a disposal to it strands up to ~1 perp lot (prod
        # 2026-06-09: 0.0058 ETH ≈ $10 left behind because 0.0058 < perp
        # step 0.01). The MIN_SWAP_USDC value gate above is the real floor;
        # validate_qty drops a true sub-spot-min remainder on its own.
        qty = sellable
        # `.49`: BTC/ETH (DiscountBuy settlement landing spots) ship to
        # USDC directly via `{coin}USDC`. Everything else keeps the
        # universal `{coin}USDT` route.
        symbol, dest_coin = _orphan_sell_quote(coin_u)
        swaps.append(
            Action(
                kind=ActionKind.SWAP_SPOT,
                category="Spot",
                product_id=symbol,
                coin=dest_coin,
                amount=qty,
                side="Sell",
                order_link_id=_order_link_id(snapshot_ts, cursor),
                reason=(
                    f"sell orphan {qty} {coin_u} → {dest_coin} "
                    f"(~${usd:.2f}): wallet {balance} (UNIFIED+FUND) + Earn "
                    f"{earn_long.get(coin_u, 0)} - perp short {short} = "
                    f"excess {excess_long}"
                ),
                # Disposal sell: force the real Sell. Without this the
                # dispatch's `_transfer_satisfies_swap` reads `amount` as a
                # dest-coin (USDC/USDT) requirement, finds it already in FUND,
                # and no-ops the sell — stranding the non-stable (the ETH/dust
                # never liquidates). Mirrors the stable-consolidation builder.
                extra={"skip_fund_transfer": True},
            )
        )
        cursor += 1
    _ = redeems  # parameter kept for call-site symmetry / future use
    return swaps


def _lm_residual_redeem_actions(
    snapshot: Snapshot,
    snapshot_ts: str,
    *,
    idx_offset: int,
    blocked_position_ids: set[str] | None = None,
) -> list[Action]:
    """Full-redeem any HELD LM position whose un-hedgeable naked base-coin
    residual exceeds `LM_RESIDUAL_NAKED_MAX` of book (lm-residual epic).

    The residual is a base-coin long stuck INSIDE the LP — the coarse perp lot
    under-hedges the base half and no spot/perp sweep can reach the remainder,
    only redeeming the LP can. So it is pure risk reduction that, like the
    orphan-perp/spot sweep, MUST run even on a cycle the validator or
    confidence floor won't execute: the agent's own redeem decision shrinks the
    book to mostly cash and so scores BELOW the 0.60 execute gate, meaning the
    de-risk would otherwise never fire (observed live 2026-06-09). Reads only
    `lm_positions` (the snapshot injects `hedge_residual_pct_of_book` on each
    held LM), not the decision — absent/unparseable ⇒ skip (no signal).

    Leaves the paired perp short ALONE: the LP redeem is async, so this cycle
    the short still backs real base exposure; once the LP settles the short
    goes orphan and `_orphan_perp_close_actions` closes it. Full exit only
    (removeRate=100). Skipped: positions at/under `MIN_ACTION_USDC`, and any
    positionId in `blocked_position_ids` — the durable redeem cooldown (wt-3)
    we already submitted a redeem for within the settlement window, so we
    don't re-submit a doomed removeRate=100 every non-executing cycle (Bybit
    180020). This replaces an earlier `status=="Processing"` guard the LM
    payload doesn't reliably populate; positionId + timestamp is robust to a
    missing status field. After the window a still-naked position retries."""
    actions: list[Action] = []
    cursor = idx_offset
    seen: set[str] = set()
    blocked = blocked_position_ids or set()
    for pos in snapshot.lm_positions or []:
        raw = pos.get("hedge_residual_pct_of_book")
        if raw is None:
            continue
        try:
            pct = Decimal(str(raw))
        except (InvalidOperation, TypeError):
            continue
        if pct <= LM_RESIDUAL_NAKED_MAX:
            continue
        position_id = str(pos.get("positionId") or "")
        if not position_id or position_id in seen:
            continue
        # Redeem already emitted for this position within the cooldown window
        # — the LP settles async, so re-emitting removeRate=100 every cycle
        # just 180020-spams until settlement.
        if position_id in blocked:
            continue
        held_usd = _lm_principal_usd(pos)
        if held_usd <= MIN_ACTION_USDC:
            continue
        seen.add(position_id)
        product_id = str(pos.get("productId") or "")
        actions.append(
            Action(
                kind=ActionKind.REDEEM_LM,
                category=_LM_CATEGORY,
                product_id=product_id,
                coin="?",
                amount=held_usd,
                order_link_id=_order_link_id(snapshot_ts, cursor),
                reason=(
                    f"residual de-risk: LM/{product_id} naked base residual "
                    f"{float(pct) * 100:.2f}% of book > "
                    f"{float(LM_RESIDUAL_NAKED_MAX) * 100:.0f}% floor → full "
                    f"exit (removeRate=100); in-LP naked long no sweep reaches"
                ),
                position_id=position_id,
            )
        )
        cursor += 1
    return actions


def _carry_liq_close_actions(
    snapshot: Snapshot,
    carry_state: CarryState,
    near_liq_coins: set[str],
    snapshot_ts: str,
    *,
    idx_offset: int,
) -> list[Action]:
    """Deterministically close any held funding-carry whose perp short is
    nearing liquidation (watcher `carry_liq_close` event), even on a cycle the
    validator / confidence floor won't execute.

    A carry has NO separate Earn/LM leg to redeem — the perp IS the position
    (plus its spot Buy leg). So unlike the Earn/LM auto-close (which drops a
    pick and lets the diff redeem the leg), this emits one
    `CLOSE_FUNDING_CARRY` per coin in `near_liq_coins ∩ active carry coins` —
    the same Action the normal carry diff uses, sized from the persisted
    `CarryPositionRecord` so BOTH legs unwind atomically. A near-liq coin with
    NO carry record (a manual naked short) is skipped here and left to the
    orphan-perp / LLM path.

    Mirrors the CLOSE branch of `_funding_carry_diff`: honors
    `MAX_CARRY_CLOSE_ATTEMPTS` so a persistently-failing close (margin / symbol
    issue) stops auto-retrying and surfaces for operator review instead of
    spamming a doomed order every non-executing cycle. `snapshot` is unused for
    sizing (state carries the qtys) but kept in the signature to match the
    other sweep helpers and leave room for a future depth/slippage guard."""
    actions: list[Action] = []
    cursor = idx_offset
    for coin in sorted(c.upper() for c in near_liq_coins):
        existing = carry_state.get(coin)
        if existing is None:
            continue
        if existing.close_attempts >= MAX_CARRY_CLOSE_ATTEMPTS:
            log.warning(
                "carry liq-close skipped for %s: close_attempts=%d exceeded "
                "MAX_CARRY_CLOSE_ATTEMPTS=%d — needs operator review",
                coin, existing.close_attempts, MAX_CARRY_CLOSE_ATTEMPTS,
            )
            continue
        order_link_id = _order_link_id(snapshot_ts, cursor)
        cursor += 1
        symbol = f"{coin}USDT"
        actions.append(
            Action(
                kind=ActionKind.CLOSE_FUNDING_CARRY,
                category=CARRY_CATEGORY,
                product_id=symbol,
                coin=coin,
                amount=existing.target_pick_usd,
                amount_native=existing.spot_qty_base,
                order_link_id=order_link_id,
                reason=(
                    f"liq de-risk: funding-carry {coin} short nearing "
                    f"liquidation — close spot qty={existing.spot_qty_base} + "
                    f"perp qty={existing.perp_qty_base} (both legs, no LLM)"
                ),
                extra={
                    "spot_order_link_id": f"{order_link_id}_spot",
                    "perp_order_link_id": f"{order_link_id}_perp",
                },
            )
        )
    return actions


def _stable_consolidate_actions(
    snapshot: Snapshot,
    snapshot_ts: str,
    *,
    idx_offset: int,
) -> list[Action]:
    """Rebase idle NON-CORE stable wallet balances (USD1, …) into a core
    stable (USDT) so they re-enter the liquid budget and get deployed
    instead of sitting at 0%. Mirrors `_orphan_spot_sell_actions` but for
    perp-less stables.

    Pure stable→stable disposal: no perp, no directional exposure, no delta
    guard (these coins have no perp market). Reads ONLY wallet balances
    (UNIFIED+FUND) — earn-staked native is excluded (it must be REDEEMed,
    not sold, and never lands in these maps). Skips sub-MIN_SWAP_USDC dust.

    Each per-account balance is floored to the stable lot BEFORE summing:
    the UNIFIED+FUND total can exceed the tradable amount when a sub-lot
    FUND dust can't transfer to UNIFIED, and the snapshot's per-account
    value is itself 2dp-rounded (can round UP) — so summing raw then
    flooring would size the Sell past the real balance and Bybit rejects
    it (observed live 2026-06-08: sized 41.90 vs 41.8966 in UNIFIED).

    Emits one SWAP_SPOT Sell per coin via its confirmed base=coin pair.
    `extra.skip_fund_transfer` forces the actual sell: the dispatch's
    `_transfer_satisfies_swap` optimization is for *acquiring* a target
    coin already sitting in FUND — wrong for disposal, where it would
    no-op the sell and leave the stable stranded.
    """
    balances: dict[str, Decimal] = {}
    for src in (
        snapshot.wallet.unified_coin_balances or {},
        getattr(snapshot.wallet, "fund_coin_balances", None) or {},
    ):
        for coin, raw in src.items():
            if not coin:
                continue
            try:
                bal = raw if isinstance(raw, Decimal) else Decimal(str(raw))
            except (InvalidOperation, TypeError):
                continue
            # Floor EACH account to the lot before summing (see docstring).
            bal = bal.quantize(_STABLE_LOT, rounding=ROUND_DOWN)
            if bal <= 0:
                continue
            cu = coin.upper()
            balances[cu] = balances.get(cu, Decimal(0)) + bal

    swaps: list[Action] = []
    cursor = idx_offset
    for coin_u, qty in sorted(balances.items()):
        pair = _STABLE_CONSOLIDATE_PAIRS.get(coin_u)
        if pair is None:
            continue  # core stable (USDC/USDT), non-stable, or no Sell pair
        # Stable ~ $1, so qty ≈ USD. Skip fee-dominated dust.
        if qty < MIN_SWAP_USDC:
            continue
        symbol, dest_coin = pair
        swaps.append(
            Action(
                kind=ActionKind.SWAP_SPOT,
                category="Spot",
                product_id=symbol,
                coin=dest_coin,
                amount=qty,
                side="Sell",
                order_link_id=_order_link_id(snapshot_ts, cursor),
                reason=(
                    f"consolidate idle {qty} {coin_u} → {dest_coin} "
                    f"(~${qty:.2f}): non-core stable invisible to the "
                    f"USDC+USDT liquid budget, rebasing to deployable stable"
                ),
                extra={"skip_fund_transfer": True},
            )
        )
        cursor += 1
    return swaps


def _close_naked_perp_actions(
    snapshot: Snapshot,
    hedge_closes: list[Action],
    hedge_opens: list[Action],
    redeems: list[Action],
    subscribes: list[Action],
    snapshot_ts: str,
    *,
    idx_offset: int,
) -> list[Action]:
    """Safety net: when a coin's perp SHORT exceeds its post-cycle LONG
    exposure (UNIFIED wallet + Earn staked, adjusted for this cycle's
    planned redeems / subscribes / hedge moves), emit a `CLOSE_PERP` to
    trim the short back to delta-neutral.

    Catches naked shorts produced by upstream sequencing bugs (e.g.
    orphan-sell on a hedged spot, REDEEM_EARN without a paired CLOSE,
    failed subscribe leaving a stranded perp). Runs alongside the LLM-
    planned hedge diff — only fires when the LLM didn't already close
    enough, and only for the gap, never to override an explicit choice.

    Conservative: only closes the NAKED portion, never the whole short.
    If `perp_short = 4.1` and `total_long_post_cycle = 1.0`, this emits
    `CLOSE_PERP qty=3.1` (closes the 3.1 that has no spot backing) and
    leaves the 1.0 still hedging the 1.0 long.
    """
    # Native long exposure per coin AFTER this cycle's actions settle:
    #   UNIFIED + Earn(staked) + subscribe_native(planned) - redeem_native(planned)
    long_now = _coin_to_long_exposure(snapshot)
    for coin, bal in (snapshot.wallet.unified_coin_balances or {}).items():
        if not coin:
            continue
        coin_u = coin.upper()
        if coin_u in _STABLES or coin_u == "USDC":
            continue
        if bal > 0:
            long_now[coin_u] = long_now.get(coin_u, Decimal(0)) + bal
    for a in subscribes:
        if a.kind != ActionKind.SUBSCRIBE_EARN or not a.coin:
            continue
        if (a.coin or "").upper() in _STABLES:
            continue
        add = a.amount_native if a.amount_native is not None else Decimal(0)
        if add > 0:
            long_now[a.coin.upper()] = long_now.get(a.coin.upper(), Decimal(0)) + add
    # A planned LM subscribe creates a base-coin long this cycle (the
    # deposit rebalances 50/50). Count its base leg so the paired hedge
    # OPEN isn't seen as naked and trimmed. `amount` is the USD deposit;
    # half is the base leg, priced at the perp mark to match the hedge.
    for a in subscribes:
        if a.kind != ActionKind.SUBSCRIBE_LM:
            continue
        product = _lm_product_from_snapshot(snapshot, a.product_id)
        if product is None:
            continue
        parts = product.coin.split("/", 1)
        if len(parts) != 2:
            continue
        base = parts[0].upper()
        if not base or base in _STABLES:
            continue
        native = _lm_base_leg_native(a.amount * LM_BASE_LEG_FRACTION, base, snapshot)
        if native > 0:
            long_now[base] = long_now.get(base, Decimal(0)) + native
    # A planned REDEEM_EARN does NOT reduce long exposure here, regardless of
    # category. The staked coin stays as backing until it actually leaves Earn
    # and lands in the wallet — the next snapshot reflects that on its own
    # (earn row → 0, wallet credited). Pre-subtracting the in-flight redeem
    # would close the paired short while the coin is still settling → a naked
    # directional long for the whole settlement window. FlexibleSaving was
    # wrongly assumed instant; prod proved it is NOT (POPCAT 2026-06-09:
    # "redeem not credited within 180s"), the same hazard as OnChain
    # unbonding. The deterministic redeem-settle exit (gated on
    # `check_earn_redeem_settled`) closes the FULL short + sells the freed
    # coin atomically once it arrives; this backstop only trims shorts that
    # are already naked for other reasons.
    _ = redeems

    perp_short = _coin_to_perp_short_size(snapshot)
    for a in hedge_closes:
        if a.kind != ActionKind.CLOSE_PERP or not a.coin:
            continue
        coin_u = a.coin.upper()
        perp_short[coin_u] = max(
            Decimal(0), perp_short.get(coin_u, Decimal(0)) - a.amount
        )
    for a in hedge_opens:
        if a.kind != ActionKind.OPEN_PERP_SHORT or not a.coin:
            continue
        coin_u = a.coin.upper()
        perp_short[coin_u] = perp_short.get(coin_u, Decimal(0)) + a.amount

    closes: list[Action] = []
    cursor = idx_offset
    for coin_u, short in perp_short.items():
        if short <= 0:
            continue
        long_amt = long_now.get(coin_u, Decimal(0))
        naked = short - long_amt
        if naked <= 0:
            continue
        perp_info = (snapshot.perp_market or {}).get(coin_u) or (
            snapshot.perp_market or {}
        ).get(coin_u.title())
        qty_step = getattr(perp_info, "qty_step", None) if perp_info else None
        min_qty = getattr(perp_info, "min_order_qty", None) if perp_info else None
        qty = _round_to_qty_step(naked, qty_step, min_qty)
        if qty is None or qty <= 0:
            continue
        mark = getattr(perp_info, "mark_price", None) if perp_info else None
        notional_note = (
            f" ~${(qty * mark):.2f}" if mark and mark > 0 else ""
        )
        symbol = f"{coin_u}USDT"
        closes.append(
            Action(
                kind=ActionKind.CLOSE_PERP,
                category="linear",
                product_id=symbol,
                coin=coin_u,
                amount=qty,
                order_link_id=_order_link_id(snapshot_ts, cursor),
                reason=(
                    f"auto-close naked perp {coin_u} short: short {short} "
                    f"vs long {long_amt} → close {qty}{notional_note}"
                ),
            )
        )
        cursor += 1
    return closes
