from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from agent.scheduler import poller as poller_mod
from agent.scheduler.poller import _poll_once
from agent.scheduler.triggers import TriggerEvaluator
from agent.validate.risk_context import RiskContext
from agent.gather.models import PerpMarketData, PerpVenueData


T0 = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)


def _ev(**kw) -> TriggerEvaluator:
    return TriggerEvaluator(**kw)


# ─── funding ──────────────────────────────────────────────────────────────────

def test_funding_first_observation_always_fires():
    ev = _ev()
    out = ev.evaluate_funding("SOLUSDT", 0.0005, T0)  # 5 bps
    assert out.fire is True
    assert "first observation" in out.reason


def test_funding_no_fire_when_below_threshold():
    ev = _ev(funding_threshold_bps=50)
    ev.evaluate_funding("SOLUSDT", 0.0005, T0)            # seed @ 5bps
    out = ev.evaluate_funding("SOLUSDT", 0.0010, T0)      # 10bps → Δ=5bps
    assert out.fire is False


def test_funding_fires_when_at_or_above_threshold():
    ev = _ev(funding_threshold_bps=50)
    ev.evaluate_funding("SOLUSDT", 0.0001, T0)            # seed @ 1bps
    out = ev.evaluate_funding("SOLUSDT", 0.0051, T0)      # 51bps → Δ=50bps (boundary)
    assert out.fire is True
    assert "50.0bps" in out.reason


def test_funding_per_symbol_state_is_independent():
    ev = _ev()
    ev.evaluate_funding("SOLUSDT", 0.0001, T0)            # seed SOL
    out = ev.evaluate_funding("ETHUSDT", 0.0001, T0)      # ETH never seen → fires
    assert out.fire is True
    # Second SOL call with sub-threshold delta still doesn't fire
    out_sol = ev.evaluate_funding("SOLUSDT", 0.0002, T0)
    assert out_sol.fire is False


# ─── aave utilization ────────────────────────────────────────────────────────

def test_aave_util_first_observation_fires():
    ev = _ev()
    out = ev.evaluate_aave_util("aave_v3_usdc", 0.75, T0)
    assert out.fire is True


def test_aave_util_sub_threshold_does_not_fire():
    ev = _ev(aave_util_threshold=0.05)
    ev.evaluate_aave_util("aave_v3_usdc", 0.70, T0)
    out = ev.evaluate_aave_util("aave_v3_usdc", 0.74, T0)  # Δ=0.04
    assert out.fire is False


def test_aave_util_at_threshold_fires():
    ev = _ev(aave_util_threshold=0.05)
    ev.evaluate_aave_util("aave_v3_usdc", 0.70, T0)
    out = ev.evaluate_aave_util("aave_v3_usdc", 0.75, T0)
    assert out.fire is True


# ─── usdc peg (absolute) ─────────────────────────────────────────────────────

def test_peg_below_threshold_no_fire():
    ev = _ev(peg_threshold_bps=100)
    out = ev.evaluate_peg(50, T0)
    assert out.fire is False


def test_peg_at_or_above_threshold_fires():
    ev = _ev(peg_threshold_bps=100)
    out = ev.evaluate_peg(100, T0)
    assert out.fire is True


def test_peg_does_not_use_delta_semantics():
    """Even if peg stays at the same elevated level, every check fires
    until the deviation drops back. The signal IS the absolute number."""
    ev = _ev(peg_threshold_bps=100)
    a = ev.evaluate_peg(150, T0)
    b = ev.evaluate_peg(150, T0)
    assert a.fire is True
    assert b.fire is True


# ─── cooldown ─────────────────────────────────────────────────────────────────

def test_cooldown_suppresses_all_signals():
    ev = _ev(cooldown_minutes=30)
    ev.mark_decision_taken(T0)
    inside = T0 + timedelta(minutes=15)
    assert ev.evaluate_funding("SOLUSDT", 0.05, inside).fire is False
    assert ev.evaluate_aave_util("aave_v3_usdc", 0.99, inside).fire is False
    assert ev.evaluate_peg(500, inside).fire is False


def test_cooldown_expires_after_window():
    ev = _ev(cooldown_minutes=30)
    ev.mark_decision_taken(T0)
    outside = T0 + timedelta(minutes=31)
    # Fresh state — first observation fires.
    assert ev.evaluate_funding("SOLUSDT", 0.0001, outside).fire is True


def test_cooldown_does_not_corrupt_baseline_during_window():
    """While in cooldown, evaluate returns 'cooldown' without updating
    last-seen state. After cooldown lifts, the next observation is
    treated as 'first observation' if no prior real observation existed."""
    ev = _ev(cooldown_minutes=30)
    # Seed baseline with a real evaluation before cooldown is armed
    ev.evaluate_funding("SOLUSDT", 0.0001, T0 - timedelta(minutes=1))
    ev.mark_decision_taken(T0)
    inside = T0 + timedelta(minutes=10)
    out = ev.evaluate_funding("SOLUSDT", 0.0500, inside)
    assert out.fire is False and out.reason == "cooldown"
    # baseline retained
    assert ev.last_funding_bps_per_symbol["SOLUSDT"] == pytest.approx(1.0)


# ─── poller integration ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_poller_bundles_all_fired_reasons_into_one_call(monkeypatch):
    risk = RiskContext(
        bybit_attestor_lag_minutes=10,
        usdc_peg_deviation_bps=150,                # fires (absolute > 100)
        aave_v3_usdc_utilization=0.80,             # first observation → fires
        aave_v3_weth_utilization=None,             # skipped (None)
    )
    perp = PerpMarketData(
        venues=[PerpVenueData(symbol="SOLUSDT", funding_rate_8h=0.0001)],  # first → fires
        is_available=True,
        timestamp=T0,
    )
    monkeypatch.setattr(poller_mod, "get_risk_context", AsyncMock(return_value=risk))
    monkeypatch.setattr(poller_mod, "get_perp_market_data", AsyncMock(return_value=perp))

    on_trigger = AsyncMock()
    await _poll_once(TriggerEvaluator(), on_trigger)

    assert on_trigger.call_count == 1
    reason = on_trigger.call_args.args[0]
    assert "usdc_peg" in reason
    assert "aave_util[aave_v3_usdc]" in reason
    assert "funding[SOLUSDT]" in reason


@pytest.mark.asyncio
async def test_poller_does_not_trigger_when_no_signals_fire(monkeypatch):
    """All values within thresholds AND already-seen → no trigger."""
    evaluator = TriggerEvaluator()
    # Pre-seed last-seen baselines so the first-observation rule doesn't fire
    evaluator.evaluate_aave_util("aave_v3_usdc", 0.80, T0)
    evaluator.evaluate_funding("SOLUSDT", 0.0001, T0)

    risk = RiskContext(
        usdc_peg_deviation_bps=20,
        aave_v3_usdc_utilization=0.81,             # Δ=1% → no fire
        aave_v3_weth_utilization=None,
    )
    perp = PerpMarketData(
        venues=[PerpVenueData(symbol="SOLUSDT", funding_rate_8h=0.00011)],  # Δ=0.1bps
        is_available=True,
        timestamp=T0,
    )
    monkeypatch.setattr(poller_mod, "get_risk_context", AsyncMock(return_value=risk))
    monkeypatch.setattr(poller_mod, "get_perp_market_data", AsyncMock(return_value=perp))

    on_trigger = AsyncMock()
    await _poll_once(evaluator, on_trigger)

    on_trigger.assert_not_called()


@pytest.mark.asyncio
async def test_poller_skips_perp_when_unavailable(monkeypatch):
    """is_available=False on perp snapshot → no funding evaluation."""
    risk = RiskContext()  # all None
    perp = PerpMarketData(venues=[], is_available=False, timestamp=T0)
    monkeypatch.setattr(poller_mod, "get_risk_context", AsyncMock(return_value=risk))
    monkeypatch.setattr(poller_mod, "get_perp_market_data", AsyncMock(return_value=perp))

    on_trigger = AsyncMock()
    await _poll_once(TriggerEvaluator(), on_trigger)

    on_trigger.assert_not_called()
