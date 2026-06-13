"""Tests for the honest realized-APR reputation (`agent.sandbox.reputation`).

Covers:
  • `compute_realized_apr_bps`: positive / negative growth annualization,
    baseline = earliest positive sample, and every "not enough signal"
    guard (too few points, non-positive baseline, zero/short window).
  • `should_push` / `record_push` throttle round-trip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from agent.sandbox.reputation import (
    PUSH_INTERVAL,
    ReputationScore,
    compute_realized_apr_bps,
    record_push,
    should_push,
)

_T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def _series(*pairs: tuple[float, float]) -> list[tuple[datetime, Decimal]]:
    """`(days_from_T0, equity)` → history entries."""
    return [(_T0 + timedelta(days=d), Decimal(str(eq))) for d, eq in pairs]


def test_positive_growth_annualizes() -> None:
    # +10% over 30 days (one slow step, gap >> FLOW_MAX_GAP → real, not a
    # flow) → simple-annualized ~121.7% → ~12166 bps.
    score = compute_realized_apr_bps(_series((0, 100.0), (30, 110.0)))
    assert score is not None
    assert score.apr_bps == round(0.10 * 365 / 30 * 10_000)
    assert score.baseline_equity == Decimal("100.0")
    assert score.current_equity == Decimal("110.0")
    assert score.n_points == 2
    assert score.n_flows == 0


def test_negative_growth_is_signed() -> None:
    # The honest case at small book size: equity fell, APR is negative.
    score = compute_realized_apr_bps(_series((0, 83.52), (30, 75.75)))
    assert score is not None
    assert score.apr_bps < 0


def test_baseline_is_earliest_positive_sample() -> None:
    # Out-of-order input + a leading zero/garbage sample that must be ignored.
    score = compute_realized_apr_bps(
        _series((30, 110.0), (0, 0.0), (2, 100.0), (15, 105.0))
    )
    assert score is not None
    assert score.baseline_equity == Decimal("100.0")  # day 2, not day 0 (=0)
    assert score.current_equity == Decimal("110.0")  # day 30


def test_cash_flow_is_neutralized() -> None:
    """A deposit (near-instant +100% jump) must NOT read as performance.
    Time-weighted return skips the flow step and chains only the real ~1%
    moves around it — naive (current-baseline)/baseline would report ~+104%.
    """
    t0 = _T0
    history = [
        (t0, Decimal("100")),
        (t0 + timedelta(days=1), Decimal("101")),  # +1% real
        (t0 + timedelta(days=1, minutes=30), Decimal("200")),  # +$99 deposit
        (t0 + timedelta(days=2), Decimal("202")),  # +1% real
        (t0 + timedelta(days=4), Decimal("204")),  # ~+1% real
    ]
    score = compute_realized_apr_bps(history)
    assert score is not None
    assert score.n_flows == 1
    # The period return is the small real P&L net of the deposit, not the
    # +104% you'd get by folding the $99 top-up into the gain.
    assert score.period_return < Decimal("0.05")
    naive_bps = round(float((204 - 100) / 100) * 365 / 4 * 10_000)
    assert score.apr_bps < naive_bps // 4


def test_money_weighted_sign_tracks_dollar_loss() -> None:
    """The prod scenario that exposed the bug: a small gain on a small base,
    a deposit, then a larger dollar loss on the bigger base. Time-weighting
    would report a positive %; money-weighting (Modified Dietz) must stay
    negative because the account actually lost dollars."""
    t0 = _T0
    history = [
        (t0, Decimal("76")),
        (t0 + timedelta(days=1), Decimal("77")),  # +1.3% on ~$76 base
        (t0 + timedelta(days=1, minutes=5), Decimal("177")),  # +$100 deposit
        (t0 + timedelta(days=4), Decimal("175.62")),  # -0.78% on ~$177 base
    ]
    score = compute_realized_apr_bps(history)
    assert score is not None
    assert score.n_flows == 1
    assert score.period_return < 0
    assert score.apr_bps < 0


def test_none_with_fewer_than_two_positive_points() -> None:
    assert compute_realized_apr_bps(_series((0, 100.0))) is None
    assert compute_realized_apr_bps(_series((0, 0.0), (30, 110.0))) is None


def test_none_when_window_too_short() -> None:
    # 12h < MIN_WINDOW_HOURS (24h) → not enough window to annualize.
    short = [
        (_T0, Decimal("100")),
        (_T0 + timedelta(hours=12), Decimal("110")),
    ]
    assert compute_realized_apr_bps(short) is None


def test_none_on_zero_length_window() -> None:
    same_ts = [(_T0, Decimal("100")), (_T0, Decimal("110"))]
    assert compute_realized_apr_bps(same_ts) is None


def test_should_push_true_when_never_pushed(tmp_path: Path) -> None:
    assert should_push(path=tmp_path / "reputation_push.json") is True


def test_push_throttle_round_trip(tmp_path: Path) -> None:
    state = tmp_path / "reputation_push.json"
    score = ReputationScore(
        apr_bps=1234,
        period_return=Decimal("0.10"),
        baseline_equity=Decimal("100"),
        current_equity=Decimal("110"),
        baseline_ts=_T0,
        current_ts=_T0 + timedelta(days=30),
        elapsed_seconds=Decimal(30 * 86400),
        n_points=2,
        n_flows=0,
    )
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    record_push(score, "0xabc", now=now, path=state)

    # Just pushed → not due yet; due again once PUSH_INTERVAL elapses.
    assert should_push(now=now + timedelta(minutes=30), path=state) is False
    assert should_push(now=now + PUSH_INTERVAL, path=state) is True
