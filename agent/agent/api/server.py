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
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agent.reason.fees import round_trip_fee_fraction
from agent.reason.quality import compute_stability, is_stable
from agent.sandbox.store import (
    funding_7d_averages,
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

# Earn-Explorer coin-quality scoring knobs (heuristic — tests assert bands,
# not exact values, so these stay tunable). See `_coin_quality`.
# Stability (APR steadiness + price calm) is computed by the shared
# `agent.reason.quality.compute_stability` so the agent ranker and this API
# can't drift; only the yield/source-confidence knobs live here.
_YIELD_KNEE = 20.0  # net-APR % where tanh credit hits ~0.76 (saturates mirage)
_MIRAGE_VOL = 40.0  # weekly move % above which quality is halved
# Confidence in the APR number by its source — discounts noisy/estimated rates.
_SOURCE_CONF = {
    "apr_history": 1.0,
    "measured_yield": 1.0,
    "aave_pool": 0.9,
    "estimate_apr": 0.7,
    "apy_e8": 0.7,
    "quote_dual_offer": 0.6,
    "quote_discount": 0.6,
    "momentum": 0.3,
    "missing": 0.0,
}


def _coin_quality(
    *,
    coin: str,
    apr_source: str,
    effective_apr: float | None,
    effective_apr_gross: float | None,
    effective_apr_net_hedge: float | None,
    apr_history_pts: list[float] | None,
    price_change_7d_pct: float | None,
    price_change_30d_pct: float | None,
    funding_rate: float | None,
    funding_interval_hours: float | None,
    funding_rate_7d_avg: float | None,
    funding_7d_avg_cross: float | None,
) -> dict[str, Any]:
    """The Earn-Explorer coin-quality model — single source of truth. Pure
    (primitives in, dict out) so it's trivially unit-testable. Returns every
    derived `EarnProductRow` field. See plan: stability blends APR steadiness
    + price calm; quality blends saturating net yield + stability + source
    confidence, with mirage penalties. All scores clamp to sane ranges."""
    stable = is_stable(coin)

    # avg APR over the week (gross), from the daily series when present.
    if apr_history_pts:
        avg_apr_7d_pct = (sum(apr_history_pts) / len(apr_history_pts)) * 100.0
    else:
        base = effective_apr_gross if effective_apr_gross is not None else effective_apr
        avg_apr_7d_pct = base * 100.0 if base is not None else None

    # Realizable yield — net of hedge for non-stables (already baked into
    # effective_apr by the agent), gross for stables. Never read gross here.
    net = (
        effective_apr_net_hedge
        if effective_apr_net_hedge is not None
        else effective_apr
    )
    net_apr_pct = net * 100.0 if net is not None else None

    # Stability (APR steadiness + price calm) — shared with the agent ranker.
    stab = compute_stability(
        coin=coin,
        apr_history_pts=apr_history_pts,
        price_change_7d_pct=price_change_7d_pct,
        price_change_30d_pct=price_change_30d_pct,
    )
    apr_stability = stab["apr_stability"]
    price_volatility_pct = stab["price_volatility_pct"]
    price_stability = stab["price_stability"]
    stability_score = stab["stability_score"]

    # Weekly funding (annualized): accurate 21-period avg → cross-cycle avg →
    # current. Display-only; funding is already inside net_apr for non-stables.
    if funding_rate_7d_avg is not None:
        funding_7d_annual_pct = _annual_funding_pct(
            funding_rate_7d_avg, funding_interval_hours
        )
    elif funding_7d_avg_cross is not None:
        funding_7d_annual_pct = _annual_funding_pct(
            funding_7d_avg_cross, funding_interval_hours
        )
    else:
        funding_7d_annual_pct = _annual_funding_pct(
            funding_rate, funding_interval_hours
        )

    # Composite quality: saturating yield + stability + source confidence.
    yield_score = math.tanh(max(net_apr_pct or 0.0, 0.0) / _YIELD_KNEE)
    stab_unit = (stability_score / 100.0) if stability_score is not None else 0.5
    conf = _SOURCE_CONF.get(apr_source, 0.5)
    quality = 100.0 * (0.45 * yield_score + 0.40 * stab_unit + 0.15 * conf)
    if net_apr_pct is not None and net_apr_pct < 0:
        quality *= 0.3
    if price_volatility_pct is not None and price_volatility_pct >= _MIRAGE_VOL:
        quality *= 0.5
    quality_score = max(0.0, min(100.0, quality))

    return {
        "is_stable": stable,
        "avg_apr_7d_pct": avg_apr_7d_pct,
        "net_apr_pct": net_apr_pct,
        "apr_stability": apr_stability,
        "price_volatility_pct": price_volatility_pct,
        "price_stability": price_stability,
        "stability_score": stability_score,
        "funding_7d_annual_pct": funding_7d_annual_pct,
        "quality_score": quality_score,
    }


def _profit_horizon(
    days: int,
    *,
    apr_history_pts: list[float] | None,
    base_apr_frac: float | None,
    funding_annual_pct: float | None,
    is_stable: bool,
) -> ProfitHorizon:
    """Return on notional over `days`, realized from the daily APR history
    where it reaches and projected (flagged) beyond it. Earn uses the GROSS
    daily APR series (so earn + funding doesn't double-count the hedge);
    funding accrues the best-available average rate over the window.

    `total_pct` is the GROSS yield (earn + funding) — what you make holding it.
    The round-trip Bybit fee is a one-time entry+exit cost, reported separately
    via `fee_pct` + `break_even_days` (hold past break-even to net positive)
    rather than buried in every horizon (which made tiny daily yields all read
    negative)."""
    note: str | None = None
    if apr_history_pts:
        n = len(apr_history_pts)
        window = min(days, n)
        # Daily return ≈ APR/365; sum the realized days in the window.
        earn_pct = sum(apr_history_pts[-window:]) / 365.0 * 100.0
        if window < days:
            avg = sum(apr_history_pts) / n
            earn_pct += avg * (days - window) / 365.0 * 100.0
            basis = "projected"
            note = f"{window}/{days}d from APR history, rest projected"
        else:
            basis = "realized"
    elif base_apr_frac is not None:
        earn_pct = base_apr_frac * 100.0 * days / 365.0
        basis = "projected"
        note = "projected from current APR (no daily history)"
    else:
        return ProfitHorizon(basis="unavailable", note="no APR data")

    if is_stable:
        funding_pct: float | None = 0.0
    elif funding_annual_pct is not None:
        funding_pct = funding_annual_pct * days / 365.0
    else:
        funding_pct = None
        note = (note + "; " if note else "") + "funding history unavailable"

    # Gross yield over the horizon (what you make holding the position).
    total_pct = earn_pct + (funding_pct or 0.0)

    # Round-trip Bybit fee is a one-time entry+exit cost — report it via the
    # break-even hold rather than netting it into a tiny daily yield.
    fee_pct = round_trip_fee_fraction(is_stable=is_stable) * 100.0
    per_day_yield = total_pct / days
    if fee_pct <= 0.0:
        break_even_days: float | None = 0.0
    elif per_day_yield > 1e-9:
        break_even_days = fee_pct / per_day_yield
    else:
        break_even_days = None  # yield ≤ 0 → fee never recouped (a real loss)

    return ProfitHorizon(
        earn_pct=earn_pct,
        funding_pct=funding_pct,
        fee_pct=fee_pct,
        break_even_days=break_even_days,
        total_pct=total_pct,
        basis=basis,
        note=note,
    )


def _coin_profit(
    *,
    apr_history_pts: list[float] | None,
    effective_apr: float | None,
    effective_apr_gross: float | None,
    is_stable: bool,
    funding_7d_annual_pct: float | None,
) -> dict[str, ProfitHorizon]:
    """Realized/projected profit over 1d / 7d / 30d. 1d & 7d are realized from
    Bybit's 7 daily APR points; 30d is projected (only 7d of history exists) —
    always flagged via `basis`/`note`."""
    base = effective_apr_gross if effective_apr_gross is not None else effective_apr
    return {
        f"profit_{label}": _profit_horizon(
            days,
            apr_history_pts=apr_history_pts,
            base_apr_frac=base,
            funding_annual_pct=funding_7d_annual_pct,
            is_stable=is_stable,
        )
        for label, days in (("1d", 1), ("7d", 7), ("30d", 30))
    }


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


class ProfitHorizon(BaseModel):
    """Realized/projected return on notional over a horizon, split into the
    Earn leg, the hedge funding leg, and the round-trip trading FEE paid to
    enter+exit once. `basis`: "realized" (entirely from history), "projected"
    (some/all extrapolated — see `note`), or "unavailable" (no data).
    `total_pct` is NET: earn + funding − fee."""

    earn_pct: float | None = None
    funding_pct: float | None = None
    # Round-trip Bybit fee (enter+exit). A ONE-TIME cost, NOT subtracted from
    # total_pct (the fee amortizes over the hold) — surfaced via break_even_days.
    fee_pct: float | None = None
    # Days to hold at this yield rate before the gross return covers the
    # round-trip fee. 0 for stables (no fee); None when the yield is ≤ 0 (the
    # position loses money regardless of fee, e.g. deeply negative funding).
    break_even_days: float | None = None
    total_pct: float | None = None  # GROSS yield over the horizon (earn + funding)
    basis: str = "unavailable"
    note: str | None = None


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
    # Coin-quality derived metrics (see `_coin_quality`).
    is_stable: bool = False
    avg_apr_7d_pct: float | None = None      # gross weekly mean APR
    net_apr_pct: float | None = None         # realizable (net of hedge)
    apr_stability: float | None = None       # 0..1 (APR steadiness)
    price_volatility_pct: float | None = None  # |7d move|
    price_stability: float | None = None     # 0..1
    stability_score: float | None = None     # 0..100 (combined)
    funding_7d_annual_pct: float | None = None
    quality_score: float | None = None       # 0..100 (composite rank key)
    # Realized/projected profit on notional (earn + funding) by horizon.
    profit_1d: ProfitHorizon | None = None
    profit_7d: ProfitHorizon | None = None
    profit_30d: ProfitHorizon | None = None


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
        funding_7d = await funding_7d_averages(pool)
        rows = _build_earn_rows(
            snap, category=category, coin=coin, funding_7d=funding_7d
        )
        # Rank by quality (best earnable coins first); None scores last.
        rows.sort(
            key=lambda r: (r.quality_score is None, -(r.quality_score or 0.0))
        )
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
    funding_7d: dict[str, float] | None = None,
) -> list[EarnProductRow]:
    """Flatten the snapshot's product universe into Earn-Explorer rows,
    joining each product to its coin's current funding from `earn_funding`
    (LM `BASE/QUOTE` matches on either leg) and computing the coin-quality
    metrics via `_coin_quality`. `funding_7d` is the cross-cycle per-coin
    funding average (from `funding_7d_averages`); empty/None when unavailable.
    Pure function over the snapshot dict — no DB access."""
    products: dict[str, Any] = snap.get("products") or {}
    funding: dict[str, Any] = snap.get("earn_funding") or {}
    perp_market: dict[str, Any] = snap.get("perp_market") or {}
    funding_7d = funding_7d or {}
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
            legs = [pcoin.upper(), *(leg.strip() for leg in pcoin.upper().split("/"))]
            fund = next((funding.get(leg) for leg in legs if funding.get(leg)), None)
            perp = next(
                (perp_market.get(leg) for leg in legs if perp_market.get(leg)), None
            )
            cross = next(
                (funding_7d.get(leg) for leg in legs if leg in funding_7d), None
            )
            rate = _to_float(fund.get("funding_rate")) if fund else None
            interval = (
                _to_float(fund.get("funding_interval_hours")) if fund else None
            )
            apr_points = p.get("apr_history_points")
            apr_history_pct = (
                [(_to_float(x) or 0.0) * 100.0 for x in apr_points]
                if isinstance(apr_points, list) and apr_points
                else None
            )
            apr_pts_frac = (
                [_to_float(x) for x in apr_points]
                if isinstance(apr_points, list) and apr_points
                else None
            )
            quality = _coin_quality(
                coin=pcoin,
                apr_source=str(p.get("apr_source", "")),
                effective_apr=_to_float(p.get("effective_apr")),
                effective_apr_gross=_to_float(p.get("effective_apr_gross")),
                effective_apr_net_hedge=_to_float(p.get("effective_apr_net_hedge")),
                apr_history_pts=[x for x in (apr_pts_frac or []) if x is not None]
                or None,
                price_change_7d_pct=_to_float(p.get("price_change_7d_pct")),
                price_change_30d_pct=_to_float(p.get("price_change_30d_pct")),
                funding_rate=rate,
                funding_interval_hours=interval,
                funding_rate_7d_avg=(
                    _to_float(perp.get("funding_rate_7d_avg")) if perp else None
                ),
                funding_7d_avg_cross=cross,
            )
            profit = _coin_profit(
                apr_history_pts=[x for x in (apr_pts_frac or []) if x is not None]
                or None,
                effective_apr=_to_float(p.get("effective_apr")),
                effective_apr_gross=_to_float(p.get("effective_apr_gross")),
                is_stable=bool(quality["is_stable"]),
                funding_7d_annual_pct=quality["funding_7d_annual_pct"],
            )
            rows.append(
                EarnProductRow(
                    category=cat,
                    product_id=str(p.get("product_id", "")),
                    coin=pcoin,
                    effective_apr_pct=(_to_float(p.get("effective_apr")) or 0.0)
                    * 100.0,
                    apr_source=str(p.get("apr_source", "")),
                    apr_history_pct=apr_history_pct,
                    funding_rate=rate,
                    funding_annual_pct=_annual_funding_pct(rate, interval),
                    mark_price=_to_float(fund.get("mark_price")) if fund else None,
                    **quality,
                    **profit,
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
