"""Auto-extracted submodule (ah.25 execute split). See package __init__."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agent.bybit_oracle.bybit_client import (
    TERMINAL_BAD_SPOT_STATUSES,
    BybitAPIError,
    BybitClient,
    BybitOrderError,
)
from agent.sandbox.execute.builders import (
    _build_advance_extra,
    _decode_offer_from_reason,
    _pick_offer_for_execute,
)
from agent.sandbox.execute.common import (
    _ACCOUNT_TYPE,
    _ALPHA_DEFAULT_SLIPPAGE,
    _ALPHA_PAY_TOKEN_CODE,
    _CARRY_OPEN_USDT_FACTOR,
    _CARRY_PAIRED_NOTIONAL_TOLERANCE,
    _CARRY_SPOT_FILL_POLL_INTERVAL,
    _CARRY_SPOT_FILL_POLL_SECONDS,
    _FUNDING_SWAP_FEE_FACTOR,
    _LM_QUOTE_ACCOUNT_TYPE,
    _ORDER_HISTORY_CATEGORY,
    _PERP_SL_RETRY_BACKOFF,
    _STABLE_SWAP_HEADROOM,
    _STABLES,
    EXECUTIONS_DIR,
    MIN_SWAP_USDC,
    REDEEM_SETTLE_TIMEOUT_SECONDS,
    _coin_equity_from_wallet,
    _swap_base_coin,
    _transfer_quantum,
)
from agent.sandbox.execute.types import (
    Action,
    ActionKind,
    ActionResult,
)

log = logging.getLogger(__name__)




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
    # Move whole transfer-quanta. `fund_have` is floored to the quantum: it
    # carries full wallet precision (8+dp) and could win the min(), so the
    # move would otherwise 131210 for USDT. The GAP, however, is rounded UP
    # to a quantum — Bybit only moves whole quanta, so a sub-quantum gap
    # floored to 0 transfers nothing and the spot order is left short by
    # <1 quantum → 170131 (prod USD1 2026-06-09: gap 0.009 < 0.01 stable
    # quantum → moved 0 → sell 7.77 vs 7.76 in UNIFIED, every cycle). Over-
    # moving by <1 quantum just lands a hair extra in UNIFIED (the spot
    # order sells `required`, the rest stays) — harmless, and the min() cap
    # never moves more than FUND actually holds. See `_transfer_quantum`.
    quantum = _transfer_quantum(coin)
    move = min(
        fund_have.quantize(quantum, rounding=ROUND_DOWN),
        gap_with_buffer.quantize(quantum, rounding=ROUND_UP),
    )
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


async def _fund_carry_open_usdt(
    client: Any, pick_usd: Decimal, order_link_id: str
) -> dict[str, Any] | None:
    """Ensure UNIFIED holds enough USDT to open a `pick_usd`-sized funding
    carry — spot Buy quote (1×) + perp short margin (HEDGE_MARGIN_BUFFER×) —
    creating it from USDC via a `USDCUSDT` Sell when short (dispatch-1 / ah.6).

    A carry's legs are both USDT-denominated but the vault holds USDC, so on a
    USDC-only book the spot Buy 170131s with no USDT. Mirrors the hedge funding
    path (`_swap_actions_for_hedges` → SWAP_SPOT → `_ensure_unified_balance`)
    but inline, since carry is a compound single action with no planned swap.

    Returns a swap receipt dict, or None when existing USDT already covers the
    need (no swap emitted). Raises only if an underlying client call raises —
    caught by `_execute_one`'s outer guard, leaving NO open leg (runs first).
    """
    required = (pick_usd * _CARRY_OPEN_USDT_FACTOR).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )
    if required <= 0:
        return None
    # Pull any USDT already sitting in FUND into UNIFIED first — it may cover
    # the need without a fee-bearing swap.
    await _ensure_unified_balance(client, "USDT", required)
    try:
        bal = await client.get_wallet_balance(coin="USDT", account_type="UNIFIED")
        have = _coin_equity_from_wallet(bal, "USDT")
    except Exception as e:  # noqa: BLE001
        log.warning("carry funding: UNIFIED USDT probe failed: %s", e)
        have = Decimal(0)
    shortfall = required - have
    if shortfall < MIN_SWAP_USDC:
        return None
    # Over-convert by the taker fee + 0.5% headroom (qty rounds DOWN) so the
    # netted USDT clears both legs — a bare-shortfall swap under-delivers.
    usdc_to_sell = (
        shortfall / _FUNDING_SWAP_FEE_FACTOR * _STABLE_SWAP_HEADROOM
    ).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    await _ensure_unified_balance(client, "USDC", usdc_to_sell)
    swap_out = await client.place_spot_order(
        symbol="USDCUSDT",
        side="Sell",
        qty_base=str(usdc_to_sell),
        order_link_id=f"{order_link_id}_carryfund",
    )
    # Re-pull so the freshly-swapped USDT is in UNIFIED for the spot Buy +
    # perp margin (the Sell credits UNIFIED; this also drains any FUND dust).
    await _ensure_unified_balance(client, "USDT", required)
    return {
        "swap": "USDCUSDT Sell",
        "usdc_sold": str(usdc_to_sell),
        "required_usdt": str(required),
        "orderId": getattr(swap_out, "orderId", None),
    }


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
    # Size the move from the UNIFIED TRANSFERABLE balance, not equity.
    # `get_wallet_balance` reports walletBalance/equity, but the UTA reserves
    # a haircut so the inter-transfer endpoint only moves `transferBalance`.
    # Sizing from equity moved more than allowed and reverted 131212 at
    # execute (prod 2026-06-08: UNIFIED USDT equity 18.78 but only ~transfer
    # movable → UNIFIED→FUND of $12.47 failed, OnChain USDT subscribe stranded).
    try:
        unified_have_raw = await client.get_account_coin_balance(
            account_type="UNIFIED", coin=coin
        )
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_fund_balance: UNIFIED probe failed for %s: %s", coin, e)
        return
    if isinstance(unified_have_raw, Decimal):
        unified_have = unified_have_raw
    else:
        try:
            unified_have = Decimal(str(unified_have_raw))
        except (InvalidOperation, TypeError, ValueError):
            return
    # Bybit's internal-transfer rejects amounts whose decimal scale exceeds
    # the coin's transfer accuracy (retCode 131210 "transfer amount scale
    # more than accuracy length"). `gap_with_buffer` is 6dp and
    # `unified_have` carries the wallet's full precision (8+dp), so
    # re-quantize the final move down to the coin's accuracy. ROUND_DOWN so
    # we never move more than is actually available. USDT needs a coarser
    # scale than 6dp (`bybit-sandbox.68`) — see `_transfer_quantum`.
    move = min(unified_have, gap_with_buffer).quantize(
        _transfer_quantum(coin), rounding=ROUND_DOWN
    )
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

    Redeem settlement barrier (2026-06-07): after a successful
    REDEEM_EARN we poll the wallet until the freed coin is actually
    credited (`poll_redemption_credited`) before continuing to the spot-
    sell / subscribe / perp-margin actions that consume it. The action
    list is ordered redeems-first precisely so this freed capital funds
    the subscribes; without the wait a rebalance that needs the freed
    USD silently no-ops on retCode=180016. Polled inline (not as a
    deferred end-of-redeems barrier) because the poll captures its
    balance baseline at call entry — issuing all redeems first would let
    the credit land before the baseline and the poll would never see the
    delta.

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

            # Redeem settlement barrier (see function docstring): block
            # until the redeemed coin is credited so the downstream
            # spend (sell / subscribe / margin) sees real liquidity.
            # Timeout → warn and proceed; the dependent SUBSCRIBE will
            # 180016 and the atomic-pair guard handles its perp leg.
            if (
                action.kind == ActionKind.REDEEM_EARN
                and res.status == "ok"
                and not dry_run
                and action.coin
                and action.amount
            ):
                try:
                    await client.poll_redemption_credited(
                        coin=action.coin,
                        min_credit=action.amount,
                        timeout_seconds=REDEEM_SETTLE_TIMEOUT_SECONDS,
                    )
                except TimeoutError as e:
                    log.warning(
                        "redeem of %s %s not credited before downstream "
                        "spend (%s) — proceeding; a dependent SUBSCRIBE "
                        "may hit 180016 and the atomic-pair guard will "
                        "skip its perp leg",
                        action.amount, action.coin, e,
                    )
    return results


async def _unwind_carry_spot(
    client: BybitClient, symbol: str, base_qty: Decimal, order_link_id: str
) -> dict[str, Any]:
    """Atomically flatten a half-open funding carry: sell `base_qty` of the
    just-bought spot back when the perp leg never landed (ah.9 / state-3). A
    spot Market Sell takes base-coin qty. Returns `{"unwound": True, ...}` on
    success (zero naked directional exposure left) or `{"unwound": False,
    "error": ...}` — on failure the genuinely-naked spot is surfaced for the
    operator and swept by `_orphan_spot_sell_actions` next cycle."""
    try:
        out = await client.place_spot_order(
            symbol=symbol,
            side="Sell",
            qty_base=str(base_qty),
            order_link_id=f"{order_link_id}_unwind",
        )
        return {
            "unwound": True,
            "orderId": out.orderId,
            "qty_base": str(base_qty),
        }
    except BybitAPIError as e:
        return {"unwound": False, "error": f"retCode={e.ret_code} {e.ret_msg}"}


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
            # ah.6 (dispatch-1): both legs are USDT-denominated but the vault
            # holds USDC — provision UNIFIED USDT (swap USDC→USDT when short)
            # BEFORE either leg fires, else the spot Buy 170131s on a USDC-only
            # book. Runs first, so a funding failure leaves no open leg.
            carry_fund = await _fund_carry_open_usdt(
                client, action.amount, action.order_link_id
            )
            if carry_fund is not None:
                response["legs"]["funding"] = carry_fund
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
                            f"perp ${perp_notional_usd:.2f}) — unwinding spot"
                        )
                    }
                    # ah.9: flatten the half-open atomically (sell the spot
                    # back) instead of leaving a naked long for next cycle.
                    response["legs"]["unwind"] = await _unwind_carry_spot(
                        client, action.product_id, base_qty, action.order_link_id
                    )
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
                    "skipped": "perp leg failed after spot fill — unwinding spot",
                }
                # ah.9: flatten the half-open atomically (sell the spot back)
                # so the perp-fail leaves zero naked directional exposure.
                response["legs"]["unwind"] = await _unwind_carry_spot(
                    client, action.product_id, base_qty, action.order_link_id
                )
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
                # spot fees to recreate balance we already have. Skipped
                # for disposal sells (`skip_fund_transfer`, stable
                # consolidation) where the goal is to SELL the base coin,
                # not acquire the destination — there the optimization
                # would no-op the sell and strand the balance.
                target_coin = action.coin
                if not action.extra.get(
                    "skip_fund_transfer"
                ) and await _transfer_satisfies_swap(
                    client, target_coin, action.amount
                ):
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
            # Robust redeem sizing (safety net over the diff layer): a
            # non-stable Earn redeem with no planner-computed native qty
            # would send the USD `amount` as the coin qty → Bybit can't
            # find that many coins → retCode 180020 "Position not found"
            # (live 2026-06-06/07, TON OnChain). Pull the real held native
            # qty from Bybit so a redeem is ALWAYS in coin units; refuse
            # rather than send USD-as-native.
            if (
                action.kind == ActionKind.REDEEM_EARN
                and action.amount_native is None
                and action.coin
                and action.coin.upper() not in _STABLES
            ):
                held = await _live_earn_native_qty(
                    client, action.category, action.product_id
                )
                if held is not None:
                    send_amount = held
                else:
                    return ActionResult(
                        action=action,
                        status="error",
                        response=None,
                        error=(
                            f"redeem {action.coin} {action.category}/"
                            f"{action.product_id}: no native qty from planner "
                            "and live position lookup empty — refusing to send "
                            "USD as native (would 180020)"
                        ),
                        started_at=started,
                        finished_at=datetime.now(UTC).isoformat(),
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
            if (
                action.kind == ActionKind.REDEEM_EARN
                and action.category == "OnChain"
            ):
                # OnChain non-LST redeem must target each per-stake
                # position by its redeemPositionId, else 180020.
                response = await _redeem_onchain_by_position(
                    client, action, Decimal(str(send_amount))
                )
            else:
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


async def verify_executions_against_bybit(
    snapshot_ts: str,
    client: BybitClient,
    executions_dir: Path = EXECUTIONS_DIR,
) -> dict[str, Any]:
    """Cross-check a cycle's executions log against live Bybit order-history
    by `orderLinkId` (`bybit-sandbox.59`).

    Strictly READ-ONLY: it answers "did each confirmable action's order
    actually land on Bybit?" and classifies the answer — it does NOT retry
    anything. Auto-retry stays deliberately unbuilt because a blind replay
    inside Bybit's ~30-min `orderLinkId` dedup window can double-spend when a
    response landed just before the crash; this verifier is the prerequisite
    that removes that ambiguity for the kinds Bybit lets us confirm.

    Only `SWAP_SPOT` / `OPEN_PERP_SHORT` / `CLOSE_PERP` are confirmable —
    they hit `/v5/order/create` and show up in `/v5/order/history`. Earn /
    LM / advance-Earn / Alpha have no order-history endpoint and land in the
    `unconfirmable` bucket.

    Per-action classification:
      - `confirmed-landed`: an order with this `orderLinkId` exists on Bybit
        (regardless of what the log row says — a logged `error` whose order
        nonetheless landed is exactly the double-spend trap a naive retry
        would trigger).
      - `no-trace`: no Bybit order, and the log row is `error`/missing — a
        genuine retry candidate (surfaced, NOT retried).
      - `desync`: no Bybit order, but the log row claims `ok` — an anomaly
        worth an operator's eyes.
      - `query-error`: the history lookup itself failed (transient API
        error); recorded so one bad lookup can't abort the scan.

    Returns
    -------
    dict with `snapshot_ts`, `exists`, `checked` (confirmable rows queried),
    `unconfirmable` (rows skipped), `counts` ({classification → count}), and
    `actions` (per-row detail: order_link_id, kind, product_id, log_status,
    bybit_landed, classification).
    """
    log_path = executions_dir / f"{snapshot_ts}.jsonl"
    result: dict[str, Any] = {
        "snapshot_ts": snapshot_ts,
        "path": str(log_path),
        "exists": log_path.is_file(),
        "checked": 0,
        "unconfirmable": 0,
        "counts": {},
        "actions": [],
    }
    if not result["exists"]:
        return result

    counts: dict[str, int] = {}
    actions: list[dict[str, Any]] = []
    for raw in log_path.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            counts["malformed"] = counts.get("malformed", 0) + 1
            continue
        action_block = row.get("action") or {}
        try:
            kind = ActionKind(action_block.get("kind"))
        except ValueError:
            kind = None
        category = _ORDER_HISTORY_CATEGORY.get(kind) if kind else None
        if category is None:
            result["unconfirmable"] += 1
            continue

        order_link_id = action_block.get("order_link_id") or ""
        symbol = action_block.get("product_id") or None
        log_status = str(row.get("status") or "unknown")

        try:
            history = await client.get_order_history(
                category=category,
                order_link_id=order_link_id,
                symbol=symbol,
            )
            landed = len(history) > 0
        except BybitAPIError as exc:
            classification = "query-error"
            landed = None
            counts[classification] = counts.get(classification, 0) + 1
            actions.append({
                "order_link_id": order_link_id,
                "kind": kind.value,
                "product_id": symbol,
                "log_status": log_status,
                "bybit_landed": landed,
                "classification": classification,
                "error": str(exc),
            })
            result["checked"] += 1
            continue

        if landed:
            classification = "confirmed-landed"
        elif log_status == "ok":
            classification = "desync"
        else:
            classification = "no-trace"
        counts[classification] = counts.get(classification, 0) + 1
        actions.append({
            "order_link_id": order_link_id,
            "kind": kind.value,
            "product_id": symbol,
            "log_status": log_status,
            "bybit_landed": landed,
            "classification": classification,
        })
        result["checked"] += 1

    result["counts"] = counts
    result["actions"] = actions
    return result


def _confirmable_order_links(actions: list[Action]) -> list[dict[str, Any]]:
    """Extract the Bybit-confirmable order links a cycle is about to place, for
    the pending-intent marker (ah.7). Only orders that hit `/v5/order/create`
    and show in `/v5/order/history` are confirmable: SWAP_SPOT (spot),
    OPEN_PERP_SHORT / CLOSE_PERP (linear), and BOTH legs of a funding-carry
    open/close (spot + perp, under the derived `_spot` / `_perp` link ids).
    Earn / LM / advance-Earn / Alpha have no order-history endpoint and are
    omitted — a crash there is recoverable from the next snapshot read.
    """
    links: list[dict[str, Any]] = []
    for a in actions:
        cat = _ORDER_HISTORY_CATEGORY.get(a.kind)
        if cat is not None:
            links.append({
                "order_link_id": a.order_link_id,
                "category": cat,
                "symbol": a.product_id,
                "kind": a.kind.value,
                "coin": a.coin,
            })
        elif a.kind in (
            ActionKind.OPEN_FUNDING_CARRY, ActionKind.CLOSE_FUNDING_CARRY
        ):
            spot_link = (
                a.extra.get("spot_order_link_id") or f"{a.order_link_id}_spot"
            )
            perp_link = (
                a.extra.get("perp_order_link_id") or f"{a.order_link_id}_perp"
            )
            links.append({
                "order_link_id": spot_link, "category": "spot",
                "symbol": a.product_id, "kind": a.kind.value, "coin": a.coin,
            })
            links.append({
                "order_link_id": perp_link, "category": "linear",
                "symbol": a.product_id, "kind": a.kind.value, "coin": a.coin,
            })
    return links


async def verify_order_links(
    client: BybitClient, links: list[dict[str, Any]]
) -> dict[str, Any]:
    """Cross-check `{order_link_id, category, symbol}` links against live Bybit
    order-history (ah.7 startup gate). READ-ONLY — classifies each link like
    `verify_executions_against_bybit` but with no log row to compare against:

      - `confirmed-landed`: an order with this link exists on Bybit → a real
        position the cycle never recorded → caller HALTs.
      - `no-trace`: no order → nothing opened, safe to clear the marker.
      - `query-error`: the history lookup failed → can't confirm → caller HALTs
        (never assume safe next to a possible open position).
    """
    counts: dict[str, int] = {}
    checked: list[dict[str, Any]] = []
    for link in links:
        oid = link.get("order_link_id") or ""
        category = link.get("category") or "linear"
        symbol = link.get("symbol")
        try:
            history = await client.get_order_history(
                category=category, order_link_id=oid, symbol=symbol,
            )
            landed = len(history) > 0
            classification = "confirmed-landed" if landed else "no-trace"
            entry = {**link, "bybit_landed": landed, "classification": classification}
        except BybitAPIError as exc:
            classification = "query-error"
            entry = {
                **link, "bybit_landed": None,
                "classification": classification, "error": str(exc),
            }
        counts[classification] = counts.get(classification, 0) + 1
        checked.append(entry)
    return {"checked": len(links), "counts": counts, "actions": checked}


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


async def _redeem_onchain_by_position(
    client: Any, action: Action, target_native: Decimal
) -> dict[str, Any]:
    """Redeem an OnChain (non-LST) Earn product. Unlike pooled
    FlexibleSaving, OnChain positions are per-stake: each carries its own
    `id` and a Redeem WITHOUT `redeemPositionId` returns retCode 180020
    "Position not found" (live TON, 2026-06-06/07). Fetch the live
    positions for the product and redeem them one id at a time until the
    requested native amount is covered. Returns a summary dict; raises the
    last BybitAPIError only when every position-level redeem failed."""
    positions = await client.get_earn_positions(category=action.category)
    rows = [
        p
        for p in positions
        if str(getattr(p, "productId", "")) == str(action.product_id)
        and (getattr(p, "id", None) or "")
    ]
    if not rows:
        raise BybitAPIError(
            180020, "no OnChain position id to redeem", "/v5/earn/place-order"
        )
    remaining = target_native
    redeemed: list[str] = []
    last_err: BybitAPIError | None = None
    for p in rows:
        if remaining <= 0:
            break
        pid = str(p.id)
        # Prefer the redeemable (available) amount; fall back to the full
        # staked amount when Bybit doesn't surface availableAmount.
        try:
            avail = Decimal(str(getattr(p, "availableAmount", "") or "0"))
        except (InvalidOperation, TypeError):
            avail = Decimal(0)
        try:
            staked = Decimal(str(getattr(p, "amount", "0") or "0"))
        except (InvalidOperation, TypeError):
            staked = Decimal(0)
        pos_qty = avail if avail > 0 else staked
        if pos_qty <= 0:
            continue
        redeem_qty = min(pos_qty, remaining)
        try:
            out = await client.place_earn_order(
                category=action.category,
                product_id=action.product_id,
                amount=str(redeem_qty),
                side="Redeem",
                coin=action.coin,
                account_type=_ACCOUNT_TYPE[action.category],
                order_link_id=f"{action.order_link_id}_{pid}"[:36],
                redeem_position_id=pid,
            )
            redeemed.append(out.orderId)
            remaining -= redeem_qty
        except BybitAPIError as e:
            last_err = e
            continue
    if not redeemed:
        raise last_err or BybitAPIError(
            180020, "all OnChain position redeems failed", "/v5/earn/place-order"
        )
    return {
        "orderId": redeemed[0],
        "redeemed_position_ids": redeemed,
        "count": len(redeemed),
    }


async def _live_earn_native_qty(
    client: Any, category: str, product_id: str
) -> Decimal | None:
    """Sum the live native-coin amount Bybit holds for one Earn product
    (all rows for the product_id — e.g. settled + Processing OnChain
    chunks). Used as the executor-side fallback so a non-stable REDEEM is
    always sized in coin units even when the planner left amount_native
    unset. Returns None on lookup failure or when nothing is held."""
    try:
        positions = await client.get_earn_positions(category=category)
    except BybitAPIError:
        return None
    total = Decimal(0)
    for p in positions:
        if str(getattr(p, "productId", "")) != str(product_id):
            continue
        try:
            amt = Decimal(str(getattr(p, "amount", "0")))
        except (InvalidOperation, TypeError):
            continue
        if amt > 0:
            total += amt
    return total if total > 0 else None
