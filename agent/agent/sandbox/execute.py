"""Sandbox executor — turns a validated `Decision` into Bybit Earn actions.

Closes the `.10` decide-only loop:

    snapshot → decide → validate → execute

Scope of `.11` + `.35` + `.47`:
- FlexibleSaving + OnChain subscribe/redeem via `BybitClient.place_earn_order`.
- DualAssets + DiscountBuy via `place_advance_earn_order` (`.35`).
- Liquidity Mining via `add_liquidity` / `remove_liquidity` (`.47`).
- SmartLeverage + DoubleWin remain `SKIP_OUT_OF_SCOPE` — they're
  conditional-payoff structured products without a single annualized
  rate (`.36`).
- Cash venue produces no action (it is residual — whatever isn't
  deployed elsewhere).

Safety:
- `--dry-run` is the default. Live execution requires `--live` explicitly.
- Idempotency keys: `orderLinkId = f"sandbox-{snapshot_ts}-{i:03d}"`. Bybit
  dedupes Earn orders by `orderLinkId` for ~30min, so a repeated dry-run
  → live promotion picks up where it left off without double-subscribing.
- Per-action log line in `executions/<snapshot_ts>.jsonl`: command,
  response, outcome — append-only, easy to grep for post-mortem.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import (
    BybitAPIError,
    BybitClient,
    EarnPosition,
)
from agent.reason.schema import Decision, Pick, VenueAllocation
from agent.reason.venues import VENUE_REGISTRY
from agent.sandbox.snapshot import SNAPSHOT_DIR, STABLES, PerpInfo, Snapshot

EXECUTIONS_DIR = Path(__file__).parent / "executions"
log = logging.getLogger(__name__)

# Minimum USDC-equivalent action size. Below this, rebalances are noise:
# fees + slippage dominate the yield uplift, and Bybit min-stake amounts
# for some products are around $10 anyway.
MIN_ACTION_USDC = Decimal("0.50")

# Stables-set used to assume 1:1 USD parity for sizing. Non-stable
# current positions (cmETH, TON, etc.) get their coin amount priced
# against `snapshot.perp_market[coin].mark_price` (.34) so the diff
# against the decision's target USD doesn't drift cycle-over-cycle as
# the underlying moves. Single source of truth lives in
# `agent.sandbox.snapshot.STABLES`.
#
# DO NOT EVER use this set (or any other gate) to restrict Earn picks
# to USDC-only. Operator hard rule (2026-05-27): all Bybit Earn products
# are eligible regardless of base coin. If the wallet lacks the pick's
# coin at execute time, build an auto-swap leg (USDC → pick.coin) ahead
# of the SUBSCRIBE_EARN — same shape as `_swap_actions_for_hedges`.
_STABLES = STABLES

# Earn account-type per category. FlexibleSaving runs on UNIFIED;
# OnChain Earn requires the FUND wallet per Bybit V5 spec. Advance-Earn
# (DualAssets, DiscountBuy) also runs on UNIFIED per V5 docs (`.35`).
_ACCOUNT_TYPE: dict[str, str] = {
    "FlexibleSaving": "UNIFIED",
    "OnChain": "FUND",
    "DualAssets": "UNIFIED",
    "DiscountBuy": "UNIFIED",
}

# Bybit Earn categories the executor knows how to drive. LM + advance-
# Earn are surfaced as out-of-scope skip actions.
_BASIC_EARN_CATEGORIES: frozenset[str] = frozenset({"FlexibleSaving", "OnChain"})

# Snapshot category string for Liquidity Mining picks (`.47`). Held as a
# constant so the diff and dispatch arms refer to the same string the
# venue registry uses (`bybit_lm.snapshot_category="LiquidityMining"`).
_LM_CATEGORY: str = "LiquidityMining"

# Bybit LM deposits the quote side of a max_leverage=1 LP pair from the
# UNIFIED wallet (where Earn redemptions and spot swaps also land). FUND
# would force a manual transfer first. Quote coin is per-product (USDC
# for ETH/USDC and BTC/USDC; USDT for everything else) — when the wallet
# lacks the quote stable, the diff emits a USDC→quote swap leg via
# `_swap_actions_for_earn_picks`, same shape as the USDT-margin swap for
# perp hedges. DO NOT restrict LM picks to USDC-quote; the operator hard
# rule (2026-05-27) applies to LM same as Earn — see `_STABLES` comment.
_LM_QUOTE_ACCOUNT_TYPE: str = "UNIFIED"


class ActionKind(StrEnum):
    SUBSCRIBE_EARN = "subscribe_earn"
    REDEEM_EARN = "redeem_earn"
    SUBSCRIBE_ADVANCE_EARN = "subscribe_advance_earn"
    SUBSCRIBE_LM = "subscribe_lm"
    REDEEM_LM = "redeem_lm"
    CLAIM_LM = "claim_lm"
    OPEN_PERP_SHORT = "open_perp_short"
    CLOSE_PERP = "close_perp"
    SWAP_SPOT = "swap_spot"
    SKIP_OUT_OF_SCOPE = "skip_out_of_scope"


# Advance-Earn categories the executor knows how to subscribe to (.35).
# DualAssets + DiscountBuy carry a usable APR from the quote endpoint.
# SmartLeverage + DoubleWin still SKIP — they're conditional-payoff
# structured products without a single annualized rate (`.36`).
_ADVANCE_EARN_CATEGORIES: frozenset[str] = frozenset({"DualAssets", "DiscountBuy"})


# A perp hedge is considered "the same size" as a current open position
# when their USD notionals differ by less than this fraction. Below the
# threshold we no-op; at or above, we close-and-reopen (simpler than
# partial reduce, and avoids guessing minOrderQty steps for the residual).
HEDGE_NOTIONAL_REBALANCE_THRESHOLD = Decimal("0.10")

# Buffer multiplier on top of the raw hedge notional when sizing the
# USDT margin reserve (`.33`). Covers Bybit's initial-margin rounding
# + headroom for funding/fees accumulation between cycles. 5% on a $50
# hedge = $2.5 extra — cheap insurance against retCode=110007.
HEDGE_MARGIN_BUFFER = Decimal("1.05")

# Don't swap pennies. Below this threshold the diff suppresses the
# SWAP action and trusts that Bybit's margin call won't fire on a
# sub-dollar gap. Mirrors `MIN_ACTION_USDC` philosophy.
MIN_SWAP_USDC = Decimal("1.00")


@dataclass
class Action:
    """One planned executor step. `amount` is in the product's coin
    (treated as USD-equivalent under `_STABLES`); `order_link_id`
    encodes the snapshot timestamp + sequence index for Bybit-side
    idempotency.

    `position_id` is populated only for REDEEM_LM actions — Bybit's
    remove-liquidity endpoint addresses a specific LP position by its
    server-side id (`/v5/earn/liquidity-mining/position.positionId`),
    not by product, since one product can host multiple positions
    (e.g. opened in different cycles). Other kinds leave it `None`.
    """

    kind: ActionKind
    category: str
    product_id: str
    coin: str
    amount: Decimal
    order_link_id: str
    reason: str
    position_id: str | None = None
    # Per-action overrides for dispatch parameters that don't fit the
    # flat field set. Currently used by REDEEM_LM to carry
    # `remove_rate` (1-100) for partial exits; default behavior when
    # absent is the full-exit path (remove_rate=100).
    extra: dict[str, Any] = field(default_factory=dict)

    def to_log(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["amount"] = str(self.amount)
        return d


@dataclass
class ActionResult:
    action: Action
    status: str  # "dry-run" | "ok" | "skipped" | "error"
    response: dict[str, Any] | None = None
    error: str | None = None
    started_at: str = ""
    finished_at: str = ""

    def to_log(self) -> dict[str, Any]:
        return {
            "action": self.action.to_log(),
            "status": self.status,
            "response": self.response,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ─── Diff: decision → actions ───────────────────────────────────────────────


def diff_to_actions(
    snapshot: Snapshot,
    decision: Decision,
    snapshot_ts: str,
    total_book_usd: Decimal | None = None,
) -> list[Action]:
    """Plan the action list. Redeems first (free USD), then subscribes,
    then out-of-scope skips for visibility.

    `total_book_usd` lets the caller override the sizing baseline; by
    default we read `snapshot.wallet.total_equity_usd`. The validator
    is responsible for vetoing the decision shape — this function
    trusts the decision and just translates it into orders.
    """
    if total_book_usd is None:
        total_book_usd = snapshot.wallet.total_equity_usd
    if total_book_usd <= 0:
        return []

    current = _current_positions_by_pid(
        snapshot.earn_positions, snapshot.perp_market
    )
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

    for idx, key in enumerate(sorted(all_pids)):
        category, product_id = key
        current_pos = current.get(key)
        target = targets.get(key)
        order_link_id = _order_link_id(snapshot_ts, idx)

        if category in _ADVANCE_EARN_CATEGORIES:
            # Advance-Earn subscribe path (`.35`). Redeem not wired —
            # DualAssets / DiscountBuy settle automatically at expiry.
            if target and target.amount_usd > MIN_ACTION_USDC:
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

        delta = target_amt - current_amt
        if abs(delta) < MIN_ACTION_USDC:
            continue

        if delta > 0:
            subscribes.append(
                Action(
                    kind=ActionKind.SUBSCRIBE_EARN,
                    category=category,
                    product_id=product_id,
                    coin=coin,
                    amount=delta,
                    order_link_id=order_link_id,
                    reason=(
                        f"subscribe to {category}/{product_id} ({coin}): "
                        f"target ${target_amt:.2f} - current ${current_amt:.2f}"
                    ),
                )
            )
        else:
            redeems.append(
                Action(
                    kind=ActionKind.REDEEM_EARN,
                    category=category,
                    product_id=product_id,
                    coin=coin,
                    amount=-delta,
                    order_link_id=order_link_id,
                    reason=(
                        f"redeem from {category}/{product_id} ({coin}): "
                        f"current ${current_amt:.2f} - target ${target_amt:.2f}"
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
    )
    hedge_swaps = _swap_actions_for_hedges(
        snapshot,
        hedge_opens,
        hedge_closes,
        snapshot_ts,
        idx_offset=len(all_pids) + len(hedge_closes) + len(hedge_opens),
    )
    # Earn-pick coin swaps run AFTER hedge swaps so the cursor doesn't
    # collide on orderLinkId and so subscribes coming next see the
    # wallet already topped up with their target coins.
    earn_swaps = _swap_actions_for_earn_picks(
        snapshot,
        subscribes,
        redeems,
        snapshot_ts,
        idx_offset=(
            len(all_pids)
            + len(hedge_closes)
            + len(hedge_opens)
            + len(hedge_swaps)
        ),
    )

    return (
        redeems
        + hedge_closes
        + hedge_swaps
        + earn_swaps
        + hedge_opens
        + subscribes
        + skips
    )


def _coin_from_perp_symbol(symbol: str) -> str:
    """Strip the USDT settle-coin suffix from a linear-perp symbol to
    get the base coin. Sandbox hedges are always USDT-settled (per
    `collect_snapshot`), so symbols not ending in `USDT` are not
    something this diff should touch — caller filters them out."""
    return symbol[:-4] if symbol.endswith("USDT") else symbol


# Snapshot categories whose non-stable picks get auto-hedged. Both
# FlexibleSaving and OnChain stake the underlying coin directly, so a
# non-stable pick produces directional spot exposure that needs a paired
# perp short to neutralize. LM is excluded — it's a paired LP (the quote
# side already hedges the base on average). Advance-Earn is excluded —
# DualAssets / DiscountBuy / SmartLeverage / DoubleWin are structured
# conditional products, not simple directional spot stakes.
_AUTO_HEDGE_CATEGORIES: frozenset[str] = frozenset(
    {"OnChain", "FlexibleSaving"}
)


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


def _hedge_diff_actions(
    snapshot: Snapshot,
    decision: Decision,
    snapshot_ts: str,
    *,
    idx_offset: int,
    total_book_usd: Decimal,
) -> tuple[list[Action], list[Action]]:
    """Compute `(closes, opens)` for the perp hedge layer. Target hedges
    are auto-derived from non-stable OnChain picks (see
    `_auto_hedge_targets`) — `decision.hedges` is informational only and
    NOT used for sizing here."""
    closes: list[Action] = []
    opens: list[Action] = []

    # Index current open shorts by base coin. Long positions in the
    # sandbox are not expected — surface as out-of-scope rather than
    # touching them (the executor is hedge-only).
    current_by_coin: dict[str, Any] = {}
    for pos in snapshot.perp_positions:
        if not pos.symbol.endswith("USDT"):
            continue
        coin = _coin_from_perp_symbol(pos.symbol)
        if pos.side != "Sell":
            # Long perp — not something the hedge layer produced. Skip
            # in plan; operator can deal with it manually.
            continue
        current_by_coin[coin] = pos

    targets_by_coin: dict[str, Decimal] = _auto_hedge_targets(
        decision, snapshot, total_book_usd
    )

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
        # drift exceeds the rebalance threshold.
        needs_close = pos is not None and (
            target is None
            or _notional_drifts(current_notional, target_notional)
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
            qty = (target_notional / info.mark_price).quantize(Decimal("0.001"))
            order_link_id = _order_link_id(snapshot_ts, cursor)
            cursor += 1
            opens.append(
                Action(
                    kind=ActionKind.OPEN_PERP_SHORT,
                    category="Perp",
                    product_id=info.symbol,
                    coin=coin,
                    amount=qty,
                    order_link_id=order_link_id,
                    reason=(
                        f"short {coin} ${target_notional:.2f} notional "
                        f"({qty} {coin}) @ mark ${info.mark_price:.4f}"
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


def _lm_product_from_snapshot(
    snapshot: Snapshot, product_id: str
):
    """Look up the LM `ProductSummary` for `product_id`. Returns the
    whole row (not just the pair) so the diff can also check
    `min_subscribe_usd` without a second pass through the list."""
    for p in snapshot.products.get(_LM_CATEGORY, []):
        if p.product_id == product_id:
            return p
    return None


def _current_lm_position(
    positions: list[dict[str, Any]], product_id: str
) -> tuple[str, Decimal] | None:
    """Return `(positionId, principal_usd)` for the active position on
    `product_id`, or `None` when no such position exists.

    Bybit's LM position payload carries `principalLiquidityValue` in the
    quote coin (USD-equivalent for USDC pairs). Fall back to summing
    `principalQuoteAmount + principalBaseAmount × currentPrice` when the
    consolidated field is absent. Zero principals collapse to None so
    the diff treats them as no-position rather than a $0 exit no-op.
    """
    for pos in positions:
        if str(pos.get("productId", "")) != product_id:
            continue
        pid = str(pos.get("positionId") or "")
        if not pid:
            continue
        principal = _lm_principal_usd(pos)
        if principal <= 0:
            return None
        return pid, principal
    return None


def _lm_principal_usd(pos: dict[str, Any]) -> Decimal:
    """Extract principal USD-equivalent from one LM position row. Prefers
    `principalLiquidityValue` (Bybit's server-side consolidation) when
    present; otherwise sums quote + base × currentPrice. Returns 0 on
    parse failure — caller treats as "not a real position"."""
    raw = pos.get("principalLiquidityValue")
    if raw is not None:
        try:
            return Decimal(str(raw))
        except (InvalidOperation, TypeError):
            pass
    try:
        quote = Decimal(str(pos.get("principalQuoteAmount", "0")))
        base = Decimal(str(pos.get("principalBaseAmount", "0")))
        price = Decimal(str(pos.get("currentPrice", "0")))
    except (InvalidOperation, TypeError):
        return Decimal(0)
    return quote + base * price


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
        return best[1] if best else None
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


_OFFER_PREFIX = " offer="


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
    mirror Bybit V5 docs verbatim — caller passes the result as `extra=`."""
    if category == "DualAssets":
        return {
            "dualAssetsExtra": {
                "selectPrice": offer.get("selectPrice"),
                "side": offer.get("side", "Buy"),
                "expiredTime": offer.get("expiredTime") or offer.get("expiredAt"),
                "apyE8": offer.get("apyE8"),
            }
        }
    if category == "DiscountBuy":
        return {
            "discountBuyExtra": {
                "instUid": offer.get("instUid"),
                "currentPrice": offer.get("currentPrice"),
                "purchasePrice": offer.get("purchasePrice"),
                "knockoutPrice": offer.get("knockoutPrice"),
                "knockoutCouponE8": offer.get("knockoutCouponE8"),
                "expiredAt": offer.get("expiredAt") or offer.get("expiredTime"),
            }
        }
    return {}


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
    required_per_coin: dict[str, Decimal] = {}
    for a in subscribe_actions:
        if a.kind not in (ActionKind.SUBSCRIBE_EARN, ActionKind.SUBSCRIBE_LM):
            continue
        coin = a.coin
        # Skip the source coin and anything we wouldn't recognize as
        # a stable — the perp-hedge swap layer handles USDT margin
        # separately, and we don't auto-convert into non-stables (an
        # OnChain non-USD pick already requires a paired hedge).
        if coin == "USDC":
            continue
        if coin not in _STABLES:
            continue
        required_per_coin[coin] = required_per_coin.get(coin, Decimal(0)) + a.amount

    # Pending REDEEM_EARN actions return their coin to the wallet
    # in-cycle, so credit them against the requirement before sizing
    # any swap. Mirrors the `hedge_closes` credit in
    # `_swap_actions_for_hedges`. Without this we'd double-fund a
    # rebalance (e.g. redeem $13 USD1 then swap USDC → USDT to
    # subscribe USDT, while the USD1 just sits idle).
    redeem_credit_per_coin: dict[str, Decimal] = {}
    for a in redeem_actions:
        if a.kind != ActionKind.REDEEM_EARN:
            continue
        redeem_credit_per_coin[a.coin] = (
            redeem_credit_per_coin.get(a.coin, Decimal(0)) + a.amount
        )

    swaps: list[Action] = []
    cursor = idx_offset
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
                order_link_id=_order_link_id(snapshot_ts, cursor),
                reason=(
                    f"swap {qty} USDC → {coin} for Earn/LM subscribe coverage "
                    f"(need ${need:.2f}, have ${available:.2f})"
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
) -> list[Action]:
    """Plan a USDC → USDT spot swap when the planned `OPEN_PERP_SHORT`
    actions need more USDT margin than UNIFIED currently holds (.33).

    Net USDT needed
        = sum(open notional × HEDGE_MARGIN_BUFFER)
          − snapshot.wallet.usdt_available_usd
          − sum(close notional)           # margin released by closes

    A `CLOSE_PERP` releases its IM back to UNIFIED as USDT, so we credit
    it against the requirement before sizing the swap. SKIP_OUT_OF_SCOPE
    hedge actions don't book real margin → excluded from the open side.

    The swap uses Bybit's `USDCUSDT` spot pair with `side="Sell"` — i.e.
    sell USDC (base) for USDT (quote). `qty` is the USDC amount to sell,
    treated 1:1 with the USDT shortfall (the spread on this stable pair
    is bps-level; the `HEDGE_MARGIN_BUFFER` already absorbs it).

    Returns an empty list when:
      - no real OPEN actions planned (only SKIPs or none),
      - existing USDT already covers the buffered requirement,
      - the residual shortfall is below `MIN_SWAP_USDC`.
    """
    real_opens = [
        a for a in hedge_opens if a.kind == ActionKind.OPEN_PERP_SHORT
    ]
    if not real_opens:
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

    required = open_notional * HEDGE_MARGIN_BUFFER
    available = snapshot.wallet.usdt_available_usd + close_notional
    shortfall = required - available

    if shortfall < MIN_SWAP_USDC:
        return []

    qty = shortfall.quantize(Decimal("0.01"))
    return [
        Action(
            kind=ActionKind.SWAP_SPOT,
            category="Spot",
            product_id="USDCUSDT",
            coin="USDT",  # target coin of the swap
            amount=qty,  # USDC to sell — Bybit Sell uses base-coin qty
            order_link_id=_order_link_id(snapshot_ts, idx_offset),
            reason=(
                f"swap {qty} USDC → USDT: hedge margin shortfall "
                f"(required ${required:.2f} with {HEDGE_MARGIN_BUFFER:.0%} "
                f"buffer; have ${snapshot.wallet.usdt_available_usd:.2f} "
                f"+ ${close_notional:.2f} from closes)"
            ),
        )
    ]


def _safe_decimal(value: str | None) -> Decimal:
    if value is None:
        return Decimal(0)
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return Decimal(0)


def _position_notional_usd(pos: Any | None, info: Any | None) -> Decimal:
    """Prefer Bybit's server-computed `positionValue` (size × markPrice
    at fetch time). Fall back to `size × snapshot.perp_market.mark_price`
    when the server didn't echo it — both are USD."""
    if pos is None:
        return Decimal(0)
    pv = _safe_decimal(pos.positionValue) if pos.positionValue else Decimal(0)
    if pv > 0:
        return pv
    size = _safe_decimal(pos.size)
    if info is not None and info.mark_price is not None and info.mark_price > 0:
        return size * info.mark_price
    return Decimal(0)


def _notional_drifts(current: Decimal, target: Decimal) -> bool:
    """True iff the current vs target USD notional differ enough to
    justify a close+reopen. When `target` is 0 the caller has already
    handled the close-only case, so this is only reached for both-sides
    populated. Guards against div-by-zero on a stale `current` value."""
    if target <= 0:
        return True
    diff = abs(current - target)
    return diff / target >= HEDGE_NOTIONAL_REBALANCE_THRESHOLD


# ─── Execution ──────────────────────────────────────────────────────────────


async def execute_actions(
    client: BybitClient,
    actions: list[Action],
    *,
    snapshot_ts: str,
    dry_run: bool = True,
    executions_dir: Path = EXECUTIONS_DIR,
) -> list[ActionResult]:
    """Execute actions sequentially. Returns per-action results AND
    writes them to `executions/<snapshot_ts>.jsonl` one-line-per-action.

    Sequential by design — Bybit Earn subscriptions affect the same
    wallet balance; running in parallel would risk insufficient-funds
    errors mid-batch when the first subscribe hasn't settled yet.
    """
    executions_dir.mkdir(parents=True, exist_ok=True)
    log_path = executions_dir / f"{snapshot_ts}.jsonl"
    results: list[ActionResult] = []
    with log_path.open("a") as log_file:
        for action in actions:
            res = await _execute_one(client, action, dry_run=dry_run)
            results.append(res)
            log_file.write(json.dumps(res.to_log()) + "\n")
            log_file.flush()
    return results


async def _execute_one(
    client: BybitClient, action: Action, *, dry_run: bool
) -> ActionResult:
    started = datetime.now(UTC).isoformat()
    if action.kind == ActionKind.SKIP_OUT_OF_SCOPE:
        return ActionResult(
            action=action,
            status="skipped",
            response=None,
            error=None,
            started_at=started,
            finished_at=datetime.now(UTC).isoformat(),
        )
    if dry_run:
        return ActionResult(
            action=action,
            status="dry-run",
            response=_dry_run_payload(action),
            started_at=started,
            finished_at=datetime.now(UTC).isoformat(),
        )

    try:
        if action.kind == ActionKind.OPEN_PERP_SHORT:
            # Force 1x leverage before placing — Bybit defaults a fresh
            # symbol to ~10x cross, which would magnify mark-price drift
            # on a delta-neutral hedge. set_leverage is idempotent.
            await client.set_leverage(action.product_id, 1)
            out = await client.place_perp_order(
                symbol=action.product_id,
                side="Sell",
                qty=str(action.amount),
                order_link_id=action.order_link_id,
            )
            response = {"orderId": out.orderId}
        elif action.kind == ActionKind.CLOSE_PERP:
            # Buy-to-close the short. `reduce_only=True` so we can't
            # accidentally flip into a long if the size we computed is
            # larger than the actual remaining position (e.g. partial
            # external close between snapshot and execution).
            out = await client.place_perp_order(
                symbol=action.product_id,
                side="Buy",
                qty=str(action.amount),
                reduce_only=True,
                order_link_id=action.order_link_id,
            )
            response = {"orderId": out.orderId}
        elif action.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN:
            # `.35` + 2026-05-29 follow-up: dispatch DualAssets /
            # DiscountBuy stake. Refresh the quote at execute time
            # because Bybit rotates offers every 30-60s and the diff-
            # time offer encoded in `action.reason` may already be past
            # `expiredAt`. If the refresh fails (network, rate limit,
            # transient 5xx), fall back to the diff-time offer — stale
            # is at least an attempt vs failing the whole pick.
            fresh_offer: dict[str, Any] | None = None
            try:
                fresh_quote = await client.get_advance_product_quote(
                    category=action.category, product_id=action.product_id
                )
                fresh_offer = _pick_offer_for_execute(
                    action.category, fresh_quote
                )
            except BybitAPIError as e:
                log.warning(
                    "advance-Earn quote refresh failed for %s/%s: "
                    "retCode=%s %s — falling back to diff-time offer",
                    action.category, action.product_id, e.ret_code, e.ret_msg,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "advance-Earn quote refresh raised %s for %s/%s — "
                    "falling back to diff-time offer",
                    type(e).__name__, action.category, action.product_id,
                )
            offer = fresh_offer or _decode_offer_from_reason(action.reason)
            if not offer:
                raise BybitAPIError(
                    0,
                    "no usable offer at execute time (fresh quote rotated, "
                    "diff-time fallback empty)",
                    "/v5/earn/advance/place-order",
                )
            extra = _build_advance_extra(action.category, offer)
            raw = await client.place_advance_earn_order(
                category=action.category,
                product_id=action.product_id,
                side="Stake",
                coin=action.coin,
                amount=str(action.amount),
                account_type=_ACCOUNT_TYPE[action.category],  # type: ignore[arg-type]
                order_link_id=action.order_link_id,
                extra=extra,
            )
            response = {"orderId": raw.get("orderId")}
        elif action.kind == ActionKind.SUBSCRIBE_LM:
            # `.47`: single-sided USDC deposit into an LM LP pair at
            # leverage=1. Bybit's CPMM pool rebalances 50/50 to base
            # internally at spot — we don't supply baseAmount. Validator
            # forbids leverage>1 picks; hardcoded "1" here mirrors the
            # _LM_QUOTE_ACCOUNT_TYPE constant choice (UNIFIED, where
            # USDC sits post-Earn-redeem).
            lm_out = await client.add_liquidity(
                product_id=action.product_id,
                order_link_id=action.order_link_id,
                quote_amount=str(action.amount),
                quote_account_type=_LM_QUOTE_ACCOUNT_TYPE,  # type: ignore[arg-type]
                leverage="1",
            )
            response = {"orderId": lm_out.orderId}
        elif action.kind == ActionKind.REDEEM_LM:
            # Full exit by default (removeRate=100, removeType=Normal —
            # returns both coins pro-rata). The diff guarantees we
            # only reach here with a valid `position_id` from the
            # snapshot's lm_positions; missing id would be a programming
            # error, not a recoverable runtime state.
            if not action.position_id:
                raise RuntimeError(
                    f"REDEEM_LM action {action.order_link_id} missing "
                    "position_id — diff layer must populate this"
                )
            remove_rate = int(action.extra.get("remove_rate", 100))
            lm_out = await client.remove_liquidity(
                product_id=action.product_id,
                position_id=action.position_id,
                order_link_id=action.order_link_id,
                remove_rate=remove_rate,
                remove_type="Normal",
            )
            response = {"orderId": lm_out.orderId}
        elif action.kind == ActionKind.CLAIM_LM:
            # `productId="-1"` claims yield across every active LM
            # position in one round-trip. Yield lands in Funding. No
            # response payload to capture; we just record the call.
            await client.claim_lm_interest(product_id=action.product_id)
            response = {"claimed": True}
        elif action.kind == ActionKind.SWAP_SPOT:
            # USDC → USDT via the USDCUSDT spot pair. Bybit's spot
            # Market Sell uses base-coin qty (USDC here); Market Buy
            # would use quote, which is the asymmetry flagged in `.27`.
            # We always swap by selling USDC, so `qty` is always base.
            out = await client.place_spot_order(
                symbol=action.product_id,
                side="Sell",
                qty=str(action.amount),
                order_link_id=action.order_link_id,
            )
            response = {"orderId": out.orderId}
        else:
            side = "Stake" if action.kind == ActionKind.SUBSCRIBE_EARN else "Redeem"
            account_type = _ACCOUNT_TYPE[action.category]
            earn_out = await client.place_earn_order(
                category=action.category,  # type: ignore[arg-type]
                product_id=action.product_id,
                amount=str(action.amount),
                side=side,  # type: ignore[arg-type]
                coin=action.coin,
                account_type=account_type,  # type: ignore[arg-type]
                order_link_id=action.order_link_id,
            )
            response = {"orderId": earn_out.orderId}
    except BybitAPIError as e:
        return ActionResult(
            action=action,
            status="error",
            response=None,
            error=f"retCode={e.ret_code} {e.ret_msg}",
            started_at=started,
            finished_at=datetime.now(UTC).isoformat(),
        )
    except Exception as e:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="error",
            response=None,
            error=f"{type(e).__name__}: {e}",
            started_at=started,
            finished_at=datetime.now(UTC).isoformat(),
        )

    return ActionResult(
        action=action,
        status="ok",
        response=response,
        started_at=started,
        finished_at=datetime.now(UTC).isoformat(),
    )


def _dry_run_payload(action: Action) -> dict[str, Any]:
    if action.kind == ActionKind.OPEN_PERP_SHORT:
        return {
            "would_call": "place_perp_order",
            "side": "Sell",
            "symbol": action.product_id,
            "qty": str(action.amount),
            "leverage": 1,
            "order_link_id": action.order_link_id,
        }
    if action.kind == ActionKind.CLOSE_PERP:
        return {
            "would_call": "place_perp_order",
            "side": "Buy",
            "symbol": action.product_id,
            "qty": str(action.amount),
            "reduce_only": True,
            "order_link_id": action.order_link_id,
        }
    if action.kind == ActionKind.SWAP_SPOT:
        return {
            "would_call": "place_spot_order",
            "side": "Sell",
            "symbol": action.product_id,
            "qty": str(action.amount),
            "order_link_id": action.order_link_id,
        }
    if action.kind == ActionKind.SUBSCRIBE_LM:
        return {
            "would_call": "add_liquidity",
            "product_id": action.product_id,
            "quote_amount": str(action.amount),
            "quote_account_type": _LM_QUOTE_ACCOUNT_TYPE,
            "leverage": "1",
            "order_link_id": action.order_link_id,
        }
    if action.kind == ActionKind.REDEEM_LM:
        return {
            "would_call": "remove_liquidity",
            "product_id": action.product_id,
            "position_id": action.position_id,
            "remove_rate": int(action.extra.get("remove_rate", 100)),
            "remove_type": "Normal",
            "order_link_id": action.order_link_id,
        }
    if action.kind == ActionKind.CLAIM_LM:
        return {
            "would_call": "claim_lm_interest",
            "product_id": action.product_id,
        }
    if action.kind == ActionKind.SUBSCRIBE_ADVANCE_EARN:
        offer = _decode_offer_from_reason(action.reason)
        return {
            "would_call": "place_advance_earn_order",
            "side": "Stake",
            "category": action.category,
            "product_id": action.product_id,
            "amount": str(action.amount),
            "coin": action.coin,
            "extra": _build_advance_extra(action.category, offer),
            "order_link_id": action.order_link_id,
        }
    return {
        "would_call": "place_earn_order",
        "side": "Stake" if action.kind == ActionKind.SUBSCRIBE_EARN else "Redeem",
        "category": action.category,
        "product_id": action.product_id,
        "amount": str(action.amount),
        "coin": action.coin,
        "order_link_id": action.order_link_id,
    }


# ─── Helpers ────────────────────────────────────────────────────────────────


@dataclass
class _CurrentPos:
    coin: str
    amount_usd: Decimal


@dataclass
class _TargetPos:
    coin: str
    amount_usd: Decimal


def _current_positions_by_pid(
    positions: list[Any],
    perp_market: dict[str, PerpInfo] | None = None,
) -> dict[tuple[str, str], _CurrentPos]:
    """Index Earn positions by `(category, product_id)` with USD-equivalent
    sizing. Stable-coin amounts are taken at 1:1 USD parity; non-stable
    balances are priced via `perp_market[coin].mark_price` (`.34`) — the
    same coin → USDT pair the hedge layer uses, so executor and validator
    agree on what the position is worth. A non-stable position without
    a matching `perp_market` entry collapses to USD=0: better to treat
    as "unknown size, may re-subscribe" than to silently mis-size by
    treating coin units as dollars.

    Pydantic `EarnPosition` instances and raw dicts are both accepted so
    tests can build fixtures inline."""
    perp_market = perp_market or {}
    out: dict[tuple[str, str], _CurrentPos] = {}
    for p in positions:
        if hasattr(p, "model_dump"):
            data = p.model_dump(mode="python")
        else:
            data = p
        category = data.get("category") or ""
        pid = str(data.get("productId") or data.get("product_id") or "")
        if not category or not pid:
            continue
        try:
            amt = Decimal(str(data.get("amount", "0")))
        except (InvalidOperation, TypeError):
            amt = Decimal(0)
        if amt <= 0:
            continue
        coin = data.get("coin") or "USDC"
        amount_usd = _amount_to_usd(coin, amt, perp_market)
        out[(category, pid)] = _CurrentPos(coin=coin, amount_usd=amount_usd)
    return out


def _amount_to_usd(
    coin: str,
    amount: Decimal,
    perp_market: dict[str, PerpInfo],
) -> Decimal:
    """USD equivalent of `amount` of `coin`. Stables 1:1; non-stables via
    the perp pair's `mark_price`. Returns 0 when a non-stable coin lacks
    a mark — caller treats it as "unknown current value", which downgrades
    to a no-delta planning decision rather than a silently-wrong one."""
    if coin.upper() in _STABLES:
        return amount
    info = perp_market.get(coin) or perp_market.get(coin.upper())
    if info is None or info.mark_price is None or info.mark_price <= 0:
        return Decimal(0)
    return amount * info.mark_price


def _target_usd_by_pid(
    decision: Decision,
    total_book_usd: Decimal,
    snapshot: Snapshot,
) -> dict[tuple[str, str], _TargetPos]:
    """Convert venue + pick weights into per-product USD targets. Cash
    venue has no picks. Non-stable picks are kept in the target (so a
    redeem-direction action against a non-stable current position can
    still be planned), but the executor itself only places orders on
    stable-coin categories.

    The pick's underlying coin is resolved from `snapshot.products` —
    Bybit's `/v5/earn/place-order` rejects mismatched `coin` vs product
    with `retCode=180008 Invalid Product`, so we must send the coin
    matching the product (e.g. `1131` → `USD1`, `1` → `USDT`). The
    placeholder fallback (`USDC`) only fires when the LLM picks a
    product that isn't surfaced in the snapshot at all (which should
    have been caught by `check_hallucinated_picks` already)."""
    out: dict[tuple[str, str], _TargetPos] = {}
    for v in decision.venues:
        meta = VENUE_REGISTRY[v.venue_id]
        if not meta.snapshot_category or not v.picks:
            continue
        category = meta.snapshot_category
        product_coin = {
            p.product_id: p.coin
            for p in snapshot.products.get(category, [])
        }
        for pick in v.picks:
            usd_amount = total_book_usd * Decimal(str(v.weight)) * Decimal(str(pick.weight))
            out[(category, pick.product_id)] = _TargetPos(
                coin=product_coin.get(pick.product_id, "USDC"),
                amount_usd=usd_amount,
            )
    return out


def _order_link_id(snapshot_ts: str, idx: int) -> str:
    return f"sandbox-{snapshot_ts}-{idx:03d}"


def _load_paired_snapshot(decision_path: Path) -> tuple[Snapshot, dict[str, Any], str]:
    """Read the decision JSON, locate its paired snapshot via the
    `_meta.snapshot_filename` sidecar that `write_decision` writes, and
    parse the snapshot. Returns `(snapshot, raw_decision_dict, snapshot_ts)`.
    """
    raw_decision = json.loads(decision_path.read_text())
    meta = raw_decision.get("_meta") or {}
    snap_name = meta.get("snapshot_filename")
    if not snap_name:
        raise RuntimeError(
            f"decision {decision_path} has no _meta.snapshot_filename"
        )
    snap_path = Path(SNAPSHOT_DIR) / snap_name
    if not snap_path.is_file():
        raise RuntimeError(f"paired snapshot not found: {snap_path}")
    raw_snapshot = json.loads(snap_path.read_text())
    snap = Snapshot.model_validate(raw_snapshot)
    ts = snap_path.stem  # `<UTC ts>` without `.json`
    return snap, raw_decision, ts


# ─── CLI ────────────────────────────────────────────────────────────────────


DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE = 0.6


def request_approval(
    decision: Decision,
    actions: list[Action],
    *,
    yes: bool,
    min_confidence: float,
    stdin: Any = None,
    input_fn: Any = None,
) -> bool:
    """Return True if the operator (or the auto-approve guard) signs off
    on live execution. `.12` approval gate.

    Three paths:
    1. `--yes` flag AND `decision.confidence >= min_confidence` → auto-approve
       (intended for the loop driver `.13` once a few cycles have run
       interactively and the operator trusts the model).
    2. Interactive terminal (`stdin.isatty()`) → prompt `y/N`; anything
       other than `y` / `yes` aborts.
    3. Non-interactive stdin + no `--yes` → refuse. This is the safety
       valve: a cron / CI invocation can't accidentally place orders
       without an explicit blanket approval.

    The `stdin` arg is the injection seam for tests; production passes
    `None` (defaults to `sys.stdin`).
    """
    stdin = stdin if stdin is not None else sys.stdin
    prompt = input_fn if input_fn is not None else input

    plan_summary = _render_plan_summary(actions)
    print()
    print("=== APPROVAL REQUIRED (live execution) ===")
    print(
        f"confidence={decision.confidence:.2f}  "
        f"expected_apr={decision.expected_blended_apr_pct:.2f}%  "
        f"risk_flags={decision.risk_flags}"
    )
    print(plan_summary)

    if yes:
        if decision.confidence >= min_confidence:
            print(
                f"--yes accepted (confidence {decision.confidence:.2f} "
                f">= min {min_confidence:.2f}). Proceeding."
            )
            return True
        print(
            f"--yes ignored: confidence {decision.confidence:.2f} "
            f"below auto-approve floor {min_confidence:.2f}. "
            "Falling back to interactive prompt."
        )

    if not stdin.isatty():
        print(
            "stdin is not a TTY and --yes is not active (or confidence "
            "below floor). Refusing to execute — abort.",
            file=sys.stderr,
        )
        return False

    try:
        resp = prompt("Execute live? [y/N] ").strip().lower()
    except EOFError:
        return False
    return resp in ("y", "yes", "д", "да")


def _render_plan_summary(actions: list[Action]) -> str:
    """Group actions by kind for a human-readable diff to approve."""
    lines: list[str] = []
    by_kind: dict[ActionKind, list[Action]] = {}
    for a in actions:
        by_kind.setdefault(a.kind, []).append(a)
    for kind in (
        ActionKind.REDEEM_EARN,
        ActionKind.REDEEM_LM,
        ActionKind.CLAIM_LM,
        ActionKind.CLOSE_PERP,
        ActionKind.SWAP_SPOT,
        ActionKind.OPEN_PERP_SHORT,
        ActionKind.SUBSCRIBE_EARN,
        ActionKind.SUBSCRIBE_ADVANCE_EARN,
        ActionKind.SUBSCRIBE_LM,
        ActionKind.SKIP_OUT_OF_SCOPE,
    ):
        rows = by_kind.get(kind, [])
        if not rows:
            continue
        lines.append(f"  {kind.value} ({len(rows)}):")
        for a in rows:
            lines.append(
                f"    - {a.category}/{a.product_id} {a.coin} "
                f"${a.amount:.2f}"
            )
    return "\n".join(lines)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Execute a sandbox decision against Bybit Earn.")
    parser.add_argument(
        "--decision",
        type=Path,
        required=True,
        help="Path to a decision JSON written by agent.sandbox.decide",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually place orders on Bybit. Default is dry-run.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Skip the interactive y/N approval prompt when running --live, "
            "provided decision.confidence >= --min-confidence. For "
            "scripted / cron use after a few interactive cycles."
        ),
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE,
        help=(
            f"Auto-approve floor for --yes (default "
            f"{DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE}). Below this, --yes "
            "is ignored and the interactive prompt runs instead."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="dotenv to load (e.g. .env at repo root)",
    )
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(args.env_file, override=True)

    snap, raw_decision, snapshot_ts = _load_paired_snapshot(args.decision)
    # Reuse Decision from the raw — sandbox/decide wrote a pydantic-
    # validated decision plus _meta + optional _validator, so this is
    # round-trippable.
    decision_payload = {
        k: v for k, v in raw_decision.items() if not k.startswith("_")
    }
    decision = Decision.model_validate(decision_payload)

    actions = diff_to_actions(snap, decision, snapshot_ts)
    if not actions:
        print(f"no actions needed (book ${snap.wallet.total_equity_usd:.2f}, "
              f"decision matches current allocation within threshold)")
        return

    print(f"=== plan ({len(actions)} actions, dry_run={not args.live}) ===")
    for a in actions:
        print(f"  [{a.kind.value:22}] {a.category}/{a.product_id} {a.coin} "
              f"amount=${a.amount:.2f}  ({a.reason})")

    # `.12` approval gate. Dry-run skips; live requires interactive y/N
    # OR --yes-above-confidence. If approval is declined, downgrade to
    # a dry-run pass so the operator still gets a logged plan.
    effective_dry_run = not args.live
    if args.live:
        approved = request_approval(
            decision,
            actions,
            yes=args.yes,
            min_confidence=args.min_confidence,
        )
        if not approved:
            print("approval declined — downgrading to dry-run.")
            effective_dry_run = True

    async def run() -> None:
        async with BybitClient.from_settings() as client:
            results = await execute_actions(
                client, actions, snapshot_ts=snapshot_ts, dry_run=effective_dry_run
            )
        print(f"=== results ===")
        for r in results:
            print(f"  [{r.status:8}] {r.action.kind.value:22} "
                  f"{r.action.category}/{r.action.product_id} "
                  f"{r.action.coin} ${r.action.amount:.2f}"
                  + (f"  err={r.error}" if r.error else ""))
        print(f"  log: {EXECUTIONS_DIR / (snapshot_ts + '.jsonl')}")

    asyncio.run(run())


if __name__ == "__main__":
    _main()
