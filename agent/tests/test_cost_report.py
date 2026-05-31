"""Tests for the wake-reason frequency report (`event-driven-rebalance.8`)."""

from __future__ import annotations

import json
from pathlib import Path

from agent.sandbox.cost_report import (
    LEGACY_LABEL,
    format_report,
    load_cycles,
    summarize,
)


def _write_log(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_load_cycles_skips_blank_and_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "cycle_log.jsonl"
    path.write_text(
        json.dumps({"wake_reason": "heartbeat"}) + "\n"
        + "\n"  # blank
        + "{not json\n"  # malformed
        + json.dumps({"wake_reason": "event:price_drift"}) + "\n"
    )
    rows = load_cycles(path)
    assert len(rows) == 2
    assert rows[0]["wake_reason"] == "heartbeat"
    assert rows[1]["wake_reason"] == "event:price_drift"


def test_load_cycles_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_cycles(tmp_path / "nope.jsonl") == []


def test_load_cycles_tail_filters_to_last_n(tmp_path: Path) -> None:
    path = tmp_path / "cycle_log.jsonl"
    _write_log(path, [{"wake_reason": "heartbeat", "i": i} for i in range(5)])
    rows = load_cycles(path, tail=2)
    assert [r["i"] for r in rows] == [3, 4]


def test_summarize_empty_input_returns_zeroed_shape() -> None:
    summary = summarize([])
    assert summary["total"] == 0
    assert summary["counts"] == {}
    assert summary["shares"] == {}
    assert summary["event_driven_share"] == 0.0
    assert summary["first_started_at"] is None
    assert summary["last_started_at"] is None


def test_summarize_mixed_distribution() -> None:
    rows = [
        {"wake_reason": "heartbeat", "started_at": "T0"},
        {"wake_reason": "heartbeat"},
        {"wake_reason": "event:price_drift"},
        {"wake_reason": "event:price_drift"},
        {"wake_reason": "event:funding_flip"},
        {"wake_reason": "heartbeat", "started_at": "T5"},
    ]
    s = summarize(rows)
    assert s["total"] == 6
    assert s["counts"]["heartbeat"] == 3
    assert s["counts"]["event:price_drift"] == 2
    assert s["counts"]["event:funding_flip"] == 1
    # Counts ordered desc — heartbeat first
    assert list(s["counts"].keys())[0] == "heartbeat"
    # Shares
    assert s["shares"]["heartbeat"] == 0.5
    # event-driven share — 3/6 = 0.5
    assert s["event_driven_share"] == 0.5
    # Bracketed by first / last started_at when present
    assert s["first_started_at"] == "T0"
    assert s["last_started_at"] == "T5"


def test_summarize_missing_wake_reason_falls_back_to_legacy_label() -> None:
    """Rows from pre-`.3` runs have no `wake_reason` field — they MUST
    not be silently grouped under heartbeat (which would inflate the
    heartbeat count). Mark them explicitly so the operator knows when
    historical data is incomplete."""
    rows = [
        {"result": "ok"},  # no wake_reason
        {"wake_reason": "heartbeat"},
    ]
    s = summarize(rows)
    assert s["counts"][LEGACY_LABEL] == 1
    assert s["counts"]["heartbeat"] == 1


def test_format_report_empty_log_message() -> None:
    out = format_report(summarize([]))
    assert out == "No cycles in log."


def test_format_report_renders_breakdown_with_total() -> None:
    rows = [
        {"wake_reason": "heartbeat"},
        {"wake_reason": "event:price_drift"},
        {"wake_reason": "event:price_drift"},
    ]
    out = format_report(summarize(rows))
    assert "3 cycles" in out
    assert "heartbeat" in out
    assert "event:price_drift" in out
    assert "Event-driven share: 66.7%" in out


def test_format_report_emits_threshold_hint_when_one_kind_dominates() -> None:
    """If one event kind passes the 40% threshold of total cycles, the
    report nudges the operator toward tuning that event's threshold."""
    rows = [
        {"wake_reason": "heartbeat"},
        {"wake_reason": "heartbeat"},
        {"wake_reason": "event:price_drift"},
        {"wake_reason": "event:price_drift"},
        {"wake_reason": "event:price_drift"},
        {"wake_reason": "event:price_drift"},
    ]
    out = format_report(summarize(rows))
    assert "HINT" in out
    assert "event:price_drift" in out
    assert "notes/event-taxonomy.md" in out


def test_format_report_no_hint_when_distribution_is_balanced() -> None:
    rows = [
        {"wake_reason": "heartbeat"},
        {"wake_reason": "heartbeat"},
        {"wake_reason": "heartbeat"},
        {"wake_reason": "event:price_drift"},
        {"wake_reason": "event:funding_flip"},
    ]
    out = format_report(summarize(rows))
    assert "HINT" not in out
