"""Tests for the operator-facing safety net (`agent.sandbox.safety`).

Covers:
  • `is_halted` / `halt` round-trip with the file system
  • `record_equity` append + `_read_history` parse round-trip,
    including malformed-line tolerance
  • `check_daily_drawdown` corner cases: no history, history < 24h,
    baseline > 24h with drop within / above threshold, zero / negative
    current equity
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from agent.sandbox.safety import (
    DAILY_DRAWDOWN_HALT_PCT,
    check_daily_drawdown,
    halt,
    is_halted,
    record_equity,
)


def test_is_halted_false_when_missing(tmp_path: Path) -> None:
    assert is_halted(path=tmp_path / "HALT") == (False, None)


def test_halt_creates_file_with_reason(tmp_path: Path) -> None:
    halt_path = tmp_path / "HALT"
    halt("test halt reason", path=halt_path)
    assert halt_path.exists()
    body = halt_path.read_text()
    assert "test halt reason" in body
    # Timestamp line precedes the reason — preserves WHEN the halt fired
    # so the operator can correlate with logs.
    first_line = body.splitlines()[0]
    # ISO format with timezone
    datetime.fromisoformat(first_line)  # raises if not parseable


def test_is_halted_reads_reason_from_file(tmp_path: Path) -> None:
    halt_path = tmp_path / "HALT"
    halt("specific carry-state failure", path=halt_path)
    halted, reason = is_halted(path=halt_path)
    assert halted is True
    assert reason is not None and "specific carry-state failure" in reason


def test_is_halted_handles_empty_file(tmp_path: Path) -> None:
    halt_path = tmp_path / "HALT"
    halt_path.touch()
    halted, reason = is_halted(path=halt_path)
    assert halted is True
    assert reason is not None and "no reason recorded" in reason


def test_halt_overwrites_existing(tmp_path: Path) -> None:
    """Idempotent: re-halting overwrites with the newest context — the
    operator usually wants the most recent trigger reason."""
    halt_path = tmp_path / "HALT"
    halt("first", path=halt_path)
    halt("second", path=halt_path)
    assert "second" in halt_path.read_text()
    assert "first" not in halt_path.read_text()


def test_record_equity_appends_jsonl(tmp_path: Path) -> None:
    history = tmp_path / "equity.jsonl"
    record_equity(Decimal("300.00"), path=history)
    record_equity(Decimal("299.50"), path=history)
    lines = history.read_text().splitlines()
    assert len(lines) == 2
    e0 = json.loads(lines[0])
    e1 = json.loads(lines[1])
    assert e0["total_equity_usd"] == "300.00"
    assert e1["total_equity_usd"] == "299.50"


def test_check_daily_drawdown_no_history(tmp_path: Path) -> None:
    """Fresh deploy — nothing to compare against → no halt signal."""
    history = tmp_path / "equity.jsonl"
    hit, reason = check_daily_drawdown(
        Decimal("300"),
        history_path=history,
    )
    assert hit is False
    assert reason is None


def test_check_daily_drawdown_history_younger_than_window(tmp_path: Path) -> None:
    """History exists but all entries are within the baseline window —
    we don't yet have a 24h-old reference, so no halt fires."""
    history = tmp_path / "equity.jsonl"
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    # Entry from 6h ago — within 24h window, not a baseline candidate.
    record_equity(Decimal("400"), ts=now - timedelta(hours=6), path=history)
    hit, _ = check_daily_drawdown(
        Decimal("300"),  # would be 25% drop if 400 were the baseline
        history_path=history,
        now=now,
    )
    assert hit is False


def test_check_daily_drawdown_trips_on_baseline_drop(tmp_path: Path) -> None:
    history = tmp_path / "equity.jsonl"
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    # 25h ago equity was $400, now $300 → 25% drawdown ≥ 10% threshold.
    record_equity(Decimal("400"), ts=now - timedelta(hours=25), path=history)
    record_equity(Decimal("350"), ts=now - timedelta(hours=12), path=history)
    hit, reason = check_daily_drawdown(
        Decimal("300"),
        history_path=history,
        now=now,
    )
    assert hit is True
    assert reason is not None
    assert "drawdown" in reason
    assert "25.00%" in reason or "25%" in reason
    assert "400" in reason and "300" in reason


def test_check_daily_drawdown_passes_within_threshold(tmp_path: Path) -> None:
    """5% drop over 24h is within the default 10% threshold → pass."""
    history = tmp_path / "equity.jsonl"
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    record_equity(Decimal("300"), ts=now - timedelta(hours=25), path=history)
    hit, reason = check_daily_drawdown(
        Decimal("285"),  # 5% drop
        history_path=history,
        now=now,
    )
    assert hit is False
    assert reason is None


def test_check_daily_drawdown_custom_threshold(tmp_path: Path) -> None:
    """Operator can tighten threshold via env / explicit kwarg for
    smaller-vault experiments — 3% drop trips on a 2% threshold."""
    history = tmp_path / "equity.jsonl"
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    record_equity(Decimal("300"), ts=now - timedelta(hours=30), path=history)
    hit, _ = check_daily_drawdown(
        Decimal("291"),  # 3% drop
        history_path=history,
        threshold_pct=0.02,
        now=now,
    )
    assert hit is True


def test_check_daily_drawdown_zero_equity(tmp_path: Path) -> None:
    """Zero / negative current equity → ratio is degenerate, return no
    halt and let the cycle's other guards handle the empty-wallet case."""
    history = tmp_path / "equity.jsonl"
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    record_equity(Decimal("300"), ts=now - timedelta(hours=25), path=history)
    hit, _ = check_daily_drawdown(
        Decimal("0"),
        history_path=history,
        now=now,
    )
    assert hit is False


def test_check_daily_drawdown_picks_closest_pre_cutoff_entry(tmp_path: Path) -> None:
    """When multiple entries are >24h old, we pick the NEWEST one before
    the cutoff — not the oldest. Otherwise a week-old high-water mark
    would forever trigger a halt against a recently-stabilized book."""
    history = tmp_path / "equity.jsonl"
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    # 5-day-old peak $500, 25h-old realistic baseline $310.
    record_equity(Decimal("500"), ts=now - timedelta(days=5), path=history)
    record_equity(Decimal("310"), ts=now - timedelta(hours=25), path=history)
    record_equity(Decimal("305"), ts=now - timedelta(hours=12), path=history)
    hit, reason = check_daily_drawdown(
        Decimal("295"),  # ≈ 4.8% drop vs 310, but 41% vs 500
        history_path=history,
        now=now,
    )
    # vs the 25h-old $310 baseline, drop is well below 10% → pass.
    assert hit is False, reason


def test_read_history_tolerates_malformed_lines(tmp_path: Path) -> None:
    """One torn line during an OS hiccup must not invalidate the whole
    history — drawdown is a safety net, not an audit log."""
    history = tmp_path / "equity.jsonl"
    record_equity(Decimal("300"), path=history)
    # Inject garbage between valid lines.
    with history.open("a") as f:
        f.write("{not json\n")
        f.write('{"ts": "not-a-date", "total_equity_usd": "100"}\n')
    record_equity(Decimal("310"), path=history)
    # check_daily_drawdown is the integration consumer; if it returns
    # without raising, the malformed lines were skipped silently.
    now = datetime.now(UTC) + timedelta(hours=25)
    hit, _ = check_daily_drawdown(Decimal("305"), history_path=history, now=now)
    # 300 → 305 is positive return → no halt.
    assert hit is False


def test_default_drawdown_threshold_is_conservative() -> None:
    """Document the default — protects against accidentally tightening
    in production via an unreviewed env override."""
    # 10% is our conservative default; should NOT change without a
    # corresponding operator-facing change-log entry.
    assert DAILY_DRAWDOWN_HALT_PCT == pytest.approx(0.10)
