"""Tests for the manual promo-APR whitelist.

The whitelist itself is data — tests focus on the lookup contract so
`.6` snapshot collector and the LLM ranker can rely on stable behavior.
"""

from decimal import Decimal

from agent.bybit_oracle.promo_whitelist import (
    PROMO_OVERRIDES,
    PromoOverride,
    get_promo_effective_apr,
    get_promo_override,
)


def test_usd1_whitelisted_at_7_52_percent():
    """USD1 productId=1131 must round-trip the documented headline APR
    (Phase A.18 observation: API gives 0.65% base / 0.5% apr-history,
    UI gives 7.52% under WLFI promo)."""
    apr = get_promo_effective_apr("FlexibleSaving", "1131")
    assert apr == Decimal("0.0752")


def test_missing_product_returns_none():
    assert get_promo_effective_apr("FlexibleSaving", "does-not-exist") is None
    assert get_promo_effective_apr("OnChain", "1131") is None  # wrong category


def test_get_promo_override_returns_full_entry_for_debug():
    """Caller needs source + last_checked for stale-warn logging."""
    entry = get_promo_override("FlexibleSaving", "1131")
    assert isinstance(entry, PromoOverride)
    assert entry.coin == "USD1"
    assert entry.effective_apr == Decimal("0.0752")
    assert entry.source_url.startswith("https://")
    assert entry.last_checked.year == 2026


def test_all_entries_use_fractional_apr_form():
    """Invariant: effective_apr is always Decimal in [0, 1] fractional
    form, never a percent-string or whole-number-percent. Ranker
    multiplies directly — a stray `7.52` would silently break ranking
    by 100x."""
    for entry in PROMO_OVERRIDES:
        assert isinstance(entry.effective_apr, Decimal), entry
        assert Decimal(0) <= entry.effective_apr <= Decimal(1), entry


def test_all_entries_have_source_and_recent_check_date():
    """A whitelist entry without a source URL or with a missing date is
    useless for demo prep — operator can't re-verify."""
    for entry in PROMO_OVERRIDES:
        assert entry.source_url.startswith("https://"), entry
        assert entry.last_checked is not None, entry


def test_no_duplicate_entries():
    """(category, product_id) is the lookup key — duplicates would
    silently overwrite in the by-key dict."""
    keys = [(e.category, e.product_id) for e in PROMO_OVERRIDES]
    assert len(keys) == len(set(keys))
