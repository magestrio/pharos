"""Shared constants + leaf helpers for the execution layer (ah.25 split).

The utility leaf of the `execute` package import DAG: constants, pure
calculators, and snapshot readers used by both the planner and the dispatch
arms. Imports only stdlib, external packages, and `.types` — never a sibling
submodule — so everything else can import from here without a cycle.
"""

from __future__ import annotations

import os
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agent.reason.venues import (
    BASIC_EARN_CATEGORIES,
    LM_BASE_LEG_FRACTION,
    SLOW_SETTLE_CATEGORIES,
)
from agent.sandbox.execute.types import (
    Action,
    ActionKind,
    _CurrentPos,
)
from agent.sandbox.snapshot import (
    HEDGE_MARGIN_BUFFER,
    STABLES,
    PerpInfo,
    Snapshot,
)

EXECUTIONS_DIR = Path(__file__).parent.parent / "executions"


MIN_ACTION_USDC = Decimal("0.50")


_STABLES = STABLES


_USDC_PAIR_COINS: frozenset[str] = frozenset({"BTC", "ETH", "SOL"})


def _orphan_sell_quote(coin_u: str) -> tuple[str, str]:
    """Pick the spot pair + destination coin for an orphan sell. Returns
    `(symbol, dest_coin)`. USDC is preferred for the whitelisted coins
    so the vault rebases to USDC directly; everything else falls back
    to the universal USDT quote."""
    if coin_u in _USDC_PAIR_COINS:
        return f"{coin_u}USDC", "USDC"
    return f"{coin_u}USDT", "USDT"


_ACCOUNT_TYPE: dict[str, str] = {
    "FlexibleSaving": "UNIFIED",
    "OnChain": "FUND",
    "DualAssets": "UNIFIED",
    "DiscountBuy": "UNIFIED",
}


_BASIC_EARN_CATEGORIES = BASIC_EARN_CATEGORIES


_LM_CATEGORY: str = "LiquidityMining"


def _redeem_settles_in_cycle(action: Action) -> bool:
    """False for a REDEEM_EARN whose category settles slower than a cycle
    (OnChain ~4d Processing, per `SLOW_SETTLE_CATEGORIES`) — its freed coin
    must NOT be credited toward in-cycle funding. True for fast-settling
    redeems and non-redeems."""
    if action.kind != ActionKind.REDEEM_EARN:
        return True
    return action.category not in SLOW_SETTLE_CATEGORIES


def _liquid_for_coin(wallet: Any, coin: str) -> Decimal:
    """Spendable wallet balance for `coin` this cycle. USDC/USDT use the
    UNIFIED+FUND `liquid_*_usd` fields (the executor can pull FUND→UNIFIED);
    other coins read the UNIFIED balance map."""
    cu = (coin or "").upper()
    if cu == "USDC":
        return wallet.liquid_usdc_usd
    if cu == "USDT":
        return wallet.liquid_usdt_usd
    return wallet.unified_coin_balances.get(coin, Decimal(0))


_LM_QUOTE_ACCOUNT_TYPE: str = "UNIFIED"


_ALPHA_CATEGORY: str = "AlphaFarm"


_ALPHA_PAY_TOKEN_CODE: str = "CEX_1"  # USDT


_ALPHA_DEFAULT_SLIPPAGE: str = os.getenv("VAULT8004_ALPHA_SLIPPAGE", "0.01")


ALPHA_EXEC_ENABLED: bool = os.getenv("VAULT8004_ALPHA_EXEC_ENABLED", "0") == "1"


_ADVANCE_EARN_CATEGORIES: frozenset[str] = frozenset({"DualAssets", "DiscountBuy"})


HEDGE_NOTIONAL_REBALANCE_THRESHOLD = Decimal("0.10")


# Min spot-swap notional. Also the disposal-sell floor (orphan/excess +
# redeem-settle freed coin): $5 is Bybit's typical spot `minOrderAmt` (e.g.
# ETHUSDC, USD1USDT, IOUSDT all = $5), so a sub-$5 sell would just bounce with
# retCode=170140 "Order value exceeded lower limit". A non-stable remainder
# below this is genuinely unsellable on Bybit — it stays as micro-dust.
MIN_SWAP_USDC = Decimal("5.00")


_STABLE_SWAP_HEADROOM = Decimal("1.005")


_UNIFIED_SPEND_RESERVE_FACTOR = Decimal("0.01")


_UNIFIED_SPEND_RESERVE_FLOOR = Decimal("0.20")


_FUNDING_SWAP_FEE_FACTOR = Decimal("0.999")


_CARRY_OPEN_USDT_FACTOR = Decimal(1) + HEDGE_MARGIN_BUFFER


def _coin_from_perp_symbol(symbol: str) -> str:
    """Strip the USDT settle-coin suffix from a linear-perp symbol to
    get the base coin. Sandbox hedges are always USDT-settled (per
    `collect_snapshot`), so symbols not ending in `USDT` are not
    something this diff should touch — caller filters them out."""
    return symbol[:-4] if symbol.endswith("USDT") else symbol


_AUTO_HEDGE_CATEGORIES = BASIC_EARN_CATEGORIES


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


_ADVANCE_EARN_AMOUNT_FIELDS: tuple[str, ...] = (
    "amount",
    "stakeAmount",
    "quoteAmount",
    "purchaseAmount",
    "positionAmount",
)


_OFFER_PREFIX = " offer="


_CARRY_PAIRED_NOTIONAL_TOLERANCE = Decimal("0.05")


MAX_CARRY_CLOSE_ATTEMPTS = 3


_CARRY_SPOT_FILL_POLL_SECONDS = 10.0


_CARRY_SPOT_FILL_POLL_INTERVAL = 0.5


_PERP_SL_RETRY_BACKOFF = 1.0


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


def _transfer_quantum(coin: str | None) -> Decimal:
    """Decimal quantum for a UNIFIED↔FUND internal transfer. Bybit rejects
    amounts whose scale exceeds the coin's transfer accuracy (retCode
    131210 "transfer amount scale more than accuracy length"). USDT's
    accuracy is coarser than 6dp — a 6dp move like `44.574262` 131210's
    even though USDC settles fine at 6dp (`bybit-sandbox.68`). Stablecoins
    are ~$1, so a coarse 2dp move is well within every stable's accuracy
    and the 0.5% sizing buffer dwarfs the ≤$0.01 rounding loss. Non-stable
    coins keep 6dp (the prior default — fine for the coins we move and too
    fine-grained to drop for sub-dollar tokens)."""
    if coin and (coin == "USDC" or coin.upper() in _STABLES):
        return Decimal("0.01")
    return Decimal("0.000001")


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


def _coin_mark(snapshot: Snapshot, coin: str) -> Decimal | None:
    """Mark price for `coin` from the perp market, or None when missing /
    non-positive. Shared resolver so the `.2` / `.3` coverage helpers price
    the same way the planner and budget passes do."""
    info = (snapshot.perp_market or {}).get(coin) or (
        snapshot.perp_market or {}
    ).get((coin or "").upper())
    mark = getattr(info, "mark_price", None) if info else None
    if mark is None or mark <= 0:
        return None
    return mark


def _lm_base_leg_native(usd_base_leg: Decimal, base: str, snapshot: Snapshot) -> Decimal:
    """Native base-coin amount for an LM base leg worth `usd_base_leg`,
    priced at the perp mark. Sized identically to the hedge target
    (`_lm_hedge_targets` → half-notional / mark) so the held/planned LM
    long and its paired short net to zero in the naked-perp trimmer
    instead of churning open/close on rounding. Returns 0 with no mark."""
    info = (snapshot.perp_market or {}).get(base) or (snapshot.perp_market or {}).get(
        base.lower()
    )
    mark = getattr(info, "mark_price", None) if info else None
    if not mark or mark <= 0:
        return Decimal(0)
    return usd_base_leg / mark


def _coin_to_long_exposure(snapshot: Snapshot) -> dict[str, Decimal]:
    """Sum native LONG exposure per coin from currently-held Earn AND
    Liquidity-Mining positions. UNIFIED wallet balance is NOT included
    here — caller adds it on top because it's the only thing actually
    sellable via SWAP_SPOT. Stables are skipped (irrelevant for hedge
    balance).

    LM positions hold half their value in the non-stable BASE coin (a
    50/50 LP), which is genuine long exposure backing the paired perp
    short. Without it the naked-perp trimmer would treat the LM hedge as
    unbacked and close it (`bybit_lm` was historically unhedged)."""
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
    for pos in snapshot.lm_positions or []:
        product = _lm_product_from_snapshot(snapshot, str(pos.get("productId") or ""))
        if product is None:
            continue
        parts = product.coin.split("/", 1)
        if len(parts) != 2:
            continue
        base = parts[0].upper()
        if not base or base in _STABLES:
            continue
        native = _lm_base_leg_native(
            _lm_principal_usd(pos) * LM_BASE_LEG_FRACTION, base, snapshot
        )
        if native > 0:
            out[base] = out.get(base, Decimal(0)) + native
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


def _coin_wallet_native(snapshot: Snapshot, coin: str) -> Decimal:
    """Native coin balance across UNIFIED + FUND (the freed-Earn coin lands in
    FUND for OnChain). Mirrors the merge in `_orphan_spot_sell_actions`."""
    cu = (coin or "").upper()
    total = Decimal(0)
    for src in (
        snapshot.wallet.unified_coin_balances or {},
        getattr(snapshot.wallet, "fund_coin_balances", None) or {},
    ):
        for c, raw in src.items():
            if (c or "").upper() != cu:
                continue
            try:
                total += raw if isinstance(raw, Decimal) else Decimal(str(raw))
            except (InvalidOperation, TypeError):
                continue
    return total


_STABLE_CONSOLIDATE_PAIRS: dict[str, tuple[str, str]] = {
    "USD1": ("USD1USDT", "USDT"),
}


_STABLE_LOT = Decimal("0.01")


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


REDEEM_SETTLE_TIMEOUT_SECONDS: float = 180


_ORDER_HISTORY_CATEGORY: dict[ActionKind, str] = {
    ActionKind.SWAP_SPOT: "spot",
    ActionKind.OPEN_PERP_SHORT: "linear",
    ActionKind.CLOSE_PERP: "linear",
}


def _is_fully_processing(pos: _CurrentPos | None) -> bool:
    """True when `pos` exists but its ENTIRE balance is still `Processing`
    (un-redeemable). Callers that don't compute status (Alpha / carry /
    legacy) leave `redeemable_*` as None → treated as redeemable (False),
    preserving prior behavior. Used to suppress doomed REDEEM_EARN calls
    (retCode=180020) on positions Bybit hasn't settled yet."""
    if pos is None:
        return False
    if pos.redeemable_native is None and pos.redeemable_usd is None:
        return False
    return (pos.redeemable_native or Decimal(0)) <= 0 and (
        pos.redeemable_usd or Decimal(0)
    ) <= 0


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


def _order_link_id(snapshot_ts: str, idx: int) -> str:
    return f"sandbox-{snapshot_ts}-{idx:03d}"


DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE = 0.6


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
