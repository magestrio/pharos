"""Read-side queries for the cycle store (`data-store.5`).

Thin async functions over `asyncpg.Pool`. Each returns plain dicts /
lists of dicts — no pydantic models — so the API layer can wrap them
in its own response models without rebuilding the data once.

All queries respect Postgres index ordering: `cycles_started_at_idx`
DESC for time-ordered cycle listings, `events_event_ts_idx` DESC for
events. Limits default to comfortable values; the API exposes them as
query params.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg


async def list_cycles(
    pool: asyncpg.Pool,
    *,
    since: datetime | None = None,
    limit: int = 50,
    wake_reason_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """Most-recent-first cycle metadata. Each row is the cycles table
    row — no snapshot/decision payload (use `get_cycle` for the full
    detail panel)."""
    conditions = []
    params: list[Any] = []
    if since is not None:
        params.append(since)
        conditions.append(f"started_at >= ${len(params)}")
    if wake_reason_prefix is not None:
        params.append(f"{wake_reason_prefix}%")
        conditions.append(f"wake_reason LIKE ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    sql = (
        "SELECT cycle_ts, started_at, finished_at, result, wake_reason, "
        "       confidence, expected_apr_pct, actions_planned, "
        "       actions_executed, error "
        f"FROM cycles {where} "
        "ORDER BY started_at DESC "
        f"LIMIT ${len(params)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def get_cycle(
    pool: asyncpg.Pool, cycle_ts: datetime
) -> dict[str, Any] | None:
    """Full cycle detail: cycles row + snapshot JSONB + decision JSONB +
    positions[] + executions[] + linked events[]. Returns None when
    the cycle doesn't exist (lets the API map to 404 cleanly)."""
    async with pool.acquire() as conn:
        cycle = await conn.fetchrow(
            "SELECT * FROM cycles WHERE cycle_ts = $1", cycle_ts
        )
        if cycle is None:
            return None
        snapshot = await conn.fetchval(
            "SELECT payload FROM snapshots WHERE cycle_ts = $1", cycle_ts
        )
        decision = await conn.fetchval(
            "SELECT payload FROM decisions WHERE cycle_ts = $1", cycle_ts
        )
        positions = await conn.fetch(
            "SELECT venue, product_id, coin, "
            "       amount::text AS amount, "
            "       amount_usd::text AS amount_usd "
            "FROM positions_snapshot WHERE cycle_ts = $1 "
            "ORDER BY venue, product_id",
            cycle_ts,
        )
        executions = await conn.fetch(
            "SELECT idx, action, status, error FROM executions "
            "WHERE cycle_ts = $1 ORDER BY idx",
            cycle_ts,
        )
        events = await conn.fetch(
            "SELECT id, event_ts, kind, severity, position_id, coin, "
            "       payload "
            "FROM events WHERE triggered_cycle_ts = $1 "
            "ORDER BY event_ts",
            cycle_ts,
        )
    return {
        **dict(cycle),
        "snapshot": snapshot,
        "decision": decision,
        "positions": [dict(r) for r in positions],
        "executions": [dict(r) for r in executions],
        "events": [dict(r) for r in events],
    }


async def list_events(
    pool: asyncpg.Pool,
    *,
    since: datetime | None = None,
    limit: int = 100,
    kind: str | None = None,
    severity: str | None = None,
) -> list[dict[str, Any]]:
    """Most-recent-first watcher events. Filters: `since` (event_ts >=),
    `kind` (exact), `severity` (exact). All optional."""
    conditions = []
    params: list[Any] = []
    if since is not None:
        params.append(since)
        conditions.append(f"event_ts >= ${len(params)}")
    if kind is not None:
        params.append(kind)
        conditions.append(f"kind = ${len(params)}")
    if severity is not None:
        params.append(severity)
        conditions.append(f"severity = ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    sql = (
        "SELECT id, event_ts, kind, severity, position_id, coin, "
        "       payload, triggered_cycle_ts "
        f"FROM events {where} "
        "ORDER BY event_ts DESC "
        f"LIMIT ${len(params)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def get_latest_snapshot(
    pool: asyncpg.Pool,
) -> dict[str, Any] | None:
    """Most-recent snapshot payload (full JSONB → dict). Powers the
    Earn-Explorer `/earn/products` endpoint, which reads the product
    universe + `earn_funding` from the latest snapshot. None when no
    snapshot recorded yet."""
    async with pool.acquire() as conn:
        payload = await conn.fetchval(
            "SELECT payload FROM snapshots ORDER BY cycle_ts DESC LIMIT 1"
        )
    return payload if isinstance(payload, dict) else None


async def funding_history(
    pool: asyncpg.Pool,
    coin: str,
    *,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """Cross-cycle funding-rate series for one Earn coin, oldest→newest.

    Reads `earn_funding.<COIN>.funding_rate` from each snapshot, falling
    back to the hedge-subset `perp_market.<COIN>.funding_rate_8h` for
    cycles recorded before `earn_funding` existed. Depth is bounded by how
    many cycles carry the data (forward-only — see plan). Returns rows with
    `cycle_ts`, `funding_rate` (text|None), `funding_interval_hours`
    (text|None)."""
    key = coin.upper()
    sql = (
        "SELECT cycle_ts, "
        "  COALESCE(payload->'earn_funding'->$1->>'funding_rate', "
        "           payload->'perp_market'->$1->>'funding_rate_8h') "
        "    AS funding_rate, "
        "  COALESCE(payload->'earn_funding'->$1->>'funding_interval_hours', "
        "           payload->'perp_market'->$1->>'funding_interval_hours') "
        "    AS funding_interval_hours "
        "FROM snapshots "
        "WHERE (payload->'earn_funding' ? $1) OR (payload->'perp_market' ? $1) "
        "ORDER BY cycle_ts DESC LIMIT $2"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, key, limit)
    # Query is newest-first for the LIMIT; the chart wants oldest→newest.
    return [dict(r) for r in reversed(rows)]


async def funding_7d_averages(
    pool: asyncpg.Pool, *, days: int = 7
) -> dict[str, float]:
    """Per-coin average earn `funding_rate` over the trailing `days` (default
    7), across all snapshots. Powers the Earn-Explorer `funding_7d_annual_pct`
    fallback when `perp_market.funding_rate_7d_avg` is absent (it covers only
    the ~12-16 hedge coins). Forward-only — shallow until a week of cycles
    accrues. No API calls — one aggregate query over `snapshots`.

    `jsonb_each` is wrapped in COALESCE so snapshots predating `earn_funding`
    (NULL key) contribute zero rows instead of erroring; the regex guard +
    `::numeric` cast skip any malformed/empty funding string."""
    sql = """
        SELECT f.key AS coin,
               avg((f.value->>'funding_rate')::numeric) AS avg_rate
        FROM snapshots s
        CROSS JOIN LATERAL jsonb_each(
            COALESCE(s.payload->'earn_funding', '{}'::jsonb)
        ) AS f
        WHERE s.cycle_ts >= now() - make_interval(days => $1)
          AND (f.value->>'funding_rate') ~ '^-?[0-9.]+$'
        GROUP BY f.key
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, days)
    return {
        r["coin"].upper(): float(r["avg_rate"])
        for r in rows
        if r["avg_rate"] is not None
    }


async def get_current_portfolio(
    pool: asyncpg.Pool,
) -> dict[str, Any] | None:
    """Latest cycle's positions + a trimmed wallet block from the
    snapshot. Returns None when no cycles recorded yet — caller maps to
    a 404 or empty-state UI."""
    async with pool.acquire() as conn:
        latest = await conn.fetchrow(
            "SELECT cycle_ts, started_at, result, wake_reason FROM cycles "
            "ORDER BY started_at DESC LIMIT 1"
        )
        if latest is None:
            return None
        cycle_ts = latest["cycle_ts"]
        positions = await conn.fetch(
            "SELECT venue, product_id, coin, "
            "       amount::text AS amount, "
            "       amount_usd::text AS amount_usd "
            "FROM positions_snapshot WHERE cycle_ts = $1 "
            "ORDER BY venue, product_id",
            cycle_ts,
        )
        snapshot = await conn.fetchval(
            "SELECT payload FROM snapshots WHERE cycle_ts = $1", cycle_ts
        )
    wallet = (snapshot or {}).get("wallet") if isinstance(snapshot, dict) else None
    return {
        **dict(latest),
        "positions": [dict(r) for r in positions],
        "wallet": wallet,
    }
