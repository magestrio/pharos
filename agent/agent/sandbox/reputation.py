"""Honest realized-APR reputation for the canonical ERC-8004 registry.

The deployed `ReputationOracle` derives its score from `vUSDC.exchangeRate()`
growth — but the on-chain vault is empty (no public deposits), so that path
never initializes (`canUpdate()` stays false, `updateReputation()` reverts
`VaultEmpty`). Reputation here instead reflects the agent's REAL track
record: the annualized return of the live Bybit book, read from the
persisted equity history (`safety.record_equity` → `equity_history.jsonl`).

The return is **money-weighted (Modified Dietz)**, NOT time-weighted. A
time-weighted return neutralizes the timing of deposits/withdrawals — which
sounds desirable but at this book size MASKS the dollar truth: a small gain
on a small base before a top-up, then a larger dollar loss on the bigger
base after it, time-weights to a positive % while the account is actually
down in dollars. Money-weighting keeps the sign honest: if the agent lost
money, the APR is negative. Cash flows are detected as near-instant equity
jumps, subtracted from the numerator and time-weighted in the denominator,
so a deposit never reads as performance.

`compute_realized_apr_bps` is a pure function over the equity series so it
is unit-testable without the file system or a chain. The loop's heartbeat
(`loop._push_reputation_heartbeat`) reads the history, computes the score,
and `OnchainWriter.push_apr_reputation` attests it via
`giveFeedback(agentId, scoreBps, ...)` straight from the agent EOA — no
oracle contract, no vault coupling.

The number is signed and honest: at small book sizes trading friction (and
LLM mistakes) can exceed Earn yield, so a negative APR is a faithful
reading, not a bug.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

log = logging.getLogger(__name__)

# Annualization constants mirror the on-chain ReputationOracle so the
# off-chain score uses the same (simple, not compounded) convention:
#   apr_bps = (current - baseline) / baseline * SECONDS_PER_YEAR / elapsed * 1e4
# VALUE_DECIMALS=2 → 1234 bps reads as 12.34% in the registry.
_SECONDS_PER_YEAR = Decimal(365 * 24 * 60 * 60)
_BPS_SCALE = Decimal(10_000)

# `giveFeedback` takes an int128 — clamp defensively so a degenerate short
# window can't overflow the on-chain type.
_INT128_MAX = 2**127 - 1
_INT128_MIN = -(2**127)

# Don't annualize a window shorter than this. Simple annualization of a
# young window turns ordinary noise into an absurd yearly figure, so require
# a few days of real track record before quoting an APR.
MIN_WINDOW_HOURS = Decimal(24 * 3)

# A near-instant equity jump is an external CASH FLOW (deposit/withdrawal),
# not performance — a step exceeding FLOW_THRESHOLD that lands within
# FLOW_MAX_GAP. At cycle cadence real yield/friction is cents, so a
# multi-percent jump in minutes is unmistakably a transfer. Modified Dietz
# subtracts the flow from the numerator and time-weights it in the
# denominator, so a $100 top-up can't read as a +130% gain. The time gate
# keeps a genuine slow move (large % over many days of sparse sampling)
# from being mistaken for a flow.
FLOW_THRESHOLD = Decimal("0.05")
FLOW_MAX_GAP = timedelta(hours=6)

# Off-chain throttle: the canonical registry has no MIN_INTERVAL of its own,
# and event-driven cycles can fire every couple of minutes — pushing every
# cycle would burn gas for no new signal. One hour mirrors the retired
# ReputationOracle.MIN_INTERVAL.
PUSH_INTERVAL = timedelta(hours=1)

_STATE_DIR = Path(__file__).parent / "state"
LAST_PUSH_FILE: Path = _STATE_DIR / "reputation_push.json"


@dataclass(frozen=True)
class ReputationScore:
    """Result of annualizing the equity series. `apr_bps` is signed and
    derived from the money-weighted (Modified Dietz) period return."""

    apr_bps: int
    period_return: Decimal
    baseline_equity: Decimal
    current_equity: Decimal
    baseline_ts: datetime
    current_ts: datetime
    elapsed_seconds: Decimal
    n_points: int
    n_flows: int


def compute_realized_apr_bps(
    history: list[tuple[datetime, Decimal]],
    *,
    min_window_hours: Decimal = MIN_WINDOW_HOURS,
    flow_threshold: Decimal = FLOW_THRESHOLD,
    flow_max_gap: timedelta = FLOW_MAX_GAP,
) -> ReputationScore | None:
    """Annualized realized return (signed bps) from the equity series.

    Money-weighted (**Modified Dietz**): the period return is

        R = (EMV - BMV - net_flows) / (BMV + Σ flow_i * weight_i)

    where each cash flow (a jump > `flow_threshold` within `flow_max_gap`)
    is weighted by the fraction of the window remaining after it. R is then
    simple-annualized over the full window — the same annualization
    convention as the on-chain ReputationOracle. Unlike a time-weighted
    return, R's sign tracks the actual dollar P&L, so a losing book reads
    negative even if a deposit landed mid-window.

    Returns `None` when there isn't enough honest signal to attest: fewer
    than two positive points, a non-positive baseline, a zero-length
    window, a window shorter than `min_window_hours`, or a non-positive
    average-capital denominator (e.g. the book was fully withdrawn).
    """
    points = sorted(
        ((ts, eq) for ts, eq in history if eq > 0), key=lambda x: x[0]
    )
    if len(points) < 2:
        return None

    baseline_ts, baseline_eq = points[0]
    current_ts, current_eq = points[-1]

    elapsed = Decimal((current_ts - baseline_ts).total_seconds())
    if elapsed <= 0:
        return None
    if elapsed < min_window_hours * Decimal(3600):
        return None

    net_flows = Decimal(0)
    weighted_flows = Decimal(0)
    n_flows = 0
    for (t0, e0), (t1, e1) in zip(points, points[1:], strict=False):
        step = (e1 - e0) / e0
        if abs(step) > flow_threshold and (t1 - t0) <= flow_max_gap:
            flow = e1 - e0
            weight = Decimal((current_ts - t1).total_seconds()) / elapsed
            net_flows += flow
            weighted_flows += flow * weight
            n_flows += 1

    denom = baseline_eq + weighted_flows
    if denom <= 0:
        return None

    period_return = (current_eq - baseline_eq - net_flows) / denom
    apr = period_return * _SECONDS_PER_YEAR / elapsed * _BPS_SCALE
    apr_bps = int(apr.to_integral_value(rounding=ROUND_HALF_UP))
    apr_bps = max(_INT128_MIN, min(_INT128_MAX, apr_bps))

    return ReputationScore(
        apr_bps=apr_bps,
        period_return=period_return,
        baseline_equity=baseline_eq,
        current_equity=current_eq,
        baseline_ts=baseline_ts,
        current_ts=current_ts,
        elapsed_seconds=elapsed,
        n_points=len(points),
        n_flows=n_flows,
    )


def _read_last_push(path: Path) -> datetime | None:
    """Timestamp of the last successful attestation, or `None` (never
    pushed / unreadable → treat as due)."""
    if not path.exists():
        return None
    try:
        ts = json.loads(path.read_text()).get("ts")
        return datetime.fromisoformat(ts) if ts else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def should_push(
    *, now: datetime | None = None, path: Path | None = None
) -> bool:
    """True when at least `PUSH_INTERVAL` has elapsed since the last
    successful attestation (or none has happened yet)."""
    now = now or datetime.now(UTC)
    last = _read_last_push(path or LAST_PUSH_FILE)
    return last is None or (now - last) >= PUSH_INTERVAL


def record_push(
    score: ReputationScore,
    tx_hash: str,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> None:
    """Persist the last-push timestamp + a small audit record. Called only
    after a confirmed tx so a failed send retries next cycle. Best-effort:
    a write failure just means the next cycle may re-push early."""
    path = path or LAST_PUSH_FILE
    now = now or datetime.now(UTC)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "ts": now.isoformat(),
            "apr_bps": score.apr_bps,
            "period_return": str(score.period_return),
            "tx": tx_hash,
            "baseline_equity": str(score.baseline_equity),
            "current_equity": str(score.current_equity),
            "elapsed_days": round(float(score.elapsed_seconds) / 86400, 2),
            "n_points": score.n_points,
            "n_flows": score.n_flows,
        }, indent=2))
    except OSError as e:
        log.warning("reputation_push state write failed: %s", e)
