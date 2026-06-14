"""Tests for the read-only FastAPI app (`data-store.5`).

Uses `httpx.AsyncClient` against the FastAPI app with an injected
test pool, so each test gets a fresh DB + a real Postgres backend
through testcontainers. Lifespan startup sees `app.state.pool`
pre-populated and skips opening its own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager

from agent.api.server import build_app
from agent.sandbox.store.pool import open_pool
from agent.sandbox.store.schema import apply_migrations
from agent.sandbox.store.writer import record_cycle, record_event

# ───────────────── helpers ────────────────────────────────────────────


def _outcome(
    ts_stem: str = "20260529T160211Z",
    *,
    result: str = "executed",
    wake_reason: str = "heartbeat",
    confidence: float = 0.7,
) -> dict:
    return {
        "started_at": f"2026-05-29T{ts_stem[9:11]}:{ts_stem[11:13]}:{ts_stem[13:15]}+00:00",
        "finished_at": "2026-05-29T16:02:14.500000+00:00",
        "snapshot_filename": f"{ts_stem}.json",
        "decision_filename": f"{ts_stem}.json",
        "result": result,
        "wake_reason": wake_reason,
        "confidence": confidence,
        "expected_apr_pct": 4.5,
        "actions_planned": 1,
        "actions_executed": 1,
        "actions": [
            {
                "kind": "subscribe_earn",
                "category": "FlexibleSaving",
                "product_id": "1131",
                "coin": "USD1",
                "amount": "50",
                "status": "ok",
                "error": None,
            }
        ],
    }


def _snapshot() -> dict:
    return {
        "captured_at": "2026-05-29T16:02:11+00:00",
        "wallet": {"total_equity_usd": "200"},
        "earn_positions": [
            {"productId": "1131", "coin": "USD1", "amount": "100",
             "category": "FlexibleSaving"},
        ],
        "lm_positions": [],
        "alpha_positions": [],
        "perp_positions": [],
        "products": {},
    }


def _earn_snapshot(
    ts: str = "2026-05-29T16:02:11+00:00",
    *,
    btc_funding: str = "0.0001",
) -> dict:
    """Snapshot with a product universe + earn_funding for the
    Earn-Explorer endpoints."""
    return {
        "captured_at": ts,
        "wallet": {"total_equity_usd": "200"},
        "earn_positions": [],
        "lm_positions": [],
        "alpha_positions": [],
        "perp_positions": [],
        "products": {
            "FlexibleSaving": [
                {
                    "category": "FlexibleSaving",
                    "product_id": "F1",
                    "coin": "BTC",
                    "effective_apr": "0.05",
                    "effective_apr_gross": "0.05",
                    "effective_apr_net_hedge": "0.05",
                    "apr_source": "apr_history",
                    "apr_history_points": ["0.04", "0.05", "0.06"],
                    "price_change_7d_pct": "3.0",
                    "price_change_30d_pct": "8.0",
                },
                {
                    "category": "FlexibleSaving",
                    "product_id": "F2",
                    "coin": "USDC",
                    "effective_apr": "0.03",
                    "apr_source": "estimate_apr",
                },
            ],
            "LiquidityMining": [
                {
                    "category": "LiquidityMining",
                    "product_id": "L1",
                    "coin": "BTC/USDT",
                    "effective_apr": "0.12",
                    "apr_source": "apy_e8",
                },
            ],
        },
        "earn_funding": {
            "BTC": {
                "symbol": "BTCUSDT",
                "funding_rate": btc_funding,
                "funding_interval_hours": "8",
                "mark_price": "68000",
                "source": "tickers",
            },
        },
        "perp_market": {
            "BTC": {
                "symbol": "BTCUSDT",
                "funding_rate_8h": btc_funding,
                "funding_rate_7d_avg": "0.00008",
                "funding_interval_hours": "8",
                "mark_price": "68000",
            },
        },
    }


def _decision() -> dict:
    return {
        "thesis": "test",
        "venues": [{"venue_id": "cash_usdc", "weight": 1.0}],
        "hedges": [],
        "confidence": 0.7,
        "risk_flags": [],
        "notes": [],
        "expected_blended_apr_pct": 4.5,
    }


def _event(
    kind: str = "price_drift",
    severity: str = "P0",
    ts: str = "2026-05-29T16:01:00+00:00",
) -> dict:
    return {
        "ts": ts,
        "kind": kind,
        "severity": severity,
        "position_id": "perp:TONUSDT",
        "coin": "TON",
        "baseline": {},
        "current": {},
        "threshold": {},
        "message": f"{kind} fired",
    }


@pytest_asyncio.fixture
async def api_client(fresh_db_dsn: str) -> AsyncIterator[httpx.AsyncClient]:
    """FastAPI app wired to a fresh Postgres DB (migrations applied),
    served via `httpx.AsyncClient` with ASGI transport. Lifespan runs
    via `asgi-lifespan` so startup/shutdown fire properly."""
    async with open_pool(fresh_db_dsn) as pool:
        await apply_migrations(pool)
        app = build_app(pool=pool)
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                # Stash the pool so tests can seed data directly.
                client._test_pool = pool  # type: ignore[attr-defined]
                yield client


# ───────────────── tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_reports_pool_open(api_client: httpx.AsyncClient) -> None:
    resp = await api_client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "pool": "open"}


@pytest.mark.asyncio
async def test_cycles_endpoint_returns_seeded_data(
    api_client: httpx.AsyncClient,
) -> None:
    pool = api_client._test_pool  # type: ignore[attr-defined]
    # Seed two cycles
    await record_cycle(
        pool,
        outcome=_outcome("20260529T160211Z"),
        raw_snapshot=_snapshot(),
        raw_decision=_decision(),
    )
    await record_cycle(
        pool,
        outcome=_outcome("20260529T170000Z", result="ok"),
        raw_snapshot=_snapshot(),
        raw_decision=_decision(),
    )
    resp = await api_client.get("/cycles", params={"limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # DESC by started_at → 17:00 first
    assert body[0]["cycle_ts"].startswith("2026-05-29T17:00")
    assert body[1]["cycle_ts"].startswith("2026-05-29T16:02")
    assert body[0]["result"] in ("executed", "ok")
    # Payloads NOT included in the list view
    assert "snapshot" not in body[0]
    assert "decision" not in body[0]


@pytest.mark.asyncio
async def test_cycles_endpoint_wake_reason_filter(
    api_client: httpx.AsyncClient,
) -> None:
    pool = api_client._test_pool  # type: ignore[attr-defined]
    await record_cycle(
        pool, outcome=_outcome("20260529T160000Z"),
        raw_snapshot=_snapshot(), raw_decision=_decision(),
    )
    await record_cycle(
        pool,
        outcome=_outcome("20260529T170000Z", wake_reason="event:price_drift"),
        raw_snapshot=_snapshot(), raw_decision=_decision(),
    )
    resp = await api_client.get(
        "/cycles", params={"wake_reason_prefix": "event:"}
    )
    body = resp.json()
    assert len(body) == 1
    assert body[0]["wake_reason"] == "event:price_drift"


@pytest.mark.asyncio
async def test_cycle_detail_returns_snapshot_decision_positions(
    api_client: httpx.AsyncClient,
) -> None:
    pool = api_client._test_pool  # type: ignore[attr-defined]
    await record_cycle(
        pool, outcome=_outcome("20260529T160211Z"),
        raw_snapshot=_snapshot(), raw_decision=_decision(),
    )
    resp = await api_client.get("/cycles/2026-05-29T16:02:11+00:00")
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot"]["wallet"]["total_equity_usd"] == "200"
    assert body["decision"]["confidence"] == 0.7
    assert len(body["positions"]) == 1
    assert body["positions"][0]["coin"] == "USD1"
    assert len(body["executions"]) == 1


@pytest.mark.asyncio
async def test_cycle_detail_404_for_unknown_ts(
    api_client: httpx.AsyncClient,
) -> None:
    resp = await api_client.get("/cycles/2099-01-01T00:00:00+00:00")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_events_endpoint_lists_and_filters(
    api_client: httpx.AsyncClient,
) -> None:
    pool = api_client._test_pool  # type: ignore[attr-defined]
    await record_event(pool, _event(kind="price_drift", severity="P0"))
    await record_event(
        pool,
        _event(kind="funding_flip", severity="P0",
               ts="2026-05-29T16:01:30+00:00"),
    )
    await record_event(
        pool,
        _event(kind="peg_drift", severity="P1",
               ts="2026-05-29T16:01:45+00:00"),
    )
    # All events
    resp = await api_client.get("/events")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    # Filter by kind
    resp = await api_client.get("/events", params={"kind": "price_drift"})
    body = resp.json()
    assert len(body) == 1 and body[0]["kind"] == "price_drift"
    # Filter by severity
    resp = await api_client.get("/events", params={"severity": "P1"})
    body = resp.json()
    assert len(body) == 1 and body[0]["severity"] == "P1"


@pytest.mark.asyncio
async def test_portfolio_current_returns_latest_positions(
    api_client: httpx.AsyncClient,
) -> None:
    pool = api_client._test_pool  # type: ignore[attr-defined]
    await record_cycle(
        pool, outcome=_outcome("20260529T160000Z"),
        raw_snapshot=_snapshot(), raw_decision=_decision(),
    )
    resp = await api_client.get("/portfolio/current")
    assert resp.status_code == 200
    body = resp.json()
    assert body["wallet"]["total_equity_usd"] == "200"
    assert len(body["positions"]) == 1
    assert body["positions"][0]["coin"] == "USD1"


@pytest.mark.asyncio
async def test_portfolio_current_404_when_no_cycles(
    api_client: httpx.AsyncClient,
) -> None:
    resp = await api_client.get("/portfolio/current")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_healthz_reports_missing_pool_when_no_db(
    fresh_db_dsn: str,
) -> None:
    """An app started without an injected pool AND without
    DATABASE_URL should still serve /healthz (degraded mode), but
    report pool=missing — the read endpoints would 503."""
    import os

    saved = os.environ.pop("DATABASE_URL", None)
    try:
        app = build_app()  # no pool injected
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/healthz")
                assert resp.status_code == 200
                assert resp.json()["pool"] == "missing"
                resp2 = await client.get("/cycles")
                assert resp2.status_code == 503
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved


@pytest.mark.asyncio
async def test_earn_products_endpoint_shapes_apr_and_funding(
    api_client: httpx.AsyncClient,
) -> None:
    pool = api_client._test_pool  # type: ignore[attr-defined]
    await record_cycle(
        pool,
        outcome=_outcome("20260529T160211Z"),
        raw_snapshot=_earn_snapshot(),
        raw_decision=_decision(),
    )
    resp = await api_client.get("/earn/products")
    assert resp.status_code == 200
    body = resp.json()
    assert body["captured_at"].startswith("2026-05-29T16:02:11")
    by_id = {p["product_id"]: p for p in body["products"]}
    # APR fractional → pct; Bybit daily history carried through.
    assert by_id["F1"]["effective_apr_pct"] == pytest.approx(5.0)
    assert by_id["F1"]["apr_history_pct"] == pytest.approx([4.0, 5.0, 6.0])
    # BTC funding annualized: 0.0001 × (8760/8) × 100 = 10.95%.
    assert by_id["F1"]["funding_annual_pct"] == pytest.approx(10.95)
    # LM "BTC/USDT" matches BTC funding on the base leg.
    assert by_id["L1"]["funding_annual_pct"] == pytest.approx(10.95)
    # Stablecoin product: no funding, no history.
    assert by_id["F2"]["funding_rate"] is None
    assert by_id["F2"]["apr_history_pct"] is None
    # Sorted by quality_score desc (None last), monotonic non-increasing.
    scores = [p["quality_score"] for p in body["products"]]
    present = [s for s in scores if s is not None]
    assert present == sorted(present, reverse=True)


@pytest.mark.asyncio
async def test_earn_products_quality_fields(
    api_client: httpx.AsyncClient,
) -> None:
    pool = api_client._test_pool  # type: ignore[attr-defined]
    await record_cycle(
        pool,
        outcome=_outcome("20260529T160211Z"),
        raw_snapshot=_earn_snapshot(),
        raw_decision=_decision(),
    )
    resp = await api_client.get("/earn/products")
    assert resp.status_code == 200
    by_id = {p["product_id"]: p for p in resp.json()["products"]}
    f1, f2 = by_id["F1"], by_id["F2"]
    # Every row carries a bounded quality score.
    for p in by_id.values():
        assert 0.0 <= p["quality_score"] <= 100.0
    # F1 (BTC, apr_history) — net APR + APR stability + weekly funding present.
    assert f1["net_apr_pct"] == pytest.approx(5.0)
    assert f1["avg_apr_7d_pct"] == pytest.approx(5.0)
    assert f1["apr_stability"] is not None
    assert f1["is_stable"] is False
    # funding_7d uses the accurate perp 21-period avg: 0.00008 × 8760/8 × 100.
    assert f1["funding_7d_annual_pct"] == pytest.approx(0.00008 * (8760 / 8) * 100)
    # F2 (USDC) flagged stable, high stability.
    assert f2["is_stable"] is True
    assert f2["stability_score"] is not None and f2["stability_score"] >= 90.0
    # Profit horizons: F1 has 3 daily APR points → 1d realized, 7d/30d
    # projected (window shorter than horizon), each flagged via basis.
    assert f1["profit_1d"]["basis"] == "realized"
    assert f1["profit_7d"]["basis"] == "projected"
    assert f1["profit_30d"]["basis"] == "projected"
    assert f1["profit_1d"]["total_pct"] is not None


@pytest.mark.asyncio
async def test_funding_7d_averages_reader(
    api_client: httpx.AsyncClient,
) -> None:
    from agent.sandbox.store import funding_7d_averages

    pool = api_client._test_pool  # type: ignore[attr-defined]
    await record_cycle(
        pool,
        outcome=_outcome("20260529T160000Z"),
        raw_snapshot=_earn_snapshot("2026-05-29T16:00:00+00:00", btc_funding="0.0001"),
        raw_decision=_decision(),
    )
    await record_cycle(
        pool,
        outcome=_outcome("20260529T170000Z"),
        raw_snapshot=_earn_snapshot("2026-05-29T17:00:00+00:00", btc_funding="0.0003"),
        raw_decision=_decision(),
    )
    # Wide window so the fixed-date fixtures fall inside it regardless of the
    # test host's wall clock (the prod endpoint uses the default 7 days).
    avgs = await funding_7d_averages(pool, days=100_000)
    # Only earn_funding coins; BTC averaged across the two cycles.
    assert set(avgs) == {"BTC"}
    assert avgs["BTC"] == pytest.approx(0.0002)


@pytest.mark.asyncio
async def test_earn_products_404_without_snapshot(
    api_client: httpx.AsyncClient,
) -> None:
    resp = await api_client.get("/earn/products")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_earn_funding_history_returns_cross_cycle_series(
    api_client: httpx.AsyncClient,
) -> None:
    pool = api_client._test_pool  # type: ignore[attr-defined]
    await record_cycle(
        pool,
        outcome=_outcome("20260529T160000Z"),
        raw_snapshot=_earn_snapshot("2026-05-29T16:00:00+00:00", btc_funding="0.0001"),
        raw_decision=_decision(),
    )
    await record_cycle(
        pool,
        outcome=_outcome("20260529T170000Z"),
        raw_snapshot=_earn_snapshot("2026-05-29T17:00:00+00:00", btc_funding="0.0002"),
        raw_decision=_decision(),
    )
    resp = await api_client.get("/earn/funding-history", params={"coin": "btc"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["coin"] == "BTC"
    # Oldest → newest, both cycles present.
    assert [p["funding_rate"] for p in body["points"]] == pytest.approx([0.0001, 0.0002])
    assert body["points"][0]["funding_annual_pct"] == pytest.approx(10.95)


@pytest.mark.asyncio
async def test_earn_funding_history_empty_for_unknown_coin(
    api_client: httpx.AsyncClient,
) -> None:
    pool = api_client._test_pool  # type: ignore[attr-defined]
    await record_cycle(
        pool,
        outcome=_outcome("20260529T160000Z"),
        raw_snapshot=_earn_snapshot(),
        raw_decision=_decision(),
    )
    resp = await api_client.get("/earn/funding-history", params={"coin": "DOGE"})
    assert resp.status_code == 200
    assert resp.json() == {"coin": "DOGE", "points": []}


assert datetime is not None  # silence ruff on the date helpers above
assert UTC is not None
