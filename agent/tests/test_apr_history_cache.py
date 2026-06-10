"""Tests for the daily apr-history cache (Part 3, anti-churn epic)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from agent.sandbox.apr_history_cache import (
    AprHistoryCache,
    cache_key,
    read_apr_history_cache,
    write_apr_history_cache,
)


_PTS = [Decimal("0.020"), Decimal("0.021"), Decimal("0.022")]


def test_get_hits_same_day_misses_other_day():
    c = AprHistoryCache()
    c.put("FlexibleSaving", "8", "2026-06-10", Decimal("0.0211"), _PTS)
    hit = c.get("FlexibleSaving", "8", "2026-06-10")
    assert hit == (Decimal("0.0211"), _PTS)
    # different UTC day → stale → miss
    assert c.get("FlexibleSaving", "8", "2026-06-11") is None
    # different product → miss
    assert c.get("FlexibleSaving", "9", "2026-06-10") is None


def test_roundtrip_disk_preserves_series(tmp_path: Path):
    p = tmp_path / "apr_cache.json"
    assert read_apr_history_cache(p) == AprHistoryCache()  # missing → empty
    c = AprHistoryCache()
    c.put("OnChain", "25", "2026-06-10", Decimal("0.0357"), _PTS)
    write_apr_history_cache(c, p)
    loaded = read_apr_history_cache(p)
    mean, points = loaded.get("OnChain", "25", "2026-06-10")
    assert mean == Decimal("0.0357")
    assert points == _PTS  # trajectory survives the round-trip


def test_corrupt_file_degrades_to_empty(tmp_path: Path):
    p = tmp_path / "apr_cache.json"
    p.write_text("{ not json")
    assert read_apr_history_cache(p) == AprHistoryCache()


def test_prune_drops_delisted_keys():
    c = AprHistoryCache()
    c.put("FlexibleSaving", "8", "2026-06-10", Decimal("0.02"), _PTS)
    c.put("FlexibleSaving", "9", "2026-06-10", Decimal("0.03"), _PTS)
    pruned = c.prune({cache_key("FlexibleSaving", "8")})
    assert pruned.get("FlexibleSaving", "8", "2026-06-10") == (Decimal("0.02"), _PTS)
    assert pruned.get("FlexibleSaving", "9", "2026-06-10") is None
