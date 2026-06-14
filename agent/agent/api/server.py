"""Vault8004 read-only API (`data-store.5`).

FastAPI app that the web frontend hits to render the history view +
current portfolio. Lives next to the agent on Hetzner; uvicorn worker
on the same box, fronted by Caddy for TLS. Postgres stays bound to
localhost — the API process is the only thing that talks to it.

CLI:
    # local dev (default DATABASE_URL from .env)
    uvicorn agent.api.server:app --reload

    # systemd / production
    uvicorn agent.api.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agent.sandbox.store import (
    funding_history,
    get_current_portfolio,
    get_cycle,
    get_latest_snapshot,
    list_cycles,
    list_events,
    open_pool,
)

log = logging.getLogger(__name__)

# Hours in a (non-leap) year, for annualizing a per-period funding rate.
# Annual = rate × (HOURS_PER_YEAR / funding_interval_hours). Matches the
# agent's `_annual_funding` (snapshot.py) — never a hardcoded × 3 × 365,
# which under-states 4h/1h perps.
_HOURS_PER_YEAR = 24 * 365
_DEFAULT_FUNDING_INTERVAL_HOURS = 8.0


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _annual_funding_pct(
    rate: float | None, interval_hours: float | None
) -> float | None:
    if rate is None:
        return None
    interval = interval_hours or _DEFAULT_FUNDING_INTERVAL_HOURS
    if interval <= 0:
        interval = _DEFAULT_FUNDING_INTERVAL_HOURS
    return rate * (_HOURS_PER_YEAR / interval) * 100.0


# ───────────────────────── response models ───────────────────────────


class CycleSummary(BaseModel):
    cycle_ts: datetime
    started_at: datetime
    finished_at: datetime | None = None
    result: str
    wake_reason: str
    confidence: float | None = None
    expected_apr_pct: float | None = None
    actions_planned: int | None = None
    actions_executed: int | None = None
    error: str | None = None


class PositionRow(BaseModel):
    venue: str
    product_id: str
    coin: str | None = None
    amount: str | None = None  # NUMERIC → str to preserve precision
    amount_usd: str | None = None


class ExecutionRow(BaseModel):
    idx: int
    action: dict[str, Any]
    status: str
    error: str | None = None


class EventRow(BaseModel):
    id: int
    event_ts: datetime
    kind: str
    severity: str
    position_id: str | None = None
    coin: str | None = None
    payload: dict[str, Any]
    triggered_cycle_ts: datetime | None = None


class CycleDetail(CycleSummary):
    snapshot: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    positions: list[PositionRow] = Field(default_factory=list)
    executions: list[ExecutionRow] = Field(default_factory=list)
    events: list[EventRow] = Field(default_factory=list)


class Portfolio(BaseModel):
    cycle_ts: datetime
    started_at: datetime
    result: str
    wake_reason: str
    positions: list[PositionRow] = Field(default_factory=list)
    wallet: dict[str, Any] | None = None


class EarnProductRow(BaseModel):
    category: str
    product_id: str
    coin: str
    effective_apr_pct: float
    apr_source: str
    # Bybit's own daily APR series (oldest→newest, pct), present only for
    # FlexibleSaving + OnChain. None for categories without a history feed.
    apr_history_pct: list[float] | None = None
    funding_rate: float | None = None  # current signed per-period rate
    funding_annual_pct: float | None = None
    mark_price: float | None = None


class EarnProducts(BaseModel):
    captured_at: str | None = None
    products: list[EarnProductRow] = Field(default_factory=list)


class FundingHistoryPoint(BaseModel):
    ts: datetime
    funding_rate: float | None = None
    funding_annual_pct: float | None = None


class FundingHistory(BaseModel):
    coin: str
    points: list[FundingHistoryPoint] = Field(default_factory=list)


# ───────────────────────── app + lifespan ────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the Postgres pool on startup, close on shutdown. DSN comes
    from `DATABASE_URL` in the env. If the pool is already injected
    (test fixture path), skip opening one of our own."""
    if getattr(app.state, "pool", None) is not None:
        yield
        return
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        # Run without a pool — endpoints will 503. Better than crashing
        # at import time; lets `--help`/healthz still work.
        log.warning(
            "DATABASE_URL not set — read endpoints will 503 until pool opens."
        )
        app.state.pool = None
        yield
        return
    async with open_pool(dsn) as pool:
        app.state.pool = pool
        yield


def build_app(pool: asyncpg.Pool | None = None) -> FastAPI:
    """Construct the FastAPI app. Optional `pool` parameter is for
    tests — pass an already-open pool and the lifespan skips its own
    init."""
    app = FastAPI(
        title="Vault8004 Agent API",
        description="Read-only history + portfolio queries.",
        lifespan=_lifespan,
    )
    app.state.pool = pool

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        pool_ok = isinstance(app.state.pool, asyncpg.Pool)
        return {"ok": pool_ok, "pool": "open" if pool_ok else "missing"}

    @app.get("/cycles", response_model=list[CycleSummary])
    async def cycles_endpoint(
        pool: Annotated[asyncpg.Pool, Depends(_get_pool)],
        since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        wake_reason_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        return await list_cycles(
            pool,
            since=since,
            limit=limit,
            wake_reason_prefix=wake_reason_prefix,
        )

    @app.get("/cycles/{cycle_ts}", response_model=CycleDetail)
    async def cycle_detail_endpoint(
        cycle_ts: datetime,
        pool: Annotated[asyncpg.Pool, Depends(_get_pool)],
    ) -> dict[str, Any]:
        row = await get_cycle(pool, cycle_ts)
        if row is None:
            raise HTTPException(status_code=404, detail="cycle not found")
        return row

    @app.get("/events", response_model=list[EventRow])
    async def events_endpoint(
        pool: Annotated[asyncpg.Pool, Depends(_get_pool)],
        since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        kind: str | None = None,
        severity: str | None = None,
    ) -> list[dict[str, Any]]:
        return await list_events(
            pool, since=since, limit=limit, kind=kind, severity=severity
        )

    @app.get("/portfolio/current", response_model=Portfolio)
    async def portfolio_endpoint(
        pool: Annotated[asyncpg.Pool, Depends(_get_pool)],
    ) -> dict[str, Any]:
        row = await get_current_portfolio(pool)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="no cycles recorded yet — portfolio empty",
            )
        return row

    @app.get("/earn/products", response_model=EarnProducts)
    async def earn_products_endpoint(
        pool: Annotated[asyncpg.Pool, Depends(_get_pool)],
        category: str | None = None,
        coin: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> EarnProducts:
        snap = await get_latest_snapshot(pool)
        if snap is None:
            raise HTTPException(
                status_code=404, detail="no snapshot recorded yet"
            )
        rows = _build_earn_rows(snap, category=category, coin=coin)
        rows.sort(key=lambda r: r.effective_apr_pct, reverse=True)
        return EarnProducts(captured_at=snap.get("captured_at"), products=rows[:limit])

    @app.get("/earn/funding-history", response_model=FundingHistory)
    async def earn_funding_history_endpoint(
        pool: Annotated[asyncpg.Pool, Depends(_get_pool)],
        coin: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 60,
    ) -> FundingHistory:
        rows = await funding_history(pool, coin, limit=limit)
        points = []
        for r in rows:
            rate = _to_float(r.get("funding_rate"))
            interval = _to_float(r.get("funding_interval_hours"))
            points.append(
                FundingHistoryPoint(
                    ts=r["cycle_ts"],
                    funding_rate=rate,
                    funding_annual_pct=_annual_funding_pct(rate, interval),
                )
            )
        return FundingHistory(coin=coin.upper(), points=points)

    return app


def _build_earn_rows(
    snap: dict[str, Any],
    *,
    category: str | None,
    coin: str | None,
) -> list[EarnProductRow]:
    """Flatten the snapshot's product universe into Earn-Explorer rows,
    joining each product to its coin's current funding from `earn_funding`
    (LM `BASE/QUOTE` matches on either leg). Pure function over the snapshot
    dict — no DB access."""
    products: dict[str, Any] = snap.get("products") or {}
    funding: dict[str, Any] = snap.get("earn_funding") or {}
    coin_filter = coin.upper() if coin else None
    rows: list[EarnProductRow] = []
    for cat, items in products.items():
        if category and cat != category:
            continue
        if not isinstance(items, list):
            continue
        for p in items:
            pcoin = str(p.get("coin", ""))
            if coin_filter and coin_filter not in pcoin.upper():
                continue
            fund = None
            for leg in [pcoin.upper(), *pcoin.upper().split("/")]:
                fund = funding.get(leg.strip())
                if fund is not None:
                    break
            rate = _to_float(fund.get("funding_rate")) if fund else None
            interval = (
                _to_float(fund.get("funding_interval_hours")) if fund else None
            )
            apr_points = p.get("apr_history_points")
            rows.append(
                EarnProductRow(
                    category=cat,
                    product_id=str(p.get("product_id", "")),
                    coin=pcoin,
                    effective_apr_pct=(_to_float(p.get("effective_apr")) or 0.0)
                    * 100.0,
                    apr_source=str(p.get("apr_source", "")),
                    apr_history_pct=(
                        [(_to_float(x) or 0.0) * 100.0 for x in apr_points]
                        if isinstance(apr_points, list) and apr_points
                        else None
                    ),
                    funding_rate=rate,
                    funding_annual_pct=_annual_funding_pct(rate, interval),
                    mark_price=_to_float(fund.get("mark_price")) if fund else None,
                )
            )
    return rows


def _get_pool(request: Request) -> asyncpg.Pool:
    pool = request.app.state.pool
    if not isinstance(pool, asyncpg.Pool):
        raise HTTPException(
            status_code=503,
            detail="database pool not available; check DATABASE_URL",
        )
    return pool


# Module-level singleton for `uvicorn agent.api.server:app`. Tests
# build their own via `build_app(pool=...)`.
app = build_app()
