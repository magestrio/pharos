"""Unit tests for the Earn-Explorer coin-quality scoring (`_coin_quality`).

Pure function, no DB — asserts score BANDS (not exact floats) so the
heuristic weights stay tunable without breaking the suite.
"""

from __future__ import annotations

import pytest

from agent.api.server import _coin_profit, _coin_quality, _is_stable


def _q(**over):
    base = dict(
        coin="ETH",
        apr_source="apr_history",
        effective_apr=0.10,
        effective_apr_gross=0.10,
        effective_apr_net_hedge=None,
        apr_history_pts=[0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10],
        price_change_7d_pct=2.0,
        price_change_30d_pct=5.0,
        funding_rate=0.0001,
        funding_interval_hours=8.0,
        funding_rate_7d_avg=None,
        funding_7d_avg_cross=None,
    )
    base.update(over)
    return _coin_quality(**base)


def test_is_stable_single_and_lm_legs() -> None:
    assert _is_stable("USDC")
    assert _is_stable("USDC/USDT")  # both legs stable
    assert not _is_stable("ETH")
    assert not _is_stable("ETH/USDT")  # one leg non-stable
    assert not _is_stable("")


def test_steady_stablecoin_high_stability_mid_quality() -> None:
    q = _coin_quality(
        coin="USDC",
        apr_source="apr_history",
        effective_apr=0.08,
        effective_apr_gross=None,
        effective_apr_net_hedge=None,
        apr_history_pts=[0.08, 0.081, 0.079, 0.08, 0.08, 0.082, 0.078],
        price_change_7d_pct=None,
        price_change_30d_pct=None,
        funding_rate=None,
        funding_interval_hours=None,
        funding_rate_7d_avg=None,
        funding_7d_avg_cross=None,
    )
    assert q["is_stable"] is True
    assert q["stability_score"] >= 95.0
    assert 65.0 <= q["quality_score"] <= 78.0
    assert q["net_apr_pct"] == 8.0  # gross for stable
    assert q["funding_7d_annual_pct"] is None


def test_steady_high_yield_alt_top_band() -> None:
    q = _coin_quality(
        coin="ETH",
        apr_source="apr_history",
        effective_apr=0.25,
        effective_apr_gross=0.27,
        effective_apr_net_hedge=0.25,
        apr_history_pts=[0.26, 0.27, 0.25, 0.27, 0.26, 0.27, 0.27],
        price_change_7d_pct=3.0,
        price_change_30d_pct=8.0,
        funding_rate=0.0001,
        funding_interval_hours=8.0,
        funding_rate_7d_avg=0.00008,
        funding_7d_avg_cross=None,
    )
    assert 80.0 <= q["quality_score"] <= 92.0
    assert q["net_apr_pct"] == 25.0
    # funding 7d comes from the accurate perp avg, annualized.
    assert q["funding_7d_annual_pct"] is not None and q["funding_7d_annual_pct"] > 0


def test_mirage_high_gross_negative_net_collapses() -> None:
    q = _coin_quality(
        coin="SCAM",
        apr_source="estimate_apr",
        effective_apr=-0.05,
        effective_apr_gross=6.0,
        effective_apr_net_hedge=-0.05,
        apr_history_pts=None,
        price_change_7d_pct=-60.0,
        price_change_30d_pct=-80.0,
        funding_rate=-0.01,
        funding_interval_hours=8.0,
        funding_rate_7d_avg=None,
        funding_7d_avg_cross=None,
    )
    assert q["net_apr_pct"] < 0
    assert q["quality_score"] < 20.0
    assert q["avg_apr_7d_pct"] == 600.0  # gross fallback when no series
    assert q["price_stability"] == 0.0


def test_dualasset_no_history_stability_from_price_only() -> None:
    q = _coin_quality(
        coin="BTC",
        apr_source="quote_dual_offer",
        effective_apr=0.12,
        effective_apr_gross=None,
        effective_apr_net_hedge=0.12,
        apr_history_pts=None,
        price_change_7d_pct=5.0,
        price_change_30d_pct=10.0,
        funding_rate=0.0001,
        funding_interval_hours=8.0,
        funding_rate_7d_avg=None,
        funding_7d_avg_cross=None,
    )
    assert q["apr_stability"] is None
    # stability == price_stability only: 1 - 5/25 = 0.8 → 80
    assert abs(q["stability_score"] - 80.0) < 1e-6


def test_no_signal_nonstable_finite_quality_neutral_stability() -> None:
    q = _coin_quality(
        coin="XYZ",
        apr_source="apy_e8",
        effective_apr=0.12,
        effective_apr_gross=0.12,
        effective_apr_net_hedge=None,
        apr_history_pts=None,
        price_change_7d_pct=None,
        price_change_30d_pct=None,
        funding_rate=None,
        funding_interval_hours=None,
        funding_rate_7d_avg=None,
        funding_7d_avg_cross=None,
    )
    assert q["stability_score"] is None
    assert 0.0 <= q["quality_score"] <= 100.0  # neutral 0.5 stability used


def test_zero_mean_moving_apr_no_div_by_zero() -> None:
    q = _q(apr_history_pts=[0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.0])
    assert q["apr_stability"] is not None
    assert q["apr_stability"] < 0.1  # zero-mean-moving → cv=10 → ~0


def test_single_point_apr_stability_none() -> None:
    q = _q(apr_history_pts=[0.1])
    assert q["apr_stability"] is None
    assert q["avg_apr_7d_pct"] == 10.0


def test_funding_7d_precedence_cross_cycle_then_current() -> None:
    # No perp avg → cross-cycle avg is used.
    q = _q(funding_rate_7d_avg=None, funding_7d_avg_cross=0.0002, funding_rate=0.0009)
    cross = q["funding_7d_annual_pct"]
    # Current-only fallback when neither avg present.
    q2 = _q(funding_rate_7d_avg=None, funding_7d_avg_cross=None, funding_rate=0.0009)
    assert cross is not None and q2["funding_7d_annual_pct"] is not None
    assert cross < q2["funding_7d_annual_pct"]  # 0.0002 avg < 0.0009 current


def test_quality_in_range_and_monotonic_in_net() -> None:
    low = _q(effective_apr=0.05, effective_apr_net_hedge=0.05)
    high = _q(effective_apr=0.30, effective_apr_net_hedge=0.30)
    for q in (low, high):
        assert 0.0 <= q["quality_score"] <= 100.0
    assert high["quality_score"] >= low["quality_score"]


def test_stability_monotonic_decreasing_in_volatility() -> None:
    calm = _q(price_change_7d_pct=2.0)
    rough = _q(price_change_7d_pct=20.0)
    assert calm["stability_score"] >= rough["stability_score"]


def test_high_volatility_penalty_applied() -> None:
    # |7d| >= 40 halves quality vs an otherwise-identical calm coin.
    calm = _q(price_change_7d_pct=2.0)
    vol = _q(price_change_7d_pct=45.0)
    assert vol["quality_score"] < calm["quality_score"]


# ── profit horizons ────────────────────────────────────────────────────────


def test_profit_1d_7d_realized_30d_projected() -> None:
    p = _coin_profit(
        apr_history_pts=[0.05] * 7,
        effective_apr=0.05,
        effective_apr_gross=0.05,
        is_stable=False,
        funding_7d_annual_pct=7.3,  # → +0.02%/day funding
    )
    # 1d earn ≈ 5%/365, funding ≈ 7.3%/365; both realized.
    assert p["profit_1d"].basis == "realized"
    assert p["profit_1d"].earn_pct == pytest.approx(0.05 / 365 * 100)
    assert p["profit_1d"].funding_pct == pytest.approx(7.3 / 365)
    assert p["profit_1d"].total_pct == pytest.approx(
        p["profit_1d"].earn_pct + p["profit_1d"].funding_pct
    )
    # 7d realized over the full window.
    assert p["profit_7d"].basis == "realized"
    assert p["profit_7d"].earn_pct == pytest.approx(0.05 * 7 / 365 * 100)
    # 30d projected (only 7d of history) — explicitly flagged.
    assert p["profit_30d"].basis == "projected"
    assert "projected" in (p["profit_30d"].note or "")


def test_profit_stable_no_funding_component() -> None:
    p = _coin_profit(
        apr_history_pts=[0.08] * 7,
        effective_apr=0.08,
        effective_apr_gross=None,
        is_stable=True,
        funding_7d_annual_pct=None,
    )
    assert p["profit_1d"].funding_pct == 0.0
    assert p["profit_7d"].total_pct == pytest.approx(p["profit_7d"].earn_pct)


def test_profit_no_apr_history_projects_and_flags() -> None:
    p = _coin_profit(
        apr_history_pts=None,
        effective_apr=0.12,
        effective_apr_gross=None,
        is_stable=False,
        funding_7d_annual_pct=None,
    )
    assert p["profit_1d"].basis == "projected"
    assert "no daily history" in (p["profit_1d"].note or "")
    assert "funding history unavailable" in (p["profit_1d"].note or "")


def test_profit_no_data_unavailable() -> None:
    p = _coin_profit(
        apr_history_pts=None,
        effective_apr=None,
        effective_apr_gross=None,
        is_stable=False,
        funding_7d_annual_pct=None,
    )
    assert p["profit_1d"].basis == "unavailable"
    assert p["profit_1d"].total_pct is None


def test_profit_negative_funding_drags_total() -> None:
    pos = _coin_profit(
        apr_history_pts=[0.05] * 7, effective_apr=0.05, effective_apr_gross=0.05,
        is_stable=False, funding_7d_annual_pct=10.0,
    )
    neg = _coin_profit(
        apr_history_pts=[0.05] * 7, effective_apr=0.05, effective_apr_gross=0.05,
        is_stable=False, funding_7d_annual_pct=-10.0,
    )
    assert neg["profit_7d"].total_pct < pos["profit_7d"].total_pct
