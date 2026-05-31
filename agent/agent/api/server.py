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
    get_current_portfolio,
    get_cycle,
    list_cycles,
    list_events,
    open_pool,
)

log = logging.getLogger(__name__)


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

    return app


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
