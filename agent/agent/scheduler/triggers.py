from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


# Conversion: a funding rate of 0.0001 (Bybit convention, dimensionless
# per-8h fraction) equals 1 bps. So bps = funding * 10_000.
FUNDING_TO_BPS = 10_000.0


@dataclass
class TriggerOutcome:
    fire: bool
    reason: str


@dataclass
class TriggerEvaluator:
    """Pure-logic gate for cycle triggers. No I/O, no clock — `now` is
    passed in so tests are deterministic.

    Thresholds are absolute differences (bps or fraction) against the
    last-seen value per signal. A signal first observed always fires
    (we have no baseline; one extra cycle is the safe choice).

    A successful decision must call `mark_decision_taken(now)` — this
    arms the cooldown that suppresses all subsequent triggers until
    `cooldown_minutes` have elapsed. Cron heartbeats bypass the
    evaluator entirely and never enter the cooldown gate."""

    funding_threshold_bps: float = 50.0
    aave_util_threshold: float = 0.05
    peg_threshold_bps: float = 100.0
    cooldown_minutes: float = 30.0

    last_funding_bps_per_symbol: dict[str, float] = field(default_factory=dict)
    last_aave_util_per_pool: dict[str, float] = field(default_factory=dict)
    last_peg_bps: float | None = None
    last_decision_at: datetime | None = None

    def _in_cooldown(self, now: datetime) -> bool:
        if self.last_decision_at is None:
            return False
        return now - self.last_decision_at < timedelta(minutes=self.cooldown_minutes)

    def mark_decision_taken(self, now: datetime) -> None:
        self.last_decision_at = now

    def evaluate_funding(self, symbol: str, funding_rate: float, now: datetime) -> TriggerOutcome:
        """`funding_rate` is the raw Bybit number (e.g. 0.0001 = 1bps/8h)."""
        if self._in_cooldown(now):
            return TriggerOutcome(False, "cooldown")
        current_bps = funding_rate * FUNDING_TO_BPS
        prev_bps = self.last_funding_bps_per_symbol.get(symbol)
        self.last_funding_bps_per_symbol[symbol] = current_bps
        if prev_bps is None:
            return TriggerOutcome(True, f"funding[{symbol}] first observation ({current_bps:.1f}bps)")
        delta_bps = abs(current_bps - prev_bps)
        if delta_bps >= self.funding_threshold_bps:
            return TriggerOutcome(
                True,
                f"funding[{symbol}] {prev_bps:.1f}->{current_bps:.1f}bps (|Δ|={delta_bps:.1f}bps)",
            )
        return TriggerOutcome(False, f"funding[{symbol}] |Δ|={delta_bps:.1f}bps below {self.funding_threshold_bps:.0f}bps")

    def evaluate_aave_util(self, pool: str, utilization: float, now: datetime) -> TriggerOutcome:
        if self._in_cooldown(now):
            return TriggerOutcome(False, "cooldown")
        prev = self.last_aave_util_per_pool.get(pool)
        self.last_aave_util_per_pool[pool] = utilization
        if prev is None:
            return TriggerOutcome(True, f"aave_util[{pool}] first observation ({utilization:.2%})")
        delta = abs(utilization - prev)
        if delta >= self.aave_util_threshold:
            return TriggerOutcome(
                True,
                f"aave_util[{pool}] {prev:.2%}->{utilization:.2%} (|Δ|={delta:.2%})",
            )
        return TriggerOutcome(False, f"aave_util[{pool}] |Δ|={delta:.2%} below {self.aave_util_threshold:.0%}")

    def evaluate_peg(self, peg_bps: float, now: datetime) -> TriggerOutcome:
        """USDC peg uses an *absolute* gate, not a delta — any deviation
        above the threshold is itself the signal worth acting on."""
        if self._in_cooldown(now):
            return TriggerOutcome(False, "cooldown")
        self.last_peg_bps = peg_bps
        if peg_bps >= self.peg_threshold_bps:
            return TriggerOutcome(True, f"usdc_peg={peg_bps:.0f}bps >= {self.peg_threshold_bps:.0f}bps")
        return TriggerOutcome(False, f"usdc_peg={peg_bps:.0f}bps below {self.peg_threshold_bps:.0f}bps")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
