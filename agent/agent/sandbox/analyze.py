"""Post-mortem for the sandbox cycle log.

Reads `agent/sandbox/cycle_log.jsonl` (the JSONL emitted by `loop.py`
one-line-per-cycle) and prints aggregate statistics — outcome counts,
mean confidence/APR, action histograms, Bybit error distribution,
validator failure reasons, cycle duration percentiles.

Designed for the `.14` 24-48h smoke test post-mortem. Pure-function
core (`analyze(records)` → `AnalysisReport`) keeps the math testable
without disk; the CLI wraps it with filtering and rendering.

Usage:

    python -m agent.sandbox.analyze
    python -m agent.sandbox.analyze --since 2026-05-27T00:00:00 --until ...
    python -m agent.sandbox.analyze --json    # machine-readable

Anthropic API spend tracking is not yet wired — see `.39` follow-up.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

CYCLE_LOG = Path(__file__).parent / "state" / "cycle_log.jsonl"

# `error` strings on action results look like `"retCode=110007 Insufficient
# balance"` (per `_execute_one` formatting). Capture the code for the
# histogram; non-Bybit errors (TimeoutError etc.) get bucketed separately.
_RETCODE_RE = re.compile(r"retCode=(\d+)")


@dataclass
class AnalysisReport:
    window_start: str | None
    window_end: str | None
    total_cycles: int
    result_counts: dict[str, int]
    confidence_mean: float | None
    confidence_min: float | None
    confidence_max: float | None
    expected_apr_pct_mean: float | None
    actions_planned_total: int
    actions_executed_total: int
    action_kind_counts: dict[str, int]
    action_status_counts: dict[str, int]
    bybit_error_counts: dict[str, int]
    other_error_counts: dict[str, int]
    validator_failure_counts: dict[str, int]
    cycle_duration_seconds: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "total_cycles": self.total_cycles,
            "result_counts": self.result_counts,
            "confidence": {
                "mean": self.confidence_mean,
                "min": self.confidence_min,
                "max": self.confidence_max,
            },
            "expected_apr_pct_mean": self.expected_apr_pct_mean,
            "actions": {
                "planned_total": self.actions_planned_total,
                "executed_total": self.actions_executed_total,
                "by_kind": self.action_kind_counts,
                "by_status": self.action_status_counts,
            },
            "errors": {
                "bybit_retcodes": self.bybit_error_counts,
                "other": self.other_error_counts,
                "validator_failures": self.validator_failure_counts,
            },
            "cycle_duration_seconds": self.cycle_duration_seconds,
        }


# ─── Loading + filtering ────────────────────────────────────────────────────


def load_records(path: Path) -> list[dict[str, Any]]:
    """Read JSONL one record per line. Tolerates trailing blank lines and
    JSON parse errors on individual lines (logs them to stderr but keeps
    going — a single corrupt line shouldn't blind the rest of the run)."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for i, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"warn: cycle_log line {i}: {e}", file=sys.stderr)
    return records


def filter_window(
    records: list[dict[str, Any]],
    since: datetime | None,
    until: datetime | None,
) -> list[dict[str, Any]]:
    """Keep records whose `started_at` falls within `[since, until]`.
    Records without `started_at` (malformed) are dropped when ANY bound
    is set, kept when both bounds are None."""
    if since is None and until is None:
        return list(records)
    out: list[dict[str, Any]] = []
    for r in records:
        ts = _parse_iso(r.get("started_at"))
        if ts is None:
            continue
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        out.append(r)
    return out


# ─── Aggregation ────────────────────────────────────────────────────────────


def analyze(records: list[dict[str, Any]]) -> AnalysisReport:
    if not records:
        return AnalysisReport(
            window_start=None,
            window_end=None,
            total_cycles=0,
            result_counts={},
            confidence_mean=None,
            confidence_min=None,
            confidence_max=None,
            expected_apr_pct_mean=None,
            actions_planned_total=0,
            actions_executed_total=0,
            action_kind_counts={},
            action_status_counts={},
            bybit_error_counts={},
            other_error_counts={},
            validator_failure_counts={},
        )

    started_ts = [t for t in (_parse_iso(r.get("started_at")) for r in records) if t]
    finished_ts = [t for t in (_parse_iso(r.get("finished_at")) for r in records) if t]
    window_start = min(started_ts).isoformat() if started_ts else None
    window_end = max(finished_ts).isoformat() if finished_ts else None

    result_counts = dict(Counter(r.get("result", "missing") for r in records))

    confidences = [r["confidence"] for r in records if "confidence" in r]
    aprs = [r["expected_apr_pct"] for r in records if "expected_apr_pct" in r]

    actions_planned_total = sum(r.get("actions_planned", 0) for r in records)
    actions_executed_total = sum(r.get("actions_executed", 0) for r in records)

    kind_counter: Counter[str] = Counter()
    status_counter: Counter[str] = Counter()
    bybit_errors: Counter[str] = Counter()
    other_errors: Counter[str] = Counter()
    for r in records:
        for a in r.get("actions") or []:
            kind_counter[a.get("kind", "?")] += 1
            status_counter[a.get("status", "?")] += 1
            err = a.get("error")
            if err:
                m = _RETCODE_RE.search(err)
                if m:
                    bybit_errors[f"retCode={m.group(1)}"] += 1
                else:
                    other_errors[err.split(":", 1)[0]] += 1
        # Cycle-level error (outermost guard caught something before/during dispatch).
        cycle_err = r.get("error")
        if cycle_err:
            other_errors[cycle_err.split(":", 1)[0]] += 1

    validator_failures: Counter[str] = Counter()
    for r in records:
        if r.get("validator_ok") is False:
            for e in r.get("validator_errors") or []:
                # Validator messages can carry per-coin specifics — group
                # on the first 80 chars so similar failures bucket.
                validator_failures[e[:80]] += 1

    durations = _cycle_durations(records)

    return AnalysisReport(
        window_start=window_start,
        window_end=window_end,
        total_cycles=len(records),
        result_counts=result_counts,
        confidence_mean=_mean(confidences),
        confidence_min=min(confidences) if confidences else None,
        confidence_max=max(confidences) if confidences else None,
        expected_apr_pct_mean=_mean(aprs),
        actions_planned_total=actions_planned_total,
        actions_executed_total=actions_executed_total,
        action_kind_counts=dict(kind_counter),
        action_status_counts=dict(status_counter),
        bybit_error_counts=dict(bybit_errors),
        other_error_counts=dict(other_errors),
        validator_failure_counts=dict(validator_failures),
        cycle_duration_seconds=durations,
    )


def _cycle_durations(records: list[dict[str, Any]]) -> dict[str, float]:
    """Per-cycle wall-clock duration aggregates. Skips records missing
    either timestamp; cycles where `started_at > finished_at` (clock
    drift, never observed in practice but plausible) get dropped too."""
    durations: list[float] = []
    for r in records:
        s = _parse_iso(r.get("started_at"))
        f = _parse_iso(r.get("finished_at"))
        if s is None or f is None or f < s:
            continue
        durations.append((f - s).total_seconds())
    if not durations:
        return {}
    durations.sort()
    return {
        "n": float(len(durations)),
        "mean": round(statistics.fmean(durations), 3),
        "p50": round(durations[len(durations) // 2], 3),
        "p95": round(durations[min(len(durations) - 1, int(len(durations) * 0.95))], 3),
        "max": round(durations[-1], 3),
    }


def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 4) if xs else None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        # Python 3.11 fromisoformat handles offset-aware ISO 8601.
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ─── Rendering ──────────────────────────────────────────────────────────────


def render_markdown(report: AnalysisReport) -> str:
    if report.total_cycles == 0:
        return "# Sandbox cycle log analysis\n\nNo cycles in window.\n"

    lines: list[str] = ["# Sandbox cycle log analysis", ""]
    lines.append(
        f"**Window**: {report.window_start} → {report.window_end} "
        f"({report.total_cycles} cycle{'s' if report.total_cycles != 1 else ''})"
    )
    lines.append("")

    lines.append("## Outcomes")
    lines.extend(_render_counts(report.result_counts, report.total_cycles))
    lines.append("")

    if report.confidence_mean is not None:
        lines.append("## Decisions")
        lines.append(
            f"- confidence: mean {report.confidence_mean:.2f}, "
            f"min {report.confidence_min:.2f}, max {report.confidence_max:.2f}"
        )
        if report.expected_apr_pct_mean is not None:
            lines.append(
                f"- expected blended APR: {report.expected_apr_pct_mean:.2f}%"
            )
        lines.append("")

    if report.action_kind_counts:
        lines.append("## Actions")
        lines.append(
            f"- planned total: {report.actions_planned_total}"
        )
        lines.append(
            f"- executed (status=ok): {report.actions_executed_total}"
        )
        lines.append("")
        lines.append("### By kind")
        for kind, n in sorted(
            report.action_kind_counts.items(), key=lambda x: -x[1]
        ):
            lines.append(f"- {kind}: {n}")
        lines.append("")
        lines.append("### By status")
        for status, n in sorted(
            report.action_status_counts.items(), key=lambda x: -x[1]
        ):
            lines.append(f"- {status}: {n}")
        lines.append("")

    if report.bybit_error_counts or report.other_error_counts:
        lines.append("## Errors")
        if report.bybit_error_counts:
            lines.append("### Bybit retCodes")
            for code, n in sorted(
                report.bybit_error_counts.items(), key=lambda x: -x[1]
            ):
                lines.append(f"- {code}: {n}")
        if report.other_error_counts:
            lines.append("### Other")
            for tag, n in sorted(
                report.other_error_counts.items(), key=lambda x: -x[1]
            ):
                lines.append(f"- {tag}: {n}")
        lines.append("")

    if report.validator_failure_counts:
        lines.append("## Validator failures")
        for msg, n in sorted(
            report.validator_failure_counts.items(), key=lambda x: -x[1]
        ):
            lines.append(f"- ({n}) {msg}")
        lines.append("")

    if report.cycle_duration_seconds:
        d = report.cycle_duration_seconds
        lines.append("## Cycle durations (seconds)")
        lines.append(
            f"- n={int(d['n'])}, mean={d['mean']}, "
            f"p50={d['p50']}, p95={d['p95']}, max={d['max']}"
        )
        lines.append("")

    lines.append("## Notes")
    lines.append(
        "- Anthropic API spend tracking not yet wired (`.39` follow-up)."
    )
    return "\n".join(lines) + "\n"


def _render_counts(counts: dict[str, int], total: int) -> list[str]:
    out: list[str] = []
    for k, n in sorted(counts.items(), key=lambda x: -x[1]):
        pct = (100.0 * n / total) if total else 0.0
        out.append(f"- {k}: {n} ({pct:.1f}%)")
    return out


# ─── CLI ────────────────────────────────────────────────────────────────────


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate stats from the sandbox cycle_log.jsonl."
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=CYCLE_LOG,
        help=f"Path to cycle_log.jsonl (default: {CYCLE_LOG})",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO 8601 lower bound on cycle started_at (inclusive)",
    )
    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="ISO 8601 upper bound on cycle started_at (inclusive)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of markdown",
    )
    args = parser.parse_args()

    since = _parse_iso(args.since) if args.since else None
    until = _parse_iso(args.until) if args.until else None
    if args.since and since is None:
        parser.error(f"--since: cannot parse {args.since!r} as ISO 8601")
    if args.until and until is None:
        parser.error(f"--until: cannot parse {args.until!r} as ISO 8601")

    records = filter_window(load_records(args.log), since, until)
    report = analyze(records)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_markdown(report), end="")


if __name__ == "__main__":
    _main()
