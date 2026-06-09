"""Wake-reason frequency report over the cycle log
(`event-driven-rebalance.8`).

Reads `cycle_log.jsonl` (one JSON object per line, written by
`agent.sandbox.loop`) and breaks down cycles by `wake_reason`. Used
to tune the watcher thresholds in `notes/event-taxonomy.md`: if one
event class dominates the count, its threshold is too tight and the
watcher is firing on noise.

Dollar-cost attribution (cycles × token usage × model price) is a
SEPARATE concern tracked by `bybit-sandbox.39` — that subtask wires
per-cycle token counts and `estimated_cost_usd` into the same JSONL
shape, at which point this report adds an `estimated_cost` column.
Until then we report frequencies only.

CLI:
    # default cycle_log
    python -m agent.sandbox.cost_report

    # specific file
    python -m agent.sandbox.cost_report --log path/to/cycle_log.jsonl

    # filter to last N cycles
    python -m agent.sandbox.cost_report --tail 200
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_CYCLE_LOG = Path(__file__).parent / "state" / "cycle_log.jsonl"

# Sentinel for entries that pre-date `event-driven-rebalance.3` (those
# rows have no `wake_reason` field). Treat them as heartbeat — events
# weren't a possibility yet.
LEGACY_LABEL = "heartbeat:legacy-pre-event-loop"


def load_cycles(
    log_path: Path = DEFAULT_CYCLE_LOG, tail: int | None = None
) -> list[dict[str, Any]]:
    """Read the JSONL cycle log. Skips empty + malformed lines silently
    (operator may have a partial line at the tail from a crash). Returns
    parsed dicts in file order; if `tail` is set, only the last N rows.
    """
    if not log_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in log_path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        rows.append(row)
    if tail is not None and tail >= 0:
        rows = rows[-tail:]
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the rows by `wake_reason`.

    Returns:
        {
          "total": int,
          "counts": dict[reason → count],     # ordered by count desc
          "shares": dict[reason → fraction],  # of total
          "event_driven_share": float,        # fraction with reason starting with "event:"
          "first_started_at": str | None,
          "last_started_at":  str | None,
        }

    Empty input → zeroed shape (no crash).
    """
    counts: Counter[str] = Counter()
    started: list[str] = []
    for row in rows:
        reason = row.get("wake_reason") or LEGACY_LABEL
        counts[reason] += 1
        if "started_at" in row:
            started.append(str(row["started_at"]))
    total = sum(counts.values())
    shares = {k: (v / total if total else 0.0) for k, v in counts.items()}
    event_driven_count = sum(
        v for k, v in counts.items() if k.startswith("event:")
    )
    event_driven_share = (event_driven_count / total) if total else 0.0
    # Sort counts descending for stable presentation
    ordered_counts = dict(
        sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    ordered_shares = {k: shares[k] for k in ordered_counts}
    return {
        "total": total,
        "counts": ordered_counts,
        "shares": ordered_shares,
        "event_driven_share": event_driven_share,
        "first_started_at": started[0] if started else None,
        "last_started_at": started[-1] if started else None,
    }


def format_report(summary: dict[str, Any]) -> str:
    """Render a plain-text table from `summarize()`'s output."""
    total = summary["total"]
    if total == 0:
        return "No cycles in log."
    width = max(len(k) for k in summary["counts"]) + 2
    lines = []
    span = ""
    if summary["first_started_at"] and summary["last_started_at"]:
        span = f" ({summary['first_started_at']} → {summary['last_started_at']})"
    lines.append(f"Cycle reason breakdown — {total} cycles{span}")
    lines.append("=" * 72)
    for reason, count in summary["counts"].items():
        share = summary["shares"][reason]
        lines.append(f"  {reason:<{width}} {count:>6}  ({share:6.1%})")
    lines.append("-" * 72)
    lines.append(
        f"  Event-driven share: {summary['event_driven_share']:.1%} "
        f"(remainder = heartbeat)"
    )
    # Threshold-tuning hint — only mentioned when one event kind
    # dominates so the operator knows what knob to turn.
    event_kinds = {
        k: v for k, v in summary["counts"].items() if k.startswith("event:")
    }
    if event_kinds:
        top_kind, top_count = max(event_kinds.items(), key=lambda kv: kv[1])
        top_share = top_count / total
        if top_share >= 0.40:
            lines.append("")
            lines.append(
                f"HINT: `{top_kind}` accounts for {top_share:.0%} of cycles "
                "— consider raising its threshold in "
                "`notes/event-taxonomy.md` (or tightening the underlying "
                "checker logic) to cut reactive-cycle volume."
            )
    return "\n".join(lines)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Frequency report over the cycle log — counts cycles by "
            "wake_reason (heartbeat vs event:<kind>). Dollar-cost "
            "attribution will be added when `bybit-sandbox.39` lands."
        )
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_CYCLE_LOG,
        help=f"Path to cycle log JSONL (default {DEFAULT_CYCLE_LOG}).",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=None,
        help="Only consider the last N cycles (default: all).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the raw summary as JSON instead of the formatted table.",
    )
    args = parser.parse_args()
    rows = load_cycles(args.log, tail=args.tail)
    summary = summarize(rows)
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    else:
        sys.stdout.write(format_report(summary) + "\n")


if __name__ == "__main__":
    _main()
