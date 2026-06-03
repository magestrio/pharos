"""Cycle/portfolio/event writer (`data-store.3`).

`record_cycle()` writes one cycle's full state into the Postgres store
in a single transaction. Idempotent via `ON CONFLICT (cycle_ts) DO
NOTHING` — re-recording a cycle is a no-op (files remain source of
truth; this DB is a derived view).

Caller is `loop.py:run_loop` — invoked after every `run_one_cycle`
return, wrapped in its own try/except so DB issues never break the
file-based path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


def _parse_cycle_ts(outcome: dict[str, Any]) -> datetime | None:
    """Resolve the cycle_ts for this row. Prefer the snapshot filename
    stem (`20260529T160255Z`) since that's the canonical key the rest of
    the system uses; fall back to `started_at` for error cycles that
    never produced a snapshot."""
    snap_name = outcome.get("snapshot_filename")
    if snap_name:
        stem = snap_name.removesuffix(".json")
        try:
            return datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            pass
    started = outcome.get("started_at")
    if started:
        try:
            return datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


# Stablecoins priced 1:1 USD (USDC uses snapshot's measured peg).
_STABLE_COINS = frozenset(
    {
        "USDT",
        "USD1",
        "USDE",
        "USDTB",
        "USD0",
        "SUSDE",
        "DAI",
        "FDUSD",
        "BUSD",
        "USDP",
        "TUSD",
        "LUSD",
        "GHO",
        "PYUSD",
    }
)


def _price_per_coin(coin: str | None, snapshot: dict[str, Any]) -> Decimal | None:
    """Return spot USD price for one unit of `coin` based on what the
    snapshot has on hand. Order of preference:
      1. Stablecoins → 1.0 (USDC uses the measured peg price).
      2. BTC / ETH → `market.btc_price` / `market.eth_price`.
      3. Other coins → `perp_market[coin].mark_price` (Bybit linear-perp mark).
    Returns None when no source covers the coin — caller leaves
    `amount_usd` NULL so the UI can distinguish "unpriced" from "$0".
    """
    if not coin:
        return None
    c = coin.upper()
    if c == "USDC":
        peg = (snapshot.get("usdc_peg") or {}).get("price_usd")
        d = _to_decimal(peg)
        return d if d is not None else Decimal("1")
    if c in _STABLE_COINS:
        return Decimal("1")
    market = snapshot.get("market") or {}
    if c == "BTC":
        return _to_decimal(market.get("btc_price"))
    if c == "ETH":
        return _to_decimal(market.get("eth_price"))
    perp_market = snapshot.get("perp_market") or {}
    perp = perp_market.get(c) or perp_market.get(coin)
    if isinstance(perp, dict):
        return _to_decimal(perp.get("mark_price"))
    # WLFI / promo-coin tail: no spot/perp price in the snapshot yet.
    return None


def _amount_usd(
    amount: Decimal | None, coin: str | None, snapshot: dict[str, Any]
) -> Decimal | None:
    """USD value of `amount` units of `coin` at snapshot time.
    Returns None when amount or price is unavailable."""
    if amount is None:
        return None
    price = _price_per_coin(coin, snapshot)
    if price is None:
        return None
    return amount * price


def _extract_positions(
    raw_snapshot: dict[str, Any],
) -> list[tuple[str, str, str | None, Decimal | None, Decimal | None]]:
    """Flatten the snapshot's per-venue position arrays into uniform
    rows: (venue, product_id, coin, amount, amount_usd). Empty/zero
    positions are skipped — they pollute the table without adding signal.

    `amount_usd` is derived via `_price_per_coin` against the same
    snapshot the agent saw at decision time, so historical rows price
    against historical marks rather than today's price.
    """
    out: list[tuple[str, str, str | None, Decimal | None, Decimal | None]] = []

    for p in raw_snapshot.get("earn_positions") or []:
        amount = _to_decimal(p.get("amount"))
        if amount is None or amount == 0:
            continue
        product_id = str(p.get("productId") or p.get("id") or "")
        coin = p.get("coin")
        # Bybit splits earn into categories; surface the category so the
        # web grouping can separate Flexible / OnChain / DiscountBuy /
        # DualAsset / HoldToEarn instead of lumping them as "earn".
        category = str(p.get("category") or "").strip()
        venue = _earn_venue(category)
        out.append((venue, product_id, coin, amount, _amount_usd(amount, coin, raw_snapshot)))

    for p in raw_snapshot.get("lm_positions") or []:
        product_id = str(p.get("positionId") or p.get("id") or "")
        coin = p.get("coin") or p.get("baseCoin")
        amount = _to_decimal(p.get("baseAmount") or p.get("amount"))
        # LM is a USDC-quoted CPMM pool — the most useful USD signal is
        # the position's quote-side value when the snapshot writes it.
        amount_usd = _to_decimal(p.get("quoteAmount") or p.get("notionalUsd"))
        if amount_usd is None:
            amount_usd = _amount_usd(amount, coin, raw_snapshot)
        out.append(("bybit_lm", product_id, coin, amount, amount_usd))

    for p in raw_snapshot.get("alpha_positions") or []:
        product_id = str(p.get("tokenCode") or p.get("symbol") or "")
        coin = p.get("symbol")
        amount = _to_decimal(p.get("amount") or p.get("balance"))
        amount_usd = _to_decimal(p.get("tokenAmountUsd") or p.get("notionalUsd"))
        if amount_usd is None:
            amount_usd = _amount_usd(amount, coin, raw_snapshot)
        out.append(("bybit_alpha", product_id, coin, amount, amount_usd))

    for p in raw_snapshot.get("perp_positions") or []:
        symbol = str(p.get("symbol") or "")
        coin = symbol.removesuffix("USDT") if symbol.endswith("USDT") else symbol
        amount = _to_decimal(p.get("size") or p.get("positionValue"))
        # Perp positions usually carry positionValue in USD already.
        amount_usd = _to_decimal(p.get("positionValue") or p.get("notionalUsd"))
        if amount_usd is None:
            amount_usd = _amount_usd(amount, coin, raw_snapshot)
        out.append(("perp", symbol, coin, amount, amount_usd))

    return out


def _earn_venue(category: str) -> str:
    """Map Bybit Earn `category` to our venue id namespace. Unknown
    categories fall back to a `bybit_earn:<category>` form so they're
    still distinguishable rather than silently merged."""
    norm = category.lower().replace(" ", "").replace("-", "_")
    if norm in ("flexiblesaving", "flexible", "easyearn"):
        return "bybit_flex"
    if norm in ("onchain", "onchainearn"):
        return "bybit_onchain"
    if norm in ("discountbuy",):
        return "bybit_discount_buy"
    if norm in ("dualasset", "dualassets"):
        return "bybit_dual_asset"
    if norm in ("holdtoearn", "hold_to_earn"):
        return "bybit_hold_to_earn"
    if not category:
        return "bybit_earn"
    return f"bybit_earn:{category}"


async def record_event(
    pool: asyncpg.Pool, event: dict[str, Any]
) -> int | None:
    """Persist one watcher event. Returns the generated `id` so the
    watcher can later cross-link it to the wake-driven cycle via
    `record_cycle(..., triggered_event_ids=[id, ...])`.

    Returns None on malformed input (no `ts` field — can't index without
    a time anchor). Other failures propagate; caller in the watcher
    task wraps in try/except so DB write errors don't kill polling.
    """
    event_ts = _parse_iso(event.get("ts"))
    if event_ts is None:
        log.warning(
            "record_event: missing or unparseable `ts` field: %r — skipping",
            event.get("ts"),
        )
        return None
    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            """
            INSERT INTO events (
                event_ts, kind, severity, position_id, coin, payload
            ) VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            event_ts,
            str(event.get("kind") or "unknown"),
            str(event.get("severity") or "P2"),
            event.get("position_id"),
            event.get("coin"),
            event,
        )
    return row_id


async def record_cycle(
    pool: asyncpg.Pool,
    *,
    outcome: dict[str, Any],
    raw_snapshot: dict[str, Any] | None = None,
    raw_decision: dict[str, Any] | None = None,
    triggered_event_ids: list[int] | None = None,
) -> bool:
    """Persist one cycle's state. Returns True on success, False on
    skip (no resolvable cycle_ts → outcome too degraded to record).

    Per-cycle transaction: cycles row is the parent, snapshots /
    decisions / positions_snapshot / executions are children. Either
    all rows land or none.

    Idempotent — re-recording the same cycle_ts is a no-op (won't
    update existing rows; files are the audit log if you need an
    update).
    """
    cycle_ts = _parse_cycle_ts(outcome)
    if cycle_ts is None:
        log.warning(
            "record_cycle: no resolvable cycle_ts in outcome (snapshot_filename=%r, "
            "started_at=%r) — skipping DB write",
            outcome.get("snapshot_filename"),
            outcome.get("started_at"),
        )
        return False

    started_at = _parse_iso(outcome.get("started_at")) or cycle_ts
    finished_at = _parse_iso(outcome.get("finished_at"))
    actions: list[dict[str, Any]] = outcome.get("actions") or []
    positions = _extract_positions(raw_snapshot) if raw_snapshot else []

    async with pool.acquire() as conn, conn.transaction():
        cycle_inserted = await conn.fetchval(
            """
            INSERT INTO cycles (
                cycle_ts, started_at, finished_at, result, wake_reason,
                confidence, expected_apr_pct,
                actions_planned, actions_executed, error
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7,
                $8, $9, $10
            )
            ON CONFLICT (cycle_ts) DO NOTHING
            RETURNING cycle_ts
            """,
            cycle_ts,
            started_at,
            finished_at,
            str(outcome.get("result") or "unknown"),
            str(outcome.get("wake_reason") or "heartbeat"),
            outcome.get("confidence"),
            outcome.get("expected_apr_pct"),
            outcome.get("actions_planned"),
            outcome.get("actions_executed"),
            outcome.get("error"),
        )
        if cycle_inserted is None:
            # cycle_ts already recorded — skip child rows too to honor
            # idempotency
            return False

        # JSONB codec registered in `pool.py` does the json.dumps —
        # pass the dict directly so we don't double-encode.
        if raw_snapshot is not None:
            await conn.execute(
                "INSERT INTO snapshots (cycle_ts, payload) VALUES ($1, $2) "
                "ON CONFLICT (cycle_ts) DO NOTHING",
                cycle_ts,
                raw_snapshot,
            )

        if raw_decision is not None:
            await conn.execute(
                "INSERT INTO decisions (cycle_ts, payload) VALUES ($1, $2) "
                "ON CONFLICT (cycle_ts) DO NOTHING",
                cycle_ts,
                raw_decision,
            )

        for venue, product_id, coin, amount, amount_usd in positions:
            await conn.execute(
                """
                INSERT INTO positions_snapshot (
                    cycle_ts, venue, product_id, coin, amount, amount_usd
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (cycle_ts, venue, product_id) DO NOTHING
                """,
                cycle_ts, venue, product_id, coin, amount, amount_usd,
            )

        for idx, action in enumerate(actions):
            await conn.execute(
                """
                INSERT INTO executions (cycle_ts, idx, action, status, error)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (cycle_ts, idx) DO NOTHING
                """,
                cycle_ts,
                idx,
                action,  # codec encodes dict → JSONB
                str(action.get("status") or "unknown"),
                action.get("error"),
            )

        # Cross-link: events table carries triggered_cycle_ts once the
        # wake-driven cycle resolves. UI uses this to render "event X
        # caused cycle Y".
        if triggered_event_ids:
            await conn.execute(
                "UPDATE events SET triggered_cycle_ts = $1 "
                "WHERE id = ANY($2::bigint[])",
                cycle_ts,
                triggered_event_ids,
            )

    return True
