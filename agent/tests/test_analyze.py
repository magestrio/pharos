"""Tests for sandbox.analyze — pure aggregator + filter + renderer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.sandbox.analyze import (
    AnalysisReport,
    analyze,
    filter_window,
    load_records,
    render_markdown,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────


def _ts(offset_minutes: int = 0) -> str:
    base = datetime(2026, 5, 27, 16, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(minutes=offset_minutes)).isoformat()


def _ok_cycle(idx: int = 0, *, confidence: float = 0.72, apr: float = 3.5) -> dict:
    return {
        "started_at": _ts(idx * 10),
        "finished_at": _ts(idx * 10 + 1),  # 60s cycle
        "stages": ["snapshot", "decide", "validate", "diff", "approval", "execute"],
        "snapshot_filename": f"snap-{idx}.json",
        "decision_filename": f"dec-{idx}.json",
        "confidence": confidence,
        "expected_apr_pct": apr,
        "validator_ok": True,
        "validator_errors": [],
        "actions_planned": 3,
        "actions_executed": 0,
        "actions": [
            {
                "kind": "redeem_earn",
                "category": "FlexibleSaving",
                "product_id": "2",
                "coin": "USDC",
                "amount": "5.0",
                "status": "dry-run",
                "error": None,
            },
            {
                "kind": "subscribe_earn",
                "category": "FlexibleSaving",
                "product_id": "1131",
                "coin": "USD1",
                "amount": "10.0",
                "status": "dry-run",
                "error": None,
            },
            {
                "kind": "skip_out_of_scope",
                "category": "LiquidityMining",
                "product_id": "24",
                "coin": "USDC",
                "amount": "2.0",
                "status": "skipped",
                "error": None,
            },
        ],
        "result": "ok",
    }


def _invalid_cycle() -> dict:
    return {
        "started_at": _ts(20),
        "finished_at": _ts(21),
        "stages": ["snapshot", "decide", "validate"],
        "confidence": 0.50,
        "expected_apr_pct": 2.0,
        "validator_ok": False,
        "validator_errors": [
            "hedge sizing outside tolerance band: TON: hedge $50 vs pick $30",
            "cash_usdc weight 0.05 below floor 0.10",
        ],
        "result": "skipped:invalid",
    }


def _error_cycle_with_retcode() -> dict:
    return {
        "started_at": _ts(30),
        "finished_at": _ts(31),
        "stages": ["snapshot", "decide", "validate", "diff", "approval", "execute"],
        "confidence": 0.80,
        "expected_apr_pct": 5.0,
        "validator_ok": True,
        "validator_errors": [],
        "actions_planned": 1,
        "actions_executed": 0,
        "actions": [
            {
                "kind": "open_perp_short",
                "category": "Perp",
                "product_id": "TONUSDT",
                "coin": "TON",
                "amount": "25",
                "status": "error",
                "error": "retCode=110007 Insufficient balance for sub-account",
            }
        ],
        "result": "executed",
    }


def _outermost_error_cycle() -> dict:
    return {
        "started_at": _ts(40),
        "finished_at": _ts(40),
        "stages": [],
        "error": "RuntimeError: snapshot collection blew up",
        "result": "error",
    }


# ─── analyze() aggregator ──────────────────────────────────────────────────


def test_analyze_empty_records_returns_zero_report() -> None:
    r = analyze([])
    assert r.total_cycles == 0
    assert r.result_counts == {}
    assert r.confidence_mean is None
    assert r.action_kind_counts == {}


def test_analyze_counts_outcomes_and_window() -> None:
    records = [_ok_cycle(0), _ok_cycle(1), _invalid_cycle()]
    r = analyze(records)
    assert r.total_cycles == 3
    assert r.result_counts == {"ok": 2, "skipped:invalid": 1}
    assert r.window_start == _ts(0)
    assert r.window_end == _ts(21)


def test_analyze_mean_confidence_skips_records_without_decide_stage() -> None:
    records = [
        _ok_cycle(0, confidence=0.60),
        _ok_cycle(1, confidence=0.80),
        _outermost_error_cycle(),  # no confidence field
    ]
    r = analyze(records)
    assert r.confidence_mean == 0.70  # mean of (0.60, 0.80), error cycle excluded
    assert r.confidence_min == 0.60
    assert r.confidence_max == 0.80


def test_analyze_aggregates_action_kind_and_status() -> None:
    records = [_ok_cycle(0), _ok_cycle(1)]
    r = analyze(records)
    # 2 cycles × 3 actions each.
    assert r.action_kind_counts == {
        "redeem_earn": 2,
        "subscribe_earn": 2,
        "skip_out_of_scope": 2,
    }
    assert r.action_status_counts == {"dry-run": 4, "skipped": 2}
    assert r.actions_planned_total == 6


def test_analyze_extracts_bybit_retcode_from_action_error() -> None:
    records = [_error_cycle_with_retcode()]
    r = analyze(records)
    assert r.bybit_error_counts == {"retCode=110007": 1}
    assert r.other_error_counts == {}


def test_analyze_buckets_non_retcode_errors_under_other() -> None:
    records = [_outermost_error_cycle()]
    r = analyze(records)
    # "RuntimeError: snapshot collection blew up" → tag "RuntimeError"
    assert r.other_error_counts == {"RuntimeError": 1}
    assert r.bybit_error_counts == {}


def test_analyze_buckets_validator_failures() -> None:
    records = [_invalid_cycle()]
    r = analyze(records)
    # Two distinct validator errors, each counted once.
    assert sum(r.validator_failure_counts.values()) == 2
    assert any(
        "hedge sizing" in msg for msg in r.validator_failure_counts
    )


def test_analyze_cycle_duration_stats() -> None:
    records = [_ok_cycle(0), _ok_cycle(1), _ok_cycle(2)]
    r = analyze(records)
    assert r.cycle_duration_seconds["n"] == 3
    # Each cycle is 60s (offset_minutes * 10 minutes for started, +1 min for finished).
    assert r.cycle_duration_seconds["mean"] == 60.0
    assert r.cycle_duration_seconds["p50"] == 60.0


def test_analyze_cycle_duration_drops_malformed_timestamps() -> None:
    rec = _ok_cycle(0)
    rec.pop("finished_at")
    r = analyze([rec])
    assert r.cycle_duration_seconds == {}


# ─── filter_window ─────────────────────────────────────────────────────────


def test_filter_window_returns_all_when_no_bounds() -> None:
    records = [_ok_cycle(0), _ok_cycle(1)]
    assert filter_window(records, None, None) == records


def test_filter_window_respects_since_bound_inclusive() -> None:
    records = [_ok_cycle(0), _ok_cycle(1), _ok_cycle(2)]
    since = datetime.fromisoformat(_ts(10))  # cycle 1 starts here
    out = filter_window(records, since, None)
    assert [r["started_at"] for r in out] == [_ts(10), _ts(20)]


def test_filter_window_respects_until_bound_inclusive() -> None:
    records = [_ok_cycle(0), _ok_cycle(1), _ok_cycle(2)]
    until = datetime.fromisoformat(_ts(10))
    out = filter_window(records, None, until)
    assert [r["started_at"] for r in out] == [_ts(0), _ts(10)]


def test_filter_window_drops_records_without_started_at_when_bound_set() -> None:
    rec = _ok_cycle(0)
    rec.pop("started_at")
    out = filter_window(
        [rec, _ok_cycle(1)],
        since=datetime.fromisoformat(_ts(0)),
        until=None,
    )
    assert len(out) == 1
    assert out[0]["started_at"] == _ts(10)


# ─── load_records (file I/O) ───────────────────────────────────────────────


def test_load_records_parses_jsonl(tmp_path: Path) -> None:
    log = tmp_path / "cycle_log.jsonl"
    log.write_text(json.dumps(_ok_cycle(0)) + "\n" + json.dumps(_ok_cycle(1)) + "\n")
    out = load_records(log)
    assert len(out) == 2
    assert out[0]["result"] == "ok"


def test_load_records_skips_blank_lines(tmp_path: Path) -> None:
    log = tmp_path / "cycle_log.jsonl"
    log.write_text("\n" + json.dumps(_ok_cycle(0)) + "\n\n")
    assert len(load_records(log)) == 1


def test_load_records_tolerates_corrupt_line(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    log = tmp_path / "cycle_log.jsonl"
    log.write_text(json.dumps(_ok_cycle(0)) + "\n{not json}\n" + json.dumps(_ok_cycle(1)) + "\n")
    out = load_records(log)
    assert len(out) == 2  # bad line dropped, others kept
    captured = capsys.readouterr()
    assert "line 2" in captured.err


def test_load_records_missing_path_returns_empty(tmp_path: Path) -> None:
    assert load_records(tmp_path / "no-such-file.jsonl") == []


# ─── render_markdown ───────────────────────────────────────────────────────


def test_render_markdown_empty_returns_no_cycles_note() -> None:
    out = render_markdown(analyze([]))
    assert "No cycles in window" in out


def test_render_markdown_full_report_contains_key_sections() -> None:
    records = [_ok_cycle(0), _invalid_cycle(), _error_cycle_with_retcode()]
    md = render_markdown(analyze(records))
    assert "# Sandbox cycle log analysis" in md
    assert "## Outcomes" in md
    assert "## Decisions" in md
    assert "## Actions" in md
    assert "## Errors" in md
    assert "retCode=110007" in md
    assert "## Validator failures" in md
    assert "hedge sizing" in md
    assert "## Cycle durations" in md
    assert ".39" in md  # follow-up note


def test_report_to_dict_serializes_for_json_output() -> None:
    records = [_ok_cycle(0), _error_cycle_with_retcode()]
    d = analyze(records).to_dict()
    # Round-trip through JSON to confirm everything's a primitive.
    blob = json.dumps(d)
    parsed = json.loads(blob)
    assert parsed["total_cycles"] == 2
    assert parsed["errors"]["bybit_retcodes"]["retCode=110007"] == 1
