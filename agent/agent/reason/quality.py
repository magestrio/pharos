"""Shared coin-stability scoring — single source of truth for BOTH the agent
snapshot ranker (`agent.sandbox.snapshot`) and the web Earn-Explorer API
(`agent.api.server`). Pure functions over primitives so they're trivially
testable and can't drift between the two consumers.

Stability blends two signals into a 0..100 score:
  - APR steadiness: how flat the daily `apr_history` series is (low coefficient
    of variation → steady yield).
  - Price calm: how little the underlying coin's price has moved (low |7d move|).
A coin bleeding/pumping makes its Earn APR a trap, so price is weighted higher.
"""

from __future__ import annotations

import statistics

from agent.reason.venues import STABLES

# Weekly price move (%) that scores 0 price-stability; a flat coin scores 1.0.
STABILITY_VOL_FULL = 25.0
# APR-steadiness / price-calm blend weights (price dominates — see module doc).
_W_APR = 0.4
_W_PRICE = 0.6
# Floor of the ranking multiplier — a maximally-unstable product keeps this
# fraction of its rank-APR (moderate tilt: at most a 40% discount).
STABILITY_MULTIPLIER_FLOOR = 0.6


def is_stable(coin: str) -> bool:
    """True when `coin` (or BOTH legs of an LM `BASE/QUOTE` pair) is a canonical
    stablecoin. Stablecoins are price-stable by definition."""
    legs = [leg.strip().upper() for leg in coin.split("/") if leg.strip()]
    return bool(legs) and all(leg in STABLES for leg in legs)


def apr_steadiness(apr_history_pts: list[float] | None) -> float | None:
    """0..1 steadiness of the daily APR series via a bounded transform of the
    coefficient of variation. None when <2 points (no series to judge)."""
    if not apr_history_pts or len(apr_history_pts) < 2:
        return None
    mu = statistics.fmean(apr_history_pts)
    sd = statistics.pstdev(apr_history_pts)
    if abs(mu) < 1e-9 and sd < 1e-9:
        cv = 0.0  # all-zero APR → perfectly steady
    elif abs(mu) < 1e-9:
        cv = 10.0  # zero-mean but moving → max dispersion
    else:
        cv = sd / abs(mu)
    return 1.0 / (1.0 + 4.0 * cv)


def compute_stability(
    *,
    coin: str,
    apr_history_pts: list[float] | None,
    price_change_7d_pct: float | None,
    price_change_30d_pct: float | None,
) -> dict[str, float | None]:
    """Return `{apr_stability, price_volatility_pct, price_stability,
    stability_score}` (stability_score in 0..100). Degrades gracefully: when one
    signal is missing the other carries the score; when both are missing
    stability_score is None. Stablecoins get full price-calm (no perp / pegged)."""
    apr_stability = apr_steadiness(apr_history_pts)

    # Price volatility — 7d move, or 30d ÷4 as a weekly-equivalent fallback.
    if price_change_7d_pct is not None:
        price_volatility_pct: float | None = abs(price_change_7d_pct)
    elif price_change_30d_pct is not None:
        price_volatility_pct = abs(price_change_30d_pct) / 4.0
    else:
        price_volatility_pct = None

    if is_stable(coin):
        price_stability: float | None = 1.0
    elif price_volatility_pct is None:
        price_stability = None
    else:
        price_stability = max(0.0, 1.0 - price_volatility_pct / STABILITY_VOL_FULL)

    if apr_stability is not None and price_stability is not None:
        s: float | None = _W_APR * apr_stability + _W_PRICE * price_stability
    elif apr_stability is not None:
        s = apr_stability
    elif price_stability is not None:
        s = price_stability
    else:
        s = None

    return {
        "apr_stability": apr_stability,
        "price_volatility_pct": price_volatility_pct,
        "price_stability": price_stability,
        "stability_score": s * 100.0 if s is not None else None,
    }


def stability_multiplier(
    stability_score: float | None, *, floor: float = STABILITY_MULTIPLIER_FLOOR
) -> float:
    """Map a 0..100 stability score to a ranking multiplier in [floor, 1.0].
    None → 1.0: we only discount when there is EVIDENCE of instability, never
    demote a product blindly for lacking data."""
    if stability_score is None:
        return 1.0
    s = max(0.0, min(100.0, stability_score))
    return floor + (1.0 - floor) * (s / 100.0)
