"""Daily cache for Bybit Earn apr-history (Part 3, anti-churn epic).

`/v5/earn/apr-history` is DAILY granularity — one APR point per UTC day — yet
`_measure_apr_history` is fanned out for EVERY FlexibleSaving + OnChain
candidate on every snapshot (~60 calls), including the ~120s event-driven
cycles. Since the underlying data only changes once per UTC day, we cache the
computed mean AND the daily point series keyed by `(category, productId)` with
the UTC date it was computed: a cache hit on the same day skips the call.

We keep the daily series (not just the mean) so the agent sees the APR
TRAJECTORY — a held product whose daily APR is sliding down is an exit signal;
a stable/rising series supports holding longer (anti-churn). The mean stays the
headline value; the points are the shape.

State file lives at `sandbox/state/apr_history_cache.json`, rewritten
atomically (tmp+rename) — same pattern as `lm_redeem_cooldown` / `carry_state`.
Missing or corrupt file → empty cache; the worst case is one day of re-fetching,
which is exactly today's behavior, so losing it is harmless.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_APR_HISTORY_CACHE_PATH = (
    Path(__file__).parent / "state" / "apr_history_cache.json"
)


def _to_decimal(v: object) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _key(category: str, product_id: str) -> str:
    return f"{category}/{product_id}"


class AprHistoryEntry(BaseModel):
    """One cached product: the UTC date computed, the headline mean, and the
    daily point series (oldest → newest) that forms the trajectory."""

    model_config = ConfigDict(extra="ignore")

    utc_date: str
    mean_apr: str
    points: list[str] = Field(default_factory=list)


class AprHistoryCache(BaseModel):
    """`"CATEGORY/PID"` → `AprHistoryEntry`."""

    model_config = ConfigDict(extra="ignore")

    entries: dict[str, AprHistoryEntry] = Field(default_factory=dict)

    def get(
        self, category: str, product_id: str, today: str
    ) -> tuple[Decimal, list[Decimal]] | None:
        """Cached `(mean, points)` for today, or None on miss / stale day."""
        rec = self.entries.get(_key(category, product_id))
        if rec is None or rec.utc_date != today:
            return None
        mean = _to_decimal(rec.mean_apr)
        if mean is None:
            return None
        points = [d for d in (_to_decimal(p) for p in rec.points) if d is not None]
        return mean, points

    def put(
        self,
        category: str,
        product_id: str,
        today: str,
        mean_apr: Decimal,
        points: list[Decimal],
    ) -> None:
        """Store a freshly-computed mean + daily series against today's UTC date."""
        self.entries[_key(category, product_id)] = AprHistoryEntry(
            utc_date=today,
            mean_apr=str(mean_apr),
            points=[str(p) for p in points],
        )

    def prune(self, live_keys: set[str]) -> AprHistoryCache:
        """Drop entries for products no longer in the candidate set — keeps the
        file bounded as Bybit lists/delists products."""
        kept = {k: v for k, v in self.entries.items() if k in live_keys}
        return AprHistoryCache(entries=kept)


def read_apr_history_cache(
    path: Path = DEFAULT_APR_HISTORY_CACHE_PATH,
) -> AprHistoryCache:
    """Load cache from disk. Missing file / parse error → empty (derived state
    whose worst-case loss is one extra day of re-fetching)."""
    if not path.exists():
        return AprHistoryCache()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return AprHistoryCache()
    try:
        return AprHistoryCache.model_validate(raw)
    except Exception:  # noqa: BLE001
        return AprHistoryCache()


def write_apr_history_cache(
    cache: AprHistoryCache, path: Path = DEFAULT_APR_HISTORY_CACHE_PATH
) -> None:
    """Atomic write — tmp+rename so a partial write never leaves half-parsed
    JSON on disk (same pattern as `write_lm_redeem_cooldown`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(cache.model_dump_json(indent=2))
    os.replace(tmp, path)


def cache_key(category: str, product_id: str) -> str:
    """Public helper so callers can build the live-key set for `prune`."""
    return _key(category, product_id)
