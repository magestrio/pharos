"""Operator-facing safety net for live runs.

Two independent circuit breakers, both filesystem-backed so the operator
can intervene with `touch` / `rm` without touching the process:

  • **HALT marker** (`state/HALT`). Manual kill switch. When the file
    exists, `run_one_cycle` short-circuits before snapshot, returning
    `result="halted"`. Used for emergency stops AND auto-tripped by the
    cycle when `write_carry_state` fails (state-coherence risk: real
    Bybit position open, state file not updated → next cycle could
    double-position).

  • **Daily drawdown tracker** (`state/equity_history.jsonl`). Each
    cycle appends `{ts, total_equity_usd}`. Before execute, the cycle
    compares the current equity against the closest entry ≥ 24h ago.
    If the drop exceeds `DAILY_DRAWDOWN_HALT_PCT` (default 10%) the
    cycle creates the HALT marker (`trigger=daily_drawdown`) and
    short-circuits — surfaces a sustained loss rather than letting the
    agent keep transacting against a deteriorating book.

**Drawdown HALT auto-recovery (state-8).** A drawdown-triggered HALT is
NOT sticky: a halted cycle whose marker carries `trigger=daily_drawdown`
still snapshots + `record_equity` (so the 24h window keeps advancing) and
re-checks the drawdown; once the book recovers back within threshold the
cycle `clear_halt()`s the marker and resumes the same cycle. An OPERATOR
halt (a bare `touch`ed marker, the carry-state coherence trip, or the
startup-gate trip — anything without `trigger=daily_drawdown`) stays fully
manual: it short-circuits before the snapshot and only an operator `rm`
clears it. This is why `halt_trigger` defaults to `operator` for any
ambiguous marker — auto-recovery must be opt-in, never the fallback.

Both are best-effort: filesystem failures degrade to "no halt" rather
than blocking the cycle on a stat() error. The tradeoff is acceptable
because the underlying execution code already has independent guards
(atomic-pair, paired-notional, carry close-attempt counter) — these
breakers are an outer safety layer for first-live confidence.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

log = logging.getLogger(__name__)


# Both files live in the same `state/` dir as `watcher-baseline.json` and
# `funding_carry.json` — a single mountpoint the operator can `ls` to see
# everything cycle-persistent.
_STATE_DIR = Path(__file__).parent / "state"
HALT_FILE: Path = _STATE_DIR / "HALT"
EQUITY_HISTORY_FILE: Path = _STATE_DIR / "equity_history.jsonl"

# Default daily drawdown threshold. 10% is conservative — fee drag at
# 4h cycles is ~0.3-1%/day, so 10% requires a real loss to trigger.
# Override via env `VAULT8004_DAILY_DRAWDOWN_HALT_PCT` (e.g. "0.05" for
# 5%) when testing or running smaller-vault experiments.
DAILY_DRAWDOWN_HALT_PCT: float = float(
    os.environ.get("VAULT8004_DAILY_DRAWDOWN_HALT_PCT", "0.10")
)

# How far back to look for the drawdown baseline. The "daily" framing is
# nominal — the check fires on every cycle, comparing against the
# OLDEST entry that's at least this far back. 24h matches the operator's
# mental model and is robust to single-cycle noise.
_DRAWDOWN_BASELINE_HOURS: float = 24.0


def is_halted(path: Path | None = None) -> tuple[bool, str | None]:
    """Return `(halted, reason)`. `reason` is the file contents if any,
    otherwise a generic message. Errors reading the file degrade to
    "halted with unreadable reason" — better to over-stop than miss a
    halt signal.

    `path=None` (the call-site default in `loop.py`) resolves to the
    module-level `HALT_FILE` at call time so tests can patch the
    constant and have the override picked up.
    """
    path = path or HALT_FILE
    if not path.exists():
        return False, None
    try:
        reason = path.read_text().strip() or "halt marker present (no reason recorded)"
    except OSError as e:
        reason = f"halt marker present (read failed: {e})"
    return True, reason


def halt(reason: str, path: Path | None = None, *, trigger: str = "operator") -> None:
    """Create the halt marker with a human-readable reason. Idempotent —
    if the file already exists the reason is overwritten (operator might
    want the latest trigger context). On write failure, log + give up:
    the safety net can't enforce itself when the disk is gone, but the
    surrounding cycle still records the error in its outcome.

    `trigger` is stamped on a `trigger=` line so the cycle can tell an
    auto-recoverable `daily_drawdown` halt from a manual `operator` stop
    (see module docstring). Default `operator` — any non-drawdown caller
    (carry-coherence trip, startup gate, manual `touch`) stays sticky."""
    path = path or HALT_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{datetime.now(UTC).isoformat()}\ntrigger={trigger}\n{reason}\n"
        )
        log.warning("HALT created (trigger=%s): %s", trigger, reason)
    except OSError as e:
        log.exception("failed to create HALT marker (%s) — operator must "
                      "intervene manually: %s", path, e)


def halt_trigger(path: Path | None = None) -> str:
    """Parse the halt trigger from the marker. Returns `operator` for a
    missing marker, a bare `touch`ed file, an unreadable file, or any marker
    without a `trigger=` line — auto-recovery is strictly opt-in, so anything
    ambiguous is treated as a manual operator stop that never self-clears."""
    path = path or HALT_FILE
    if not path.exists():
        return "operator"
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("trigger="):
                return line.split("=", 1)[1].strip() or "operator"
    except OSError:
        return "operator"
    return "operator"


def clear_halt(path: Path | None = None) -> None:
    """Remove the halt marker (drawdown auto-recovery). Best-effort — a
    missing file is already the desired state."""
    path = path or HALT_FILE
    try:
        path.unlink(missing_ok=True)
        log.warning("HALT cleared")
    except OSError as e:
        log.warning("failed to clear HALT marker (%s): %s", path, e)


def record_equity(
    total_equity_usd: Decimal | str,
    *,
    ts: datetime | None = None,
    path: Path | None = None,
) -> None:
    """Append one `{ts, total_equity_usd}` line to the history. Caller
    is `run_one_cycle` immediately after snapshot. JSONL append is
    intentionally non-atomic — a torn line just means one cycle's entry
    is malformed and gets skipped by `_read_history`, which is fine."""
    path = path or EQUITY_HISTORY_FILE
    ts = ts or datetime.now(UTC)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({
                "ts": ts.isoformat(),
                "total_equity_usd": str(total_equity_usd),
            }) + "\n")
    except OSError as e:
        log.warning("equity_history append failed: %s", e)


def _read_history(path: Path) -> list[tuple[datetime, Decimal]]:
    """Parse the JSONL history. Skips malformed lines silently — the
    drawdown check is a safety net, not an audit trail; partial reads
    are recoverable on the next cycle."""
    if not path.exists():
        return []
    out: list[tuple[datetime, Decimal]] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = datetime.fromisoformat(entry["ts"])
                eq = Decimal(entry["total_equity_usd"])
                out.append((ts, eq))
            except (KeyError, ValueError, InvalidOperation, json.JSONDecodeError):
                continue
    except OSError as e:
        log.warning("equity_history read failed: %s", e)
    return out


def check_daily_drawdown(
    current_equity: Decimal,
    *,
    threshold_pct: float = DAILY_DRAWDOWN_HALT_PCT,
    baseline_hours: float = _DRAWDOWN_BASELINE_HOURS,
    history_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Compare `current_equity` against the closest history entry that
    is at least `baseline_hours` old. Returns `(exceeded, reason)`:

      • `(False, None)` if no qualifying baseline exists yet (fresh
        deploy, history < baseline_hours), or if drawdown is within
        threshold.
      • `(True, reason)` if `(baseline - current) / baseline >= threshold_pct`.

    Doesn't write to the history file — caller is expected to call
    `record_equity` separately. Separating the read and write keeps
    the check side-effect-free.
    """
    if current_equity <= 0:
        # Either a fresh wallet or a broken snapshot. The drawdown
        # check is meaningless without a positive baseline; let the
        # higher-up validator catch the zero-equity case.
        return False, None
    history_path = history_path or EQUITY_HISTORY_FILE
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=baseline_hours)

    entries = _read_history(history_path)
    # Find the NEWEST entry at-or-before the cutoff. Walking newest-to-
    # oldest in reverse-sorted order gives O(n) without an extra pass.
    baseline: Decimal | None = None
    for ts, eq in reversed(sorted(entries, key=lambda x: x[0])):
        if ts <= cutoff:
            baseline = eq
            break

    if baseline is None or baseline <= 0:
        return False, None

    drawdown = (baseline - current_equity) / baseline
    if drawdown >= Decimal(str(threshold_pct)):
        return True, (
            f"24h drawdown {float(drawdown):.2%} ≥ halt threshold "
            f"{threshold_pct:.0%} (baseline=${baseline:.2f} at "
            f"{cutoff.isoformat()} or earlier, current=${current_equity:.2f})"
        )
    return False, None
