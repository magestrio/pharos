"""Auto-extracted submodule (ah.25 execute split). See package __init__."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from agent.reason.schema import Decision
from agent.reason.venues import (
    VENUE_REGISTRY,
)
from agent.sandbox.execute.common import (
    _ALPHA_CATEGORY,
    _amount_to_usd,
)
from agent.sandbox.execute.types import (
    _CurrentPos,
    _TargetPos,
)
from agent.sandbox.snapshot import (
    PerpInfo,
    Snapshot,
)


def _alpha_current_positions(
    alpha_positions: list[dict[str, Any]],
) -> dict[tuple[str, str], _CurrentPos]:
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
        # Non-redeemable while the on-chain stake is still settling.
        redeemable = str(data.get("status") or "").strip().lower() != "processing"
        r_native = amt if redeemable else Decimal(0)
        r_usd = amount_usd if redeemable else Decimal(0)
        existing = out.get((category, pid))
        if existing is not None:
            # Sum with prior entry (multiple Bybit rows for the same
            # subscription state — e.g. settled + Processing chunks).
            out[(category, pid)] = _CurrentPos(
                coin=existing.coin,
                amount_usd=existing.amount_usd + amount_usd,
                amount_native=existing.amount_native + amt,
                redeemable_native=(existing.redeemable_native or Decimal(0)) + r_native,
                redeemable_usd=(existing.redeemable_usd or Decimal(0)) + r_usd,
            )
        else:
            out[(category, pid)] = _CurrentPos(
                coin=coin, amount_usd=amount_usd, amount_native=amt,
                redeemable_native=r_native, redeemable_usd=r_usd,
            )
    return out


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
