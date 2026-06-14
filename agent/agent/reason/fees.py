"""Bybit trading-fee model — single source of truth for BOTH the agent's
net-yield economics (`agent.sandbox.snapshot`) and the web Earn-Explorer
profit numbers (`agent.api.server`). Keeps the real rates in one place so
the agent's friction and the UI's net-of-fees profit can't drift.

Base (VIP 0) rates:
  - Spot: 0.10% maker AND taker.
  - Linear perp (derivatives): 0.02% maker, 0.055% taker.

A hedged non-stable Earn position is delta-neutral: a spot leg (buy the coin
to stake) + a short perp leg. Entering and later exiting it pays a ROUND TRIP
of four taker fills — spot buy + spot sell + perp open + perp close. Earn
subscribe/redeem themselves are free, so a STABLE Earn pick (no spot/perp leg)
pays no trading fee.
"""

from __future__ import annotations

from decimal import Decimal

# ── Base VIP-0 rates (fraction of notional) ──────────────────────────────────
SPOT_FEE_RATE = Decimal("0.001")  # 0.10% maker = taker
PERP_MAKER_FEE_RATE = Decimal("0.0002")  # 0.02%
PERP_TAKER_FEE_RATE = Decimal("0.00055")  # 0.055%

# Round trip for a hedged non-stable: spot buy + spot sell + perp open + perp
# close, all taker (we cross the book to stay delta-neutral promptly).
HEDGED_ROUND_TRIP_FEE = 2 * SPOT_FEE_RATE + 2 * PERP_TAKER_FEE_RATE  # 0.0031 = 0.31%

# Annualized friction drag the agent subtracts in `effective_apr_net_hedge`
# (`gross_earn_apr + funding − friction`). Held positions rotate ~6×/yr on the
# weekly horizon with anti-churn discipline, so 0.31% round trip × ~6 ≈ 1.86%;
# held to a conservative 1.8% (rounded down — the value is intentionally stable
# so this re-homing doesn't shift live pick gating; recalibrate from realized
# P&L, not by retuning the rate here).
FUNDING_CARRY_FRICTION_ANNUAL = Decimal("0.018")


def round_trip_fee_fraction(*, is_stable: bool) -> float:
    """Fraction of notional paid to ENTER and later EXIT a position once.
    Stable Earn picks trade nothing (subscribe/redeem are free) → 0.0;
    non-stable picks pay the hedged spot+perp round trip."""
    return 0.0 if is_stable else float(HEDGED_ROUND_TRIP_FEE)
