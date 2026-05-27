"""Sandbox executor — turns a validated `Decision` into Bybit Earn actions.

Closes the `.10` decide-only loop:

    snapshot → decide → validate → execute

Scope of `.11`:
- FlexibleSaving + OnChain subscribe/redeem via `BybitClient.place_earn_order`.
- LM + advance-Earn picks are surfaced as `SKIP_OUT_OF_SCOPE` actions —
  their lifecycle (LP add/remove, settlement windows, quote-extra-info
  reservations) differs structurally and lands in a follow-up.
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


class ActionKind(StrEnum):
    SUBSCRIBE_EARN = "subscribe_earn"
    REDEEM_EARN = "redeem_earn"
    SUBSCRIBE_ADVANCE_EARN = "subscribe_advance_earn"
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
    """

    kind: ActionKind
    category: str
    product_id: str
    coin: str
    amount: Decimal
    order_link_id: str
    reason: str

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
    targets = _target_usd_by_pid(decision, total_book_usd)

    redeems: list[Action] = []
    subscribes: list[Action] = []
    skips: list[Action] = []

    # All product_ids touched by current OR target — both sides matter:
    # currents not in target should be fully redeemed.
    all_pids: set[tuple[str, str]] = set(current.keys()) | set(targets.keys())

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
    )
    swaps = _swap_actions_for_hedges(
        snapshot,
        hedge_opens,
        hedge_closes,
        snapshot_ts,
        idx_offset=len(all_pids) + len(hedge_closes) + len(hedge_opens),
    )

    return redeems + hedge_closes + swaps + hedge_opens + subscribes + skips


def _coin_from_perp_symbol(symbol: str) -> str:
    """Strip the USDT settle-coin suffix from a linear-perp symbol to
    get the base coin. Sandbox hedges are always USDT-settled (per
    `collect_snapshot`), so symbols not ending in `USDT` are not
    something this diff should touch — caller filters them out."""
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _hedge_diff_actions(
    snapshot: Snapshot,
    decision: Decision,
    snapshot_ts: str,
    *,
    idx_offset: int,
) -> tuple[list[Action], list[Action]]:
    """Compute `(closes, opens)` for the perp hedge layer. See caller
    for the sequencing rationale; pulled out so the surface area for
    tests stays narrow."""
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

    targets_by_coin: dict[str, Any] = {}
    for h in decision.hedges:
        targets_by_coin[h.coin.upper()] = h

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
        target_notional = (
            Decimal(str(abs(target.notional_usd))) if target else Decimal(0)
        )

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


def _advance_earn_subscribe_action(
    snapshot: Snapshot,
    category: str,
    product_id: str,
    target_amount_usd: Decimal,
    order_link_id: str,
) -> Action:
    """Build the SUBSCRIBE_ADVANCE_EARN action for a DualAssets or
    DiscountBuy pick by pulling the cached quote from the snapshot and
    selecting an active offer. Returns SKIP_OUT_OF_SCOPE when:

    - the quote is missing entirely (top-K window didn't include this
      product, or the per-product quote call failed),
    - all offers are past `expiredAt` (snapshot too old vs settlement
      window — Bybit offers usually rotate every few minutes for
      DualAssets, longer for DiscountBuy),
    - we don't recognize the category-specific offer shape.

    The chosen offer is stashed in `Action.reason` (operator audit) and
    the `coin` / `amount` fields are set so the executor branch has
    enough to build the per-category `*Extra` block at dispatch time —
    actually re-reading the quote then would race against expiry.
    Instead we encode everything into the Action up front: that snapshot
    is the source of truth for this cycle.
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

    offer, coin, reason_detail = _pick_advance_offer(category, quote)
    if offer is None:
        return Action(
            kind=ActionKind.SKIP_OUT_OF_SCOPE,
            category=category,
            product_id=product_id,
            coin=coin or "?",
            amount=target_amount_usd,
            order_link_id=order_link_id,
            reason=(
                f"{category}/{product_id}: no usable offer "
                f"({reason_detail})"
            ),
        )

    # Encode the per-category offer details into the action's reason so
    # the dispatch can rebuild the extra block without re-parsing the
    # whole quote (and so post-mortems can see WHY this strike).
    serialized_offer = json.dumps(offer, sort_keys=True, default=str)
    return Action(
        kind=ActionKind.SUBSCRIBE_ADVANCE_EARN,
        category=category,
        product_id=product_id,
        coin=coin,
        amount=target_amount_usd,
        order_link_id=order_link_id,
        reason=(
            f"subscribe {category}/{product_id} ({coin}) ${target_amount_usd:.2f}: "
            f"{reason_detail} offer={serialized_offer}"
        ),
    )


def _pick_advance_offer(
    category: str, quote: dict[str, Any]
) -> tuple[dict[str, Any] | None, str, str]:
    """Return `(offer_dict_or_None, subscription_coin, reason_detail)`
    for the best actionable offer in `quote` per category-specific shape.

    DualAssets quote shape (`.28`):
        {category, list: [{currentPrice, expiredTime, baseCoin, quoteCoin,
        buyLowPrice: [{selectPrice, apyE8, ...}], sellHighPrice: [...]}]}
    We pick the highest-APR `buyLowPrice` offer (strike below current →
    we commit to *buying* the base coin at a discount if price drops;
    stake currency is the quote). High-APR strike is the one closest to
    current price → most likely conversion, but also most yield per Bybit's
    pricing curve.

    DiscountBuy quote shape:
        {category, list: [{purchasePrice, currentPrice, knockoutPrice,
        knockoutCouponE8, instUid, expiredAt, ...}]}
    Use `list[0]` (the single posted offer per product). Stake currency
    is whatever the product's `coin` field says (typically USDT).

    `expiredAt` / `expiredTime` (Bybit uses both spellings across categories)
    is checked against the current wall clock — past = unusable.
    """
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    if category == "DualAssets":
        items = quote.get("list") or []
        if not items or not isinstance(items[0], dict):
            return None, "?", "empty quote list"
        payload = items[0]
        base = payload.get("baseCoin", "?")
        quote_coin = payload.get("quoteCoin", "?")
        coin = quote_coin  # stake currency for the buyLowPrice side
        expired = payload.get("expiredTime") or payload.get("expiredAt")
        if _offer_expired(expired, now_ms):
            return None, coin, f"all offers past expiredTime={expired}"
        best: tuple[Decimal, dict[str, Any]] | None = None
        for offer in payload.get("buyLowPrice") or []:
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
            return None, coin, "no buyLowPrice offers with apyE8"
        apy, offer = best
        return offer, coin, (
            f"DualAssets {base}/{quote_coin} buyLowPrice strike="
            f"{offer.get('selectPrice')} apy={apy:.4f}"
        )

    if category == "DiscountBuy":
        items = quote.get("list") or []
        if not items or not isinstance(items[0], dict):
            return None, "?", "empty quote list"
        offer = items[0]
        coin = offer.get("coin") or "USDT"
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
            # `.35`: dispatch DualAssets / DiscountBuy stake. Offer was
            # pinned at diff time (encoded in action.reason) — rebuild
            # the `*Extra` block from it instead of refetching the quote
            # (which could rotate between diff and execute and silently
            # change the strike we're committing to).
            offer = _decode_offer_from_reason(action.reason)
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
    decision: Decision, total_book_usd: Decimal
) -> dict[tuple[str, str], _TargetPos]:
    """Convert venue + pick weights into per-product USD targets. Cash
    venue has no picks. Non-stable picks are kept in the target (so a
    redeem-direction action against a non-stable current position can
    still be planned), but the executor itself only places orders on
    stable-coin categories."""
    out: dict[tuple[str, str], _TargetPos] = {}
    for v in decision.venues:
        meta = VENUE_REGISTRY[v.venue_id]
        if not meta.snapshot_category or not v.picks:
            continue
        category = meta.snapshot_category
        for pick in v.picks:
            usd_amount = total_book_usd * Decimal(str(v.weight)) * Decimal(str(pick.weight))
            out[(category, pick.product_id)] = _TargetPos(
                coin="USDC",  # placeholder; real coin resolved from snapshot if needed
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
        ActionKind.CLOSE_PERP,
        ActionKind.SWAP_SPOT,
        ActionKind.OPEN_PERP_SHORT,
        ActionKind.SUBSCRIBE_EARN,
        ActionKind.SUBSCRIBE_ADVANCE_EARN,
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
