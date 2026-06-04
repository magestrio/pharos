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
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import (
    BybitAPIError,
    BybitClient,
    BybitOrderError,
    EarnPosition,
    TERMINAL_BAD_SPOT_STATUSES,
)
from agent.reason.schema import Decision, Pick, VenueAllocation
from agent.reason.venues import (
    BASIC_EARN_CATEGORIES,
    CARRY_CATEGORY,
    CARRY_VENUE_ID,
    VENUE_REGISTRY,
)
from agent.sandbox.carry_state import (
    CarryPositionRecord,
    CarryState,
)
from agent.sandbox.snapshot import (
    HEDGE_MARGIN_BUFFER,
    SNAPSHOT_DIR,
    STABLES,
    PerpInfo,
    Snapshot,
)

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

# Coins known to have liquid USDC-quote spot pairs on Bybit. When an
# orphan spot balance accumulates in one of these (post-DiscountBuy
# settlement is the canonical case: BTC/ETH lands in UNIFIED after a 1d
# DiscountBuy without knockout), the cleanup sell routes directly to
# USDC via `{coin}USDC` instead of the default `{coin}USDT` path —
# avoids the double-fee BTC→USDT→USDC round-trip and keeps the vault's
# USDC denomination. Conservative whitelist; expanding it is a one-line
# registry change once Bybit-side liquidity on a new pair clears the
# per-cycle MIN_SWAP_USDC threshold. `.49`.
_USDC_PAIR_COINS: frozenset[str] = frozenset({"BTC", "ETH", "SOL"})


def _orphan_sell_quote(coin_u: str) -> tuple[str, str]:
    """Pick the spot pair + destination coin for an orphan sell. Returns
    `(symbol, dest_coin)`. USDC is preferred for the whitelisted coins
    so the vault rebases to USDC directly; everything else falls back
    to the universal USDT quote."""
    if coin_u in _USDC_PAIR_COINS:
        return f"{coin_u}USDC", "USDC"
    return f"{coin_u}USDT", "USDT"

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
# Earn are surfaced as out-of-scope skip actions. Imported from
# `agent.reason.venues.BASIC_EARN_CATEGORIES` so it can't drift from
# the validator's auto-hedge venue list (single source: VENUE_REGISTRY's
# `snapshot_category` for the hedge venues).
_BASIC_EARN_CATEGORIES = BASIC_EARN_CATEGORIES

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

# Snapshot category string for Bybit Alpha Farm picks (`.52` / `.54`).
_ALPHA_CATEGORY: str = "AlphaFarm"

# Bybit Alpha purchases pay in USDT by convention — Alpha's pay-token-list
# returns USDT as `CEX_1` (verified against docs 2026-05-29). We hardcode
# this for the diff/dispatch path: when a USDC-denominated target weight
# lands on Alpha, the executor needs to swap USDC → USDT first then
# `alpha_purchase` from USDT. The swap leg piggybacks on existing
# `_USDC_USDT_SPOT_SYMBOL` logic from `.33`. The CEX_<id> mapping is
# environment-dependent — if Bybit ever reassigns IDs, `list_alpha_pay_tokens`
# resolves the right code at runtime (deferred; hardcoded MVP).
_ALPHA_PAY_TOKEN_CODE: str = "CEX_1"  # USDT

# Default slippage tolerance for alpha purchases. 0.01 = 1%; tight enough
# that we don't take a haircut on calm tokens, loose enough that mid-vol
# tokens don't fail with `slippage too tight` rejections. The user can
# override via `VAULT_ALPHA_SLIPPAGE` env var if Bybit's `slippage` field
# in the quote response suggests a different floor for a specific token.
_ALPHA_DEFAULT_SLIPPAGE: str = os.getenv("VAULT_ALPHA_SLIPPAGE", "0.01")

# Alpha execute gate (`.54`). Off by default — `.14` smoke test is the
# blocking guard, AND the Alpha endpoints have NOT been live-probed
# against the sandbox sub-account as of 2026-05-29. When False, the diff
# emits SKIP_OUT_OF_SCOPE for any AlphaFarm target so the live loop
# stays clean. Flip via env `VAULT_ALPHA_EXEC_ENABLED=1` once you've
# (a) closed `.14`, (b) live-probed `/v5/alpha/trade/biz-token-list` +
# `/v5/alpha/trade/biz-token-price-list` + `/v5/alpha/asset`, (c)
# confirmed the sub-account has Alpha permission and at least one
# pickable token surfaces in the snapshot.
ALPHA_EXEC_ENABLED: bool = os.getenv("VAULT_ALPHA_EXEC_ENABLED", "0") == "1"


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
    ALPHA_PURCHASE = "alpha_purchase"
    ALPHA_REDEEM = "alpha_redeem"
    # Funding-carry compound actions (`bybit-strategy-expansion.5`).
    # OPEN dispatches: set_leverage(1) → spot Buy → paired-notional
    # check → perp Sell, atomic-pair guard between legs. CLOSE: spot
    # Sell → perp Buy reduce-only, same guard.
    OPEN_FUNDING_CARRY = "open_funding_carry"
    CLOSE_FUNDING_CARRY = "close_funding_carry"
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

# HEDGE_MARGIN_BUFFER lives in `agent.sandbox.snapshot` so the validator
# (`check_stable_spend_cap`) and the executor (USDT-budget enforcement)
# can never go out of sync on the buffer multiplier — both read the same
# constant. Re-exported here for back-compat with internal references.

# Don't swap pennies. Below this threshold the diff suppresses the
# SWAP action and trusts that Bybit's margin call won't fire on a
# sub-dollar gap. Mirrors `MIN_ACTION_USDC` philosophy.
#
# Bumped 2026-06-03 from $1 to $5 after `retCode=170140 Order value
# below lower limit` on USDCUSD1 with $1.14 notional. Bybit per-pair
# min-notional varies (USDCUSDT ~$1, USDCUSD1 ~$5, USDCFDUSD ~$5);
# $5 is a safe floor across the stables we trade. Worst case is a
# residual sub-$5 shortfall that has to be filled out of band — vs.
# the current behavior of a guaranteed live rejection.
MIN_SWAP_USDC = Decimal("5.00")


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
    # Spot-swap side. "Sell" (default) is the legacy USDC→stable flow
    # where we sell USDC (base) for a stable quote (USDCUSDT,
    # USDCUSD1). "Buy" is for non-stable Earn picks where we acquire
    # the target coin via {coin}USDT pair, paying USDT (quote). Field
    # is ignored for non-SWAP_SPOT kinds.
    side: str = "Sell"
    # Native-coin amount, populated only when `amount` (USD) and the
    # native-coin units differ. Non-stable SUBSCRIBE_EARN/_LM picks
    # set this to USD / mark_price so the dispatch can pass the right
    # units to Bybit's place_earn_order (which always expects native
    # coin amount, never USD).
    amount_native: Decimal | None = None
    # Per-action overrides for dispatch parameters that don't fit the
    # flat field set. Currently used by REDEEM_LM to carry
    # `remove_rate` (1-100) for partial exits; default behavior when
    # absent is the full-exit path (remove_rate=100).
    extra: dict[str, Any] = field(default_factory=dict)

    def to_log(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["amount"] = str(self.amount)
        if self.amount_native is not None:
            d["amount_native"] = str(self.amount_native)
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
    buy_usdt_demand = sum(
        (a.amount for a in earn_swaps if a.side == "Buy"),
        Decimal(0),
    )
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
    earn_swaps, dropped_coins = _enforce_usdc_budget(
        snapshot.wallet.liquid_usdc_usd, hedge_swaps, earn_swaps
    )

    # USDC-budget drops cascade to subscribes AND their paired perps.
    # When a stable's swap is dropped because USDC ran out, the
    # subscribe will 180016 at execute time and the perp would open
    # naked — convert both to SKIPs.
    #
    # NOTE: we DON'T extend this to "non-stable subscribe with no
    # swap path" — that's an architectural TODO (non-stable USD→native
    # conversion isn't wired). The auto-hedge tests rely on the perp
    # firing even when the spot fill is unresolved; until non-stable
    # swap wiring lands, the subscribe will hit 180016 live and the
    # operator manually closes the orphan perp. Tracked separately.
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
        buy_usdt_demand = sum(
            (a.amount for a in earn_swaps if a.side == "Buy"),
            Decimal(0),
        )
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
        buy_usdt_demand = sum(
            (a.amount for a in earn_swaps if a.side == "Buy"),
            Decimal(0),
        )
        hedge_swaps = _swap_actions_for_hedges(
            snapshot,
            hedge_opens,
            hedge_closes,
            snapshot_ts,
            idx_offset=len(all_pids) + len(hedge_closes) + len(hedge_opens),
            extra_usdt_demand=buy_usdt_demand,
        )

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
        ),
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
            + len(orphan_sells)
        ),
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

    return (
        redeems
        + carry_closes
        + hedge_closes
        + naked_closes
        + hedge_swaps
        + earn_swaps
        + orphan_sells
        + hedge_opens
        + carry_opens
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
# conditional products, not simple directional spot stakes. Same set as
# `_BASIC_EARN_CATEGORIES` above — both ultimately derive from
# `agent.reason.venues.HEDGE_VENUES`.
_AUTO_HEDGE_CATEGORIES = BASIC_EARN_CATEGORIES


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


def _alpha_action_for_target(
    snapshot: Snapshot,
    token_code: str,
    target: "_TargetPos | None",
    current_pos: "_CurrentPos | None",
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
        # firing a live API call. Operator flips VAULT_ALPHA_EXEC_ENABLED
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
                "skipped because VAULT_ALPHA_EXEC_ENABLED is off (`.54` "
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


# Position-payload amount fields across advance-Earn categories. Bybit's
# per-category schemas differ but every active row carries one of these.
# Order matters: we pick the first present field that parses to a
# positive Decimal so a row with both `amount` and `quoteAmount` doesn't
# get summed twice.
_ADVANCE_EARN_AMOUNT_FIELDS: tuple[str, ...] = (
    "amount",
    "stakeAmount",
    "quoteAmount",
    "purchaseAmount",
    "positionAmount",
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


_OFFER_PREFIX = " offer="


# ─── Funding-carry diff (`bybit-strategy-expansion.5`) ─────────────────────


# Carry-side constant: how tight the paired spot/perp legs must match in
# USD at dispatch time. The diff sizes both legs to the same base-qty
# so they're equal by construction; the check in `_execute_one` guards
# against a planning-time bug or qty-step rounding that produced an
# uneven pair (which would leave residual directional exposure).
_CARRY_PAIRED_NOTIONAL_TOLERANCE = Decimal("0.05")

# Maximum CLOSE_FUNDING_CARRY retry attempts before the diff layer
# stops emitting on a stuck position. Each failed CLOSE leaves spot
# unwound and perp open (or vice versa) — auto-retry every cycle is
# the right behavior for transient failures (margin lag, network
# blip), but a perp leg that fails the same way 3+ cycles in a row
# almost always needs operator attention (persistent margin shortfall,
# symbol delisted, account flag). Counter lives on the state record;
# reset to 0 on the next successful CLOSE (which deletes the record).
MAX_CARRY_CLOSE_ATTEMPTS = 3

# How long the OPEN_FUNDING_CARRY spot-fill poll waits (seconds) before
# orphaning the action. Market orders typically fill instantly; the 10s
# budget covers Bybit's worst-case settlement lag on a busy spot pair.
# If the fill isn't confirmed within this window the perp leg is NOT
# opened — orphan + surface for next-cycle reconciliation.
_CARRY_SPOT_FILL_POLL_SECONDS = 10.0
_CARRY_SPOT_FILL_POLL_INTERVAL = 0.5

# How long to wait between the first and second set_trading_stop attempt
# on OPEN_PERP_SHORT (fix #8). Transient SL failures (price-band drift,
# order-state race) typically clear in well under a second.
_PERP_SL_RETRY_BACKOFF = 1.0


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


def apply_carry_results_to_state(
    state: CarryState, results: list[ActionResult]
) -> CarryState:
    """Roll a fresh `CarryState` forward by walking dispatch results.

    Successful `OPEN_FUNDING_CARRY` (status="ok") → insert a position
    record sized from the planned action. Successful
    `CLOSE_FUNDING_CARRY` → drop the matching coin's record.
    OPEN orphans leave naked spot (no record to write — the orphan log
    captures it for operator reconciliation). CLOSE orphans KEEP the
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
            if a.amount_native is None or a.amount_native <= 0:
                continue
            try:
                mark = Decimal(str(a.extra.get("mark_price") or "0"))
            except (InvalidOperation, TypeError):
                mark = Decimal(0)
            next_state = next_state.upsert(
                CarryPositionRecord(
                    coin=a.coin.upper(),
                    opened_at=datetime.now(UTC),
                    target_pick_usd=a.amount,
                    spot_qty_base=a.amount_native,
                    perp_qty_base=a.amount_native,
                    mark_price_at_open=mark,
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


def _round_to_qty_step(
    raw_qty: Decimal,
    qty_step: Decimal | None,
    min_order_qty: Decimal | None,
) -> Decimal | None:
    """Round `raw_qty` DOWN to the nearest multiple of `qty_step`.
    Returns None when the rounded result is below `min_order_qty` (the
    caller surfaces a SKIP). When `qty_step` isn't known (snapshot
    couldn't fetch instruments_info for this symbol), falls back to a
    sane default of 0.001 — matches the previous hardcoded rounding."""
    step = qty_step or Decimal("0.001")
    if step <= 0:
        return None
    # Multiples of step: floor(raw / step) * step
    steps = (raw_qty / step).to_integral_value(rounding=ROUND_DOWN)
    qty = steps * step
    # Normalize precision to the step's scale so str(qty) doesn't carry
    # trailing zeros Bybit may reject.
    qty = qty.quantize(step)
    if min_order_qty is not None and qty < min_order_qty:
        return None
    return qty


def _earn_product_lookup(
    snapshot: Snapshot, category: str, product_id: str
) -> Any:
    """Find a ProductSummary in the snapshot's `products[<category>]`
    list by product_id. Used by the planner to read `min_subscribe_usd`
    before emitting a SUBSCRIBE_EARN — Bybit rejects sub-min subscribes
    with retCode=180012."""
    catalog = snapshot.products.get(category) if snapshot.products else None
    if not catalog:
        return None
    for item in catalog:
        if str(getattr(item, "product_id", "")) == str(product_id):
            return item
    return None


def _swap_base_coin(symbol: str) -> str:
    """Resolve the base coin of a spot symbol. We only swap by selling
    base (Market Sell), so for USDCUSDT base=USDC. Handles 3-5 char
    quote coins (USDT, USDC, USD1, FDUSD, USDE) since Bybit's USDC pair
    namespace covers stable→stable hops in either direction. Longest
    suffix match wins so USDCFDUSD parses to base=USDC, quote=FDUSD
    rather than base=USDCF, quote=DUSD."""
    quotes = ("FDUSD", "USDT", "USDC", "USD1", "USDE")
    candidates = sorted(quotes, key=len, reverse=True)
    for quote in candidates:
        if symbol.endswith(quote) and symbol != quote:
            return symbol[: -len(quote)]
    return symbol


async def _transfer_satisfies_swap(
    client: Any, target_coin: str | None, required: Decimal
) -> bool:
    """Pre-flight check before spot swap: if `target_coin` already sits
    in FUND in sufficient amount, transfer it to UNIFIED instead of
    paying a spot fee + slippage to manufacture it. Returns True when
    the transfer covered the requirement (caller skips the swap).

    Tolerates mocked clients in tests — any TypeError / non-Decimal
    return from `get_account_coin_balance` flips back to the swap
    path."""
    if not target_coin or required <= 0:
        return False
    try:
        fund_have_raw = await client.get_account_coin_balance(
            account_type="FUND", coin=target_coin
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "transfer_satisfies_swap: FUND probe for %s failed: %s",
            target_coin, e,
        )
        return False
    if not isinstance(fund_have_raw, Decimal):
        try:
            fund_have = Decimal(str(fund_have_raw))
        except (InvalidOperation, TypeError, ValueError):
            return False
    else:
        fund_have = fund_have_raw
    if fund_have < required:
        return False
    log.info(
        "transfer_satisfies_swap: %s FUND has %s ≥ required %s — "
        "skipping swap, moving FUND→UNIFIED",
        target_coin, fund_have, required,
    )
    try:
        await client.internal_transfer(
            coin=target_coin,
            amount=str(required),
            from_account_type="FUND",
            to_account_type="UNIFIED",
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning(
            "transfer_satisfies_swap: transfer for %s failed: %s — "
            "falling back to swap",
            target_coin, e,
        )
        return False


async def _ensure_unified_balance(
    client: Any, coin: str, required: Decimal
) -> None:
    """Make sure UNIFIED holds at least `required` of `coin` before a
    spot trade. Queries UNIFIED via `/v5/account/wallet-balance` (the
    only endpoint that returns UNIFIED), computes the gap, and pulls
    the shortfall from FUND via `internal_transfer`. FUND balance lives
    on a different endpoint (`/v5/asset/transfer/query-account-coin-
    balance`) since `wallet-balance` is UNIFIED-only. A small +0.5%
    headroom absorbs Bybit's per-coin precision and pending-balance
    lag without a second round-trip."""
    if required <= 0:
        return
    try:
        unified = await client.get_wallet_balance(coin=coin, account_type="UNIFIED")
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_unified_balance: UNIFIED probe failed for %s: %s", coin, e)
        return
    have = _coin_equity_from_wallet(unified, coin)
    if have >= required:
        return
    gap = required - have
    # +0.5% headroom
    gap_with_buffer = (gap * Decimal("1.005")).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )
    try:
        fund_have_raw = await client.get_account_coin_balance(account_type="FUND", coin=coin)
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_unified_balance: FUND probe failed for %s: %s", coin, e)
        return
    # Defensive: callers (tests) sometimes mock this method and the
    # mock return isn't a Decimal. Treat any non-Decimal as "no FUND
    # balance available" — the spot order will surface the real
    # shortfall on its own.
    if not isinstance(fund_have_raw, Decimal):
        try:
            fund_have = Decimal(str(fund_have_raw))
        except (InvalidOperation, TypeError, ValueError):
            return
    else:
        fund_have = fund_have_raw
    move = min(fund_have, gap_with_buffer)
    if move <= 0:
        log.info(
            "ensure_unified_balance: no FUND balance to move for %s "
            "(unified=%s, required=%s, fund=%s)",
            coin, have, required, fund_have,
        )
        return
    log.info(
        "ensure_unified_balance: moving %s %s FUND→UNIFIED "
        "(have=%s, required=%s, gap=%s)",
        move, coin, have, required, gap,
    )
    await client.internal_transfer(
        coin=coin,
        amount=str(move),
        from_account_type="FUND",
        to_account_type="UNIFIED",
    )
    # Bybit returns transfer success synchronously but the UNIFIED
    # balance lags ~0.5-2s before the spot endpoint sees it. Poll until
    # we see at least `required` or 5s elapse. Without this loop the
    # very next place_spot_order races and gets retCode=170131
    # "Insufficient balance" despite the transfer log showing success.
    import asyncio
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            check = await client.get_wallet_balance(coin=coin, account_type="UNIFIED")
            now_have = _coin_equity_from_wallet(check, coin)
            if now_have >= required:
                log.info(
                    "ensure_unified_balance: transfer settled — %s %s now in UNIFIED",
                    now_have, coin,
                )
                return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.3)
    log.warning(
        "ensure_unified_balance: transfer for %s did not settle within 5s; "
        "letting spot order surface the real shortfall",
        coin,
    )


async def _ensure_fund_balance(
    client: Any, coin: str, required: Decimal
) -> None:
    """Mirror of `_ensure_unified_balance` in the opposite direction:
    make sure FUND holds at least `required` of `coin` before an
    OnChain Earn subscribe (`accountType=FUND` per V5 spec). When the
    Buy spot leg deposits the coin into UNIFIED (Bybit's only spot
    delivery target on Unified Trading Account), the OnChain subscribe
    then 180016's because the same coin isn't in FUND.

    Live 2026-06-03: Buy TONUSDT delivered 7.53 TON to UNIFIED, OnChain
    TON subscribe expected FUND, the place_earn_order returned
    "Balance not enough" and a naked perp short was left in place.
    """
    if required <= 0:
        return
    try:
        fund_have_raw = await client.get_account_coin_balance(
            account_type="FUND", coin=coin
        )
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_fund_balance: FUND probe failed for %s: %s", coin, e)
        return
    if not isinstance(fund_have_raw, Decimal):
        try:
            fund_have = Decimal(str(fund_have_raw))
        except (InvalidOperation, TypeError, ValueError):
            return
    else:
        fund_have = fund_have_raw
    if fund_have >= required:
        return
    gap = required - fund_have
    gap_with_buffer = (gap * Decimal("1.005")).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )
    try:
        unified = await client.get_wallet_balance(coin=coin, account_type="UNIFIED")
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_fund_balance: UNIFIED probe failed for %s: %s", coin, e)
        return
    unified_have = _coin_equity_from_wallet(unified, coin)
    move = min(unified_have, gap_with_buffer)
    if move <= 0:
        log.info(
            "ensure_fund_balance: no UNIFIED balance to move for %s "
            "(fund=%s, required=%s, unified=%s)",
            coin, fund_have, required, unified_have,
        )
        return
    log.info(
        "ensure_fund_balance: moving %s %s UNIFIED→FUND "
        "(have=%s, required=%s, gap=%s)",
        move, coin, fund_have, required, gap,
    )
    await client.internal_transfer(
        coin=coin,
        amount=str(move),
        from_account_type="UNIFIED",
        to_account_type="FUND",
    )
    # Mirror the settle-poll from `_ensure_unified_balance` — Bybit's
    # internal-transfer is synchronous on the API but the destination
    # endpoint lags ~0.5-2s before reflecting the new balance.
    import asyncio as _asyncio
    deadline = _asyncio.get_event_loop().time() + 5.0
    while _asyncio.get_event_loop().time() < deadline:
        try:
            check = await client.get_account_coin_balance(
                account_type="FUND", coin=coin
            )
            now_have = check if isinstance(check, Decimal) else Decimal(str(check))
            if now_have >= required:
                log.info(
                    "ensure_fund_balance: transfer settled — %s %s now in FUND",
                    now_have, coin,
                )
                return
        except Exception:  # noqa: BLE001
            pass
        await _asyncio.sleep(0.3)
    log.warning(
        "ensure_fund_balance: transfer for %s did not settle within 5s; "
        "letting Earn place-order surface the real shortfall",
        coin,
    )


def _coin_equity_from_wallet(
    accounts: list[Any], coin: str
) -> Decimal:
    """Sum equity for `coin` across the WalletAccount list returned by
    `get_wallet_balance`. The shape varies slightly by account type;
    fall back to walking `coinDetail`/`coin` arrays when the model
    doesn't expose a flat coin attribute."""
    total = Decimal(0)
    coin_u = coin.upper()
    for acc in accounts:
        # Prefer the structured accessor when WalletAccount provides one.
        details = getattr(acc, "coinDetail", None) or getattr(acc, "coin", None) or []
        if isinstance(details, list):
            for entry in details:
                entry_coin = (getattr(entry, "coin", None) or
                              (entry.get("coin") if isinstance(entry, dict) else None))
                if not entry_coin or entry_coin.upper() != coin_u:
                    continue
                eq = (getattr(entry, "equity", None) or
                      getattr(entry, "walletBalance", None) or
                      (entry.get("equity") if isinstance(entry, dict) else None) or
                      (entry.get("walletBalance") if isinstance(entry, dict) else None))
                try:
                    total += Decimal(str(eq))
                except (InvalidOperation, TypeError, ValueError):
                    continue
    return total


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

    # Supply: existing USDT + hedge USDC→USDT swap inflow + close releases.
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
    close_release = Decimal(0)
    for a in hedge_closes:
        info = snapshot.perp_market.get(a.coin) or snapshot.perp_market.get(
            a.coin.upper()
        )
        if info is None or info.mark_price is None or info.mark_price <= 0:
            continue
        close_release += a.amount * info.mark_price

    supply = liquid_usdt + hedge_swap_inflow + close_release

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
            "(liquid $%s + hedge swap $%s + close release $%s) — "
            "dropping all %d non-stable Buy swap(s) for: %s",
            perp_demand, supply, liquid_usdt, hedge_swap_inflow,
            close_release, len(buy_swaps), ", ".join(sorted(dropped)),
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


def _coin_to_long_exposure(snapshot: Snapshot) -> dict[str, Decimal]:
    """Sum native LONG exposure per coin from currently-held Earn
    positions. UNIFIED wallet balance is NOT included here — caller
    adds it on top because it's the only thing actually sellable
    via SWAP_SPOT. Stables are skipped (irrelevant for hedge balance)."""
    out: dict[str, Decimal] = {}
    for p in snapshot.earn_positions or []:
        if hasattr(p, "model_dump"):
            data = p.model_dump(mode="python")
        else:
            data = p
        coin = (data.get("coin") or "").upper()
        if not coin or coin in _STABLES:
            continue
        try:
            amt = Decimal(str(data.get("amount", "0") or "0"))
        except (InvalidOperation, TypeError):
            continue
        if amt > 0:
            out[coin] = out.get(coin, Decimal(0)) + amt
    return out


def _coin_to_perp_short_size(snapshot: Snapshot) -> dict[str, Decimal]:
    """Sum native SHORT size per coin from open linear perp positions
    (side=Sell, size>0). Returns coin (uppercase) → total short qty.
    Used by orphan-sell and naked-perp detection to balance against
    the long side (UNIFIED + Earn)."""
    out: dict[str, Decimal] = {}
    for p in snapshot.perp_positions or []:
        symbol = getattr(p, "symbol", None) or (
            p.get("symbol") if isinstance(p, dict) else None
        )
        side = getattr(p, "side", None) or (
            p.get("side") if isinstance(p, dict) else None
        )
        size_raw = getattr(p, "size", None) or (
            p.get("size") if isinstance(p, dict) else None
        )
        if not symbol or side != "Sell" or not size_raw:
            continue
        try:
            size = Decimal(str(size_raw))
        except (InvalidOperation, TypeError):
            continue
        if size <= 0:
            continue
        coin = _coin_from_perp_symbol(symbol).upper()
        out[coin] = out.get(coin, Decimal(0)) + size
    return out


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
    """Defensive cleanup: sell to USDT the UNIFIED-wallet portion of a
    non-stable coin that exceeds what's needed to balance an open perp
    short on the same coin.

    Delta-neutral accounting (2026-06-03 fix after live naked-short bug):
        total_long  = unified_balance + earn_staked_native
        perp_short  = abs(open Sell perp size on {coin}USDT)
                      after this cycle's planned closes / opens
        excess_long = max(0, total_long - perp_short)
        sellable    = min(unified_balance, excess_long)

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
    balances = snapshot.wallet.unified_coin_balances or {}
    for coin, balance in balances.items():
        if not coin:
            continue
        coin_u = coin.upper()
        if coin_u in _STABLES or coin_u == "USDC":
            continue
        if balance <= 0:
            continue
        if coin_u in pending_subscribe_coins:
            continue
        perp_info = (snapshot.perp_market or {}).get(coin_u) or (
            snapshot.perp_market or {}
        ).get(coin)
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
        qty_step = getattr(perp_info, "qty_step", None) if perp_info else None
        min_qty = getattr(perp_info, "min_order_qty", None) if perp_info else None
        qty = _round_to_qty_step(sellable, qty_step, min_qty)
        if qty is None or qty <= 0:
            continue
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
                    f"(~${usd:.2f}): UNIFIED {balance} + Earn "
                    f"{earn_long.get(coin_u, 0)} - perp short {short} = "
                    f"excess {excess_long}"
                ),
            )
        )
        cursor += 1
    _ = redeems  # parameter kept for call-site symmetry / future use
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
    for a in redeems:
        if a.kind != ActionKind.REDEEM_EARN or not a.coin:
            continue
        sub = a.amount_native if a.amount_native is not None else Decimal(0)
        if sub > 0:
            long_now[a.coin.upper()] = max(
                Decimal(0), long_now.get(a.coin.upper(), Decimal(0)) - sub
            )

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
    for a in subscribe_actions:
        if a.kind not in (ActionKind.SUBSCRIBE_EARN, ActionKind.SUBSCRIBE_LM):
            continue
        coin = a.coin
        if coin in (None, "USDC"):
            continue
        if coin in _STABLES:
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

    qty = shortfall.quantize(Decimal("0.01"))
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

    Atomic-pair guard (2026-06-03): if a REDEEM_EARN errors out (most
    commonly retCode=180020 "Position not found" / "Processing"), the
    paired CLOSE_PERP on the same coin is converted to a SKIP. Without
    this, the perp closes successfully while the spot leg stays
    staked → naked LONG. Live hit: TON Earn 7.5 in Processing lock,
    redeem 180020'd, perp closed → $15 naked long until next cycle.

    Subscribe-side symmetry (2026-06-04): if SUBSCRIBE_EARN fails (most
    commonly retCode=180016 "Insufficient balance" or a product full),
    any paired OPEN_PERP_SHORT on the same coin is converted to SKIP —
    otherwise the short opens without the backing earn leg → naked
    SHORT. Different direction from the redeem case but same class of
    bug.
    """
    executions_dir.mkdir(parents=True, exist_ok=True)
    log_path = executions_dir / f"{snapshot_ts}.jsonl"
    results: list[ActionResult] = []
    redeem_failed_coins: set[str] = set()
    subscribe_failed_coins: set[str] = set()
    with log_path.open("a") as log_file:
        for action in actions:
            # Atomic-pair guard: skip the paired perp side when its
            # earn-side counterpart already failed earlier in the batch.
            # `CLOSE_PERP` follows a REDEEM only; `OPEN_PERP_SHORT`
            # follows a SUBSCRIBE only — but we test both sets against
            # `OPEN_PERP_SHORT` defensively (an LLM could in theory queue
            # an OPEN_PERP_SHORT alongside a REDEEM during a rebalance).
            coin_upper = (action.coin or "").upper()
            skip_reason: str | None = None
            if (
                action.kind in (
                    ActionKind.CLOSE_PERP, ActionKind.OPEN_PERP_SHORT
                )
                and coin_upper in redeem_failed_coins
            ):
                skip_reason = (
                    f"{action.kind.value} {action.coin}: paired "
                    f"REDEEM_EARN failed earlier in batch — "
                    f"skipping perp side to preserve hedge "
                    f"(avoids naked exposure)"
                )
            elif (
                action.kind == ActionKind.OPEN_PERP_SHORT
                and coin_upper in subscribe_failed_coins
            ):
                skip_reason = (
                    f"{action.kind.value} {action.coin}: paired "
                    f"SUBSCRIBE_EARN failed earlier in batch — "
                    f"skipping OPEN_PERP_SHORT to avoid naked short"
                )
            if skip_reason is not None:
                skip = ActionResult(
                    action=Action(
                        kind=ActionKind.SKIP_OUT_OF_SCOPE,
                        category=action.category,
                        product_id=action.product_id,
                        coin=action.coin,
                        amount=action.amount,
                        order_link_id=action.order_link_id,
                        reason=skip_reason,
                    ),
                    status="skipped",
                    response=None,
                    error=None,
                    started_at=datetime.now(UTC).isoformat(),
                    finished_at=datetime.now(UTC).isoformat(),
                )
                results.append(skip)
                log_file.write(json.dumps(skip.to_log()) + "\n")
                log_file.flush()
                log.warning(
                    "atomic-pair guard: skipping %s on %s — %s",
                    action.kind.value, action.coin, skip_reason,
                )
                continue

            res = await _execute_one(client, action, dry_run=dry_run)
            results.append(res)
            log_file.write(json.dumps(res.to_log()) + "\n")
            log_file.flush()

            if (
                action.kind == ActionKind.REDEEM_EARN
                and res.status == "error"
                and action.coin
            ):
                redeem_failed_coins.add(action.coin.upper())
                log.warning(
                    "redeem_earn failed for %s: %s — guarding any later "
                    "CLOSE_PERP / OPEN_PERP_SHORT on this coin",
                    action.coin, res.error,
                )
            elif (
                action.kind == ActionKind.SUBSCRIBE_EARN
                and res.status == "error"
                and action.coin
            ):
                subscribe_failed_coins.add(action.coin.upper())
                log.warning(
                    "subscribe_earn failed for %s: %s — guarding any "
                    "later OPEN_PERP_SHORT on this coin",
                    action.coin, res.error,
                )
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
            # Planner already rounded qty to the instrument's qty_step
            # via `_round_to_qty_step`, so `action.amount` is safe to
            # pass through as-is. Older actions written before that fix
            # may carry an unrounded qty; Bybit will reject those with
            # retCode=10001 and the BybitAPIError handler records it.
            out = await client.place_perp_order(
                symbol=action.product_id,
                side="Sell",
                qty=str(action.amount),
                order_link_id=action.order_link_id,
            )
            response = {"orderId": out.orderId}
            # 2026-06-03: Bybit-side stop-loss / take-profit on the
            # freshly-opened perp. Levels come from the planner's
            # `extra` (mirrored from the pick's `invalidate_at` block).
            # When Bybit triggers, the perp closes without waiting on
            # the agent's watcher poll. The matching Earn redeem is
            # handled separately by the auto-close path on the next
            # cycle (or by `check_perp_stopped_out` if we wire that).
            sl = action.extra.get("stop_loss") if action.extra else None
            tp = action.extra.get("take_profit") if action.extra else None
            if sl is not None or tp is not None:
                # Fix #8 (2026-06-04): one retry after a short backoff
                # before falling back to watcher-only protection. SL
                # failures are usually transient (price-band drift, race
                # with the order's `New` → `PartiallyFilled` transition);
                # a single retry catches most without spinning forever.
                # Don't cancel the freshly-opened perp on failure — that
                # would create a new exposure window and the watcher
                # poll still covers the position.
                _SL_MAX_ATTEMPTS = 2
                for attempt in range(_SL_MAX_ATTEMPTS):
                    try:
                        await client.set_trading_stop(
                            action.product_id,
                            stop_loss=sl,
                            take_profit=tp,
                        )
                        response["stop_loss"] = sl
                        response["take_profit"] = tp
                        if attempt > 0:
                            response["stop_loss_retry_succeeded"] = True
                        break
                    except BybitAPIError as e:
                        is_last = attempt == _SL_MAX_ATTEMPTS - 1
                        if is_last:
                            # Final attempt failed — log loudly, perp
                            # stays open under watcher protection only.
                            # `stop_loss_error` surfaces in the cycle log.
                            log.warning(
                                "set_trading_stop failed %d× for %s "
                                "(sl=%s tp=%s): retCode=%s %s — watcher "
                                "is sole safety net until next cycle",
                                _SL_MAX_ATTEMPTS, action.product_id, sl, tp,
                                e.ret_code, e.ret_msg,
                            )
                            response["stop_loss_error"] = (
                                f"retCode={e.ret_code} {e.ret_msg}"
                            )
                            response["stop_loss_retry_exhausted"] = True
                        else:
                            log.warning(
                                "set_trading_stop attempt %d failed for "
                                "%s (sl=%s tp=%s): retCode=%s %s — "
                                "retrying after backoff",
                                attempt + 1, action.product_id, sl, tp,
                                e.ret_code, e.ret_msg,
                            )
                            await asyncio.sleep(_PERP_SL_RETRY_BACKOFF)
        elif action.kind == ActionKind.OPEN_FUNDING_CARRY:
            # Compound dispatch (`bybit-strategy-expansion.5`): spot Buy
            # + perp Sell as ONE atomic intent. Sequence:
            #   1. set_leverage(1) on the perp symbol — idempotent
            #   2. spot Buy {coin}USDT, qty = USDT quote amount
            #   3. paired-notional check (tolerance ±5%): spot fill
            #      USD ≈ planned perp notional USD
            #   4. perp Sell, qty = base coin (= spot qty)
            # Atomic-pair guard: if step 2 fails, step 4 is skipped
            # (no naked short). If step 4 fails AFTER step 2 succeeded,
            # we have a naked spot long — surface as orphan + record
            # in response so the next cycle's close branch can wind it
            # down. Don't raise — the cycle log captures everything.
            spot_link = action.extra.get("spot_order_link_id") or (
                f"{action.order_link_id}_spot"
            )
            perp_link = action.extra.get("perp_order_link_id") or (
                f"{action.order_link_id}_perp"
            )
            response = {"legs": {}}
            await client.set_leverage(action.product_id, 1)
            # Spot Buy uses QUOTE amount (USDT) on V5 market orders —
            # action.amount is the USD-equivalent target. `.27` API now
            # takes `qty_quote=` explicitly so the asymmetry can't be
            # misremembered at the call site.
            spot_qty_quote = str(action.amount)
            spot_out = await client.place_spot_order(
                symbol=action.product_id,
                side="Buy",
                qty_quote=spot_qty_quote,
                order_link_id=spot_link,
            )
            response["legs"]["spot"] = {
                "orderId": spot_out.orderId,
                "side": "Buy",
                "qty_quote_usdt": spot_qty_quote,
            }
            # Resolve the ACTUAL spot fill — base qty and quote value
            # come from Bybit's exec record, not the planner's estimate.
            # Sizing the perp leg from `action.amount_native` (planned)
            # leaves a delta gap whenever the market fills the spot at
            # a different price than the snapshot's mark. Fix
            # 2026-06-04: short-poll the spot order until Filled, then
            # size perp from the real cumExecQty. If the fill can't be
            # confirmed within the window, do NOT open the perp —
            # orphan + surface so the next cycle reconciles. Without
            # this guard the executor would open a sized-from-plan
            # short on top of an indeterminate spot leg.
            actual_qty: Decimal | None = None
            actual_value: Decimal | None = None
            poll_error: str | None = None
            deadline = (
                asyncio.get_event_loop().time()
                + _CARRY_SPOT_FILL_POLL_SECONDS
            )
            while asyncio.get_event_loop().time() < deadline:
                try:
                    status = await client.get_spot_order_status(spot_out.orderId)
                except BybitOrderError as e:
                    poll_error = f"realtime lookup failed: {e}"
                    break
                if status.orderStatus == "Filled":
                    try:
                        actual_qty = Decimal(status.cumExecQty)
                        actual_value = Decimal(status.cumExecValue or "0")
                    except (InvalidOperation, TypeError) as e:
                        poll_error = f"bad fill numerics: {e}"
                    break
                if status.orderStatus in TERMINAL_BAD_SPOT_STATUSES:
                    poll_error = (
                        f"terminal {status.orderStatus} "
                        f"(reject={status.rejectReason})"
                    )
                    break
                await asyncio.sleep(_CARRY_SPOT_FILL_POLL_INTERVAL)
            else:
                poll_error = "fill not confirmed within poll window"

            if actual_qty is None or actual_qty <= 0:
                response["legs"]["spot"]["fill_check"] = poll_error or "unfilled"
                response["legs"]["perp"] = {
                    "skipped": (
                        f"spot fill not confirmed ({poll_error}); "
                        f"perp leg not opened — naked spot risk if order "
                        f"settles later, next-cycle CLOSE reconciles"
                    )
                }
                return ActionResult(
                    action=action,
                    status="orphan",
                    response=response,
                    error="spot fill verification failed",
                    started_at=started,
                    finished_at=datetime.now(UTC).isoformat(),
                )

            response["legs"]["spot"]["cumExecQty"] = str(actual_qty)
            if actual_value is not None and actual_value > 0:
                response["legs"]["spot"]["cumExecValue"] = str(actual_value)

            # Drift check now compares ACTUAL spot fill USD vs perp
            # notional sized from ACTUAL base qty — catches anomalous
            # slippage between mark_price (used to size the plan) and
            # the realized fill price. With both legs sized from the
            # same base qty the drift is purely the (mark vs fill)
            # spread.
            base_qty = actual_qty
            try:
                mark = Decimal(str(action.extra.get("mark_price") or "0"))
            except (InvalidOperation, TypeError):
                mark = Decimal(0)
            if mark > 0:
                perp_notional_usd = base_qty * mark
                spot_notional_usd = (
                    actual_value if actual_value is not None and actual_value > 0
                    else action.amount
                )
                drift = (
                    abs(perp_notional_usd - spot_notional_usd)
                    / spot_notional_usd
                    if spot_notional_usd > 0
                    else Decimal(0)
                )
                if drift > _CARRY_PAIRED_NOTIONAL_TOLERANCE:
                    # Orphan: spot already filled, perp uneven —
                    # don't open a mis-sized short. Record + surface.
                    response["legs"]["perp"] = {
                        "skipped": (
                            f"paired-notional drift {drift:.2%} > "
                            f"{_CARRY_PAIRED_NOTIONAL_TOLERANCE:.0%} tolerance "
                            f"(spot ${spot_notional_usd:.2f} vs "
                            f"perp ${perp_notional_usd:.2f}); "
                            f"naked spot long left — next-cycle CLOSE will reconcile"
                        )
                    }
                    return ActionResult(
                        action=action,
                        status="orphan",
                        response=response,
                        error="paired-notional check failed after spot fill",
                        started_at=started,
                        finished_at=datetime.now(UTC).isoformat(),
                    )
            try:
                perp_out = await client.place_perp_order(
                    symbol=action.product_id,
                    side="Sell",
                    qty=str(base_qty),
                    order_link_id=perp_link,
                )
                response["legs"]["perp"] = {
                    "orderId": perp_out.orderId,
                    "side": "Sell",
                    "qty_base": str(base_qty),
                }
            except BybitAPIError as e:
                # Spot already filled, perp leg failed → naked
                # spot long. Return orphan + error so the cycle
                # log carries the gap and the next cycle's diff
                # CLOSE branch can wind it down (state file won't
                # have a record since we never reached the success
                # path — operator manually injects a state row
                # OR uses the spot balance + missing-record path
                # via the hedge layer fallback).
                response["legs"]["perp"] = {
                    "error": f"retCode={e.ret_code} {e.ret_msg}",
                    "skipped": "naked spot long left after perp leg failure",
                }
                return ActionResult(
                    action=action,
                    status="orphan",
                    response=response,
                    error=(
                        f"perp leg failed after spot fill: "
                        f"retCode={e.ret_code} {e.ret_msg}"
                    ),
                    started_at=started,
                    finished_at=datetime.now(UTC).isoformat(),
                )
        elif action.kind == ActionKind.CLOSE_FUNDING_CARRY:
            # Mirror of OPEN: spot Sell + perp Buy(reduceOnly). Atomic-
            # pair guard same shape — spot fail means perp skipped, no
            # naked short. perp fail after spot succeeded leaves naked
            # spot USDT (loose USDT principal back in the wallet on
            # spot Sell — far less risky than a naked short, so we just
            # surface as orphan and let the operator reconcile).
            spot_link = action.extra.get("spot_order_link_id") or (
                f"{action.order_link_id}_spot"
            )
            perp_link = action.extra.get("perp_order_link_id") or (
                f"{action.order_link_id}_perp"
            )
            base_qty = action.amount_native
            response = {"legs": {}}
            if base_qty is None or base_qty <= 0:
                # Defensive: state-derived qty must be present. Without
                # it we can't close cleanly — skip both legs, surface
                # for operator.
                return ActionResult(
                    action=action,
                    status="error",
                    response=None,
                    error="amount_native missing on CLOSE_FUNDING_CARRY",
                    started_at=started,
                    finished_at=datetime.now(UTC).isoformat(),
                )
            spot_out = await client.place_spot_order(
                symbol=action.product_id,
                side="Sell",
                qty_base=str(base_qty),
                order_link_id=spot_link,
            )
            response["legs"]["spot"] = {
                "orderId": spot_out.orderId,
                "side": "Sell",
                "qty_base": str(base_qty),
            }
            try:
                perp_out = await client.place_perp_order(
                    symbol=action.product_id,
                    side="Buy",
                    qty=str(base_qty),
                    reduce_only=True,
                    order_link_id=perp_link,
                )
                response["legs"]["perp"] = {
                    "orderId": perp_out.orderId,
                    "side": "Buy",
                    "qty_base": str(base_qty),
                    "reduce_only": True,
                }
            except BybitAPIError as e:
                # Spot Sell already filled (we have USDT back); perp
                # short still open. Less catastrophic than the OPEN
                # orphan case (no naked direction beyond the unwound
                # short), but state needs reconciliation: the carry
                # record persists (next cycle will retry CLOSE) and
                # the orphan perp short surfaces in the next snapshot's
                # `perp_positions` for the hedge layer (which excludes
                # carry coins, so it won't auto-close it).
                response["legs"]["perp"] = {
                    "error": f"retCode={e.ret_code} {e.ret_msg}",
                    "skipped": "naked perp short left after spot sell",
                }
                return ActionResult(
                    action=action,
                    status="orphan",
                    response=response,
                    error=(
                        f"perp leg failed after spot fill: "
                        f"retCode={e.ret_code} {e.ret_msg}"
                    ),
                    started_at=started,
                    finished_at=datetime.now(UTC).isoformat(),
                )
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
        elif action.kind == ActionKind.ALPHA_PURCHASE:
            # `.54` — fetch a fresh quote and execute the buy. We don't
            # carry quote data from the diff (it would be stale by the
            # time we get here; Bybit's `expireTime` is ~5min). USD
            # `amount` from the diff becomes `fromTokenAmount` in
            # USDT base units (USDT ≈ $1, 6 decimals).
            quote = await client.get_alpha_quote(
                trade_type=1,
                from_token_code=_ALPHA_PAY_TOKEN_CODE,
                from_token_amount=str(action.amount),
                to_token_code=action.product_id,
            )
            quote_data = quote.get("quoteData")
            correcting = quote.get("correctingCode")
            gas = quote.get("gas")
            if not quote_data or not correcting or gas is None:
                raise BybitAPIError(
                    0,
                    "alpha quote missing quoteData / correctingCode / gas",
                    "/v5/alpha/trade/quote",
                )
            raw = await client.alpha_purchase(
                from_token_code=_ALPHA_PAY_TOKEN_CODE,
                from_token_amount=str(action.amount),
                to_token_code=action.product_id,
                slippage=_ALPHA_DEFAULT_SLIPPAGE,
                quote_data=quote_data,
                gas=str(gas),
                correcting_code=correcting,
            )
            response = {
                "orderNo": raw.get("orderNo"),
                "quoteDataId": quote.get("quoteDataId"),
                "expectedToTokenAmount": quote.get("toTokenAmount"),
                "slippage": _ALPHA_DEFAULT_SLIPPAGE,
            }
        elif action.kind == ActionKind.ALPHA_REDEEM:
            # `.54` — fetch a fresh quote and execute the sell. Unlike
            # purchase, `fromTokenAmount` is in the alpha token's native
            # base units (carried through `action.extra
            # ["token_amount_native"]` from the diff layer's
            # `snapshot.alpha_positions` lookup). `action.amount` here
            # is USD-equivalent for log readability only.
            native = action.extra.get("token_amount_native")
            if not native:
                raise RuntimeError(
                    f"ALPHA_REDEEM action {action.order_link_id} missing "
                    "extra.token_amount_native — diff layer must populate"
                )
            quote = await client.get_alpha_quote(
                trade_type=2,
                from_token_code=action.product_id,
                from_token_amount=str(native),
                to_token_code=_ALPHA_PAY_TOKEN_CODE,
            )
            quote_data = quote.get("quoteData")
            correcting = quote.get("correctingCode")
            gas = quote.get("gas")
            if not quote_data or not correcting or gas is None:
                raise BybitAPIError(
                    0,
                    "alpha quote missing quoteData / correctingCode / gas",
                    "/v5/alpha/trade/quote",
                )
            raw = await client.alpha_redeem(
                from_token_code=action.product_id,
                from_token_amount=str(native),
                to_token_code=_ALPHA_PAY_TOKEN_CODE,
                slippage=_ALPHA_DEFAULT_SLIPPAGE,
                quote_data=quote_data,
                gas=str(gas),
                correcting_code=correcting,
            )
            response = {
                "orderNo": raw.get("orderNo"),
                "quoteDataId": quote.get("quoteDataId"),
                "expectedToTokenAmount": quote.get("toTokenAmount"),
                "slippage": _ALPHA_DEFAULT_SLIPPAGE,
            }
        elif action.kind == ActionKind.SWAP_SPOT:
            # Two routes:
            #   side="Sell" (default): USDCx pair, sell USDC for quote
            #                          stable. `amount` is USDC qty.
            #   side="Buy":             {coin}USDT pair, buy non-stable
            #                          with USDT. `amount` is USDT qty.
            # Bybit's spot Market Sell uses base-coin qty; Market Buy
            # uses quote-coin qty (the `.27` asymmetry).
            side = action.side or "Sell"
            if side == "Sell":
                # Pre-flight: if target coin already sits in FUND in
                # sufficient quantity, transfer it instead of paying
                # spot fees to recreate balance we already have.
                target_coin = action.coin
                if await _transfer_satisfies_swap(client, target_coin, action.amount):
                    response = {
                        "transferred_in_lieu_of_swap": True,
                        "coin": target_coin,
                    }
                else:
                    # Source coin (USDC) often lives in FUND; pre-flight
                    # FUND→UNIFIED transfer and poll until settled.
                    base_coin = _swap_base_coin(action.product_id)
                    await _ensure_unified_balance(client, base_coin, action.amount)
                    out = await client.place_spot_order(
                        symbol=action.product_id,
                        side="Sell",
                        qty_base=str(action.amount),
                        order_link_id=action.order_link_id,
                    )
                    response = {"orderId": out.orderId}
            else:
                # Buy: spend `amount` USDT to acquire `action.coin`.
                # Ensure UTA has enough USDT first (FUND→UNIFIED if
                # needed).
                await _ensure_unified_balance(client, "USDT", action.amount)
                out = await client.place_spot_order(
                    symbol=action.product_id,
                    side="Buy",
                    qty_quote=str(action.amount),
                    order_link_id=action.order_link_id,
                )
                response = {"orderId": out.orderId, "side": "Buy"}
        else:
            side = "Stake" if action.kind == ActionKind.SUBSCRIBE_EARN else "Redeem"
            account_type = _ACCOUNT_TYPE[action.category]
            # Bybit Earn endpoints expect native-coin amount, never USD.
            # For stables `amount` (USD) ≈ native; for non-stables the
            # planner pre-computed `amount_native` via mark price.
            send_amount = (
                action.amount_native
                if action.amount_native is not None
                else action.amount
            )
            # For OnChain Stake (FUND wallet, per V5 spec) the coin must
            # already be in FUND. Non-stable Buy swaps deliver to UNIFIED,
            # so we transfer UNIFIED→FUND first. Mirror of the existing
            # FUND→UNIFIED auto-transfer the Buy-spot path already runs.
            # Live 2026-06-03: TON OnChain subscribe 180016 after Buy
            # deposited TON into UNIFIED — left a naked perp short.
            if (
                action.kind == ActionKind.SUBSCRIBE_EARN
                and account_type == "FUND"
                and action.coin
            ):
                try:
                    await _ensure_fund_balance(
                        client, action.coin, Decimal(str(send_amount))
                    )
                except (InvalidOperation, TypeError):
                    pass
            earn_out = await client.place_earn_order(
                category=action.category,  # type: ignore[arg-type]
                product_id=action.product_id,
                amount=str(send_amount),
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


def reconcile_executions(
    snapshot_ts: str,
    executions_dir: Path = EXECUTIONS_DIR,
) -> dict[str, Any]:
    """Read-only summary of one cycle's executions log (`.42`).

    Used at startup when a prior cycle's `executions/<ts>.jsonl` exists
    but no matching cycle_log entry does — typically systemd-OOM /
    SIGKILL between `execute_actions` writing the per-action line and
    `run_one_cycle` writing the cycle outcome. The function does NOT
    mutate state or replay actions; the caller (loop startup) decides
    whether to surface a warning or block restart.

    Returns
    -------
    dict with:
      - `snapshot_ts`: echoes input
      - `path`: absolute path to the executions file (str)
      - `exists`: whether the file is on disk
      - `total`: total per-action lines parsed
      - `counts`: {status → count} histogram across `"ok"`, `"error"`,
        `"orphan"`, `"skipped"`, `"dry-run"`. Unknown statuses are
        bucketed as-is so a future status addition surfaces visibly.
      - `errors`: list of `{kind, product_id, error}` for non-ok rows
        (truncated to first 10 — operator usually only needs the
        head for triage)
      - `last_started_at` / `last_finished_at`: ISO strings for the
        tail-end action, useful for "how far did the cycle get"

    Returns the same shape with `exists=False, total=0` when the file
    doesn't exist (cleanly absent — caller treats as a no-op).
    """
    log_path = executions_dir / f"{snapshot_ts}.jsonl"
    result: dict[str, Any] = {
        "snapshot_ts": snapshot_ts,
        "path": str(log_path),
        "exists": log_path.is_file(),
        "total": 0,
        "counts": {},
        "errors": [],
        "last_started_at": None,
        "last_finished_at": None,
    }
    if not result["exists"]:
        return result

    counts: dict[str, int] = {}
    errors: list[dict[str, Any]] = []
    last_started: str | None = None
    last_finished: str | None = None
    total = 0
    for raw in log_path.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            # Corrupt trailing line is common at the OS-kill boundary —
            # count it as malformed and continue.
            counts["malformed"] = counts.get("malformed", 0) + 1
            continue
        total += 1
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        if status not in ("ok", "dry-run", "skipped") and len(errors) < 10:
            action_block = row.get("action") or {}
            errors.append({
                "kind": action_block.get("kind"),
                "product_id": action_block.get("product_id"),
                "error": row.get("error"),
            })
        if row.get("started_at"):
            last_started = row["started_at"]
        if row.get("finished_at"):
            last_finished = row["finished_at"]
    result["total"] = total
    result["counts"] = counts
    result["errors"] = errors
    result["last_started_at"] = last_started
    result["last_finished_at"] = last_finished
    return result


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
    if action.kind == ActionKind.OPEN_FUNDING_CARRY:
        return {
            "would_call": "open_funding_carry",
            "symbol": action.product_id,
            "coin": action.coin,
            "spot_qty_quote_usdt": str(action.amount),
            "perp_qty_base": str(action.amount_native) if action.amount_native else None,
            "mark_price": action.extra.get("mark_price"),
            "spot_order_link_id": action.extra.get(
                "spot_order_link_id"
            ) or f"{action.order_link_id}_spot",
            "perp_order_link_id": action.extra.get(
                "perp_order_link_id"
            ) or f"{action.order_link_id}_perp",
        }
    if action.kind == ActionKind.CLOSE_FUNDING_CARRY:
        return {
            "would_call": "close_funding_carry",
            "symbol": action.product_id,
            "coin": action.coin,
            "qty_base": str(action.amount_native) if action.amount_native else None,
            "spot_order_link_id": action.extra.get(
                "spot_order_link_id"
            ) or f"{action.order_link_id}_spot",
            "perp_order_link_id": action.extra.get(
                "perp_order_link_id"
            ) or f"{action.order_link_id}_perp",
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
    if action.kind == ActionKind.ALPHA_PURCHASE:
        return {
            "would_call": "alpha_purchase",
            "trade_type": 1,
            "from_token_code": _ALPHA_PAY_TOKEN_CODE,
            "from_token_amount_usd": str(action.amount),
            "to_token_code": action.product_id,
            "to_token_symbol": action.coin,
            "slippage": _ALPHA_DEFAULT_SLIPPAGE,
            "note": "quote fetched at execute time, not dry-run",
        }
    if action.kind == ActionKind.ALPHA_REDEEM:
        return {
            "would_call": "alpha_redeem",
            "trade_type": 2,
            "from_token_code": action.product_id,
            "from_token_symbol": action.coin,
            "from_token_amount_native": action.extra.get("token_amount_native"),
            "to_token_code": _ALPHA_PAY_TOKEN_CODE,
            "approx_usd": str(action.amount),
            "slippage": _ALPHA_DEFAULT_SLIPPAGE,
            "note": "quote fetched at execute time, not dry-run",
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
    # Native-coin balance (e.g. 4.9005 LIT). Distinct from `amount_usd`
    # because non-stable positions whose perp mark goes missing (Bybit
    # delisted, snapshot's perp_market fan-out budget exhausted, etc.)
    # silently collapse to amount_usd=0 — the diff layer needs the
    # native value to still emit a REDEEM and avoid naked spot exposure.
    amount_native: Decimal = Decimal(0)


def _alpha_current_positions(
    alpha_positions: list[dict[str, Any]],
) -> dict[tuple[str, str], "_CurrentPos"]:
    """Index Bybit Alpha holdings by `(AlphaFarm, tokenCode)` with USD
    sizing taken from `tokenAmountUsd` (Bybit's own valuation against
    `lastPrice`). Zero-amount rows are skipped so we don't spuriously
    emit redeems for stale entries.
    """
    out: dict[tuple[str, str], _CurrentPos] = {}
    for pos in alpha_positions:
        token_code = str(pos.get("tokenCode") or "")
        if not token_code:
            continue
        try:
            amt_usd = Decimal(str(pos.get("tokenAmountUsd") or "0"))
        except (InvalidOperation, TypeError):
            amt_usd = Decimal(0)
        if amt_usd <= 0:
            continue
        symbol = str(pos.get("tokenSymbol") or token_code)
        out[(_ALPHA_CATEGORY, token_code)] = _CurrentPos(
            coin=symbol, amount_usd=amt_usd
        )
    return out


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

    Bybit returns one row per subscribe transaction while it settles —
    a freshly-subscribed OnChain position can appear as TWO entries
    (the old settled balance + a new `Processing` chunk) for the same
    `(category, productId)`. SUM them rather than overwrite so the
    diff layer sees the actual total long exposure. Without this,
    every cycle would underestimate `current` and the LLM's `target -
    current` delta would re-trigger more subscribes, creating an
    endless growth pattern of Processing entries (live hit 2026-06-03:
    TON OnChain reached 3 Processing entries totalling 10+ native).

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
        existing = out.get((category, pid))
        if existing is not None:
            # Sum with prior entry (multiple Bybit rows for the same
            # subscription state — e.g. settled + Processing chunks).
            out[(category, pid)] = _CurrentPos(
                coin=existing.coin,
                amount_usd=existing.amount_usd + amount_usd,
                amount_native=existing.amount_native + amt,
            )
        else:
            out[(category, pid)] = _CurrentPos(
                coin=coin, amount_usd=amount_usd, amount_native=amt
            )
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
        ActionKind.ALPHA_REDEEM,
        ActionKind.CLOSE_PERP,
        ActionKind.SWAP_SPOT,
        ActionKind.OPEN_PERP_SHORT,
        ActionKind.SUBSCRIBE_EARN,
        ActionKind.SUBSCRIBE_ADVANCE_EARN,
        ActionKind.SUBSCRIBE_LM,
        ActionKind.ALPHA_PURCHASE,
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
