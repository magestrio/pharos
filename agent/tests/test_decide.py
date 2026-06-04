"""`.39` tests for token + cost tracking in `agent.sandbox.decide`.

Covers `_estimate_cost_usd`, `_usage_from_response`, the `DecisionUsage`
shape, and `write_decision`'s persistence of the `_usage` sidecar.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.reason.schema import Decision, Pick, VenueAllocation
from agent.sandbox.decide import (
    DecisionUsage,
    _estimate_cost_usd,
    _usage_from_response,
    write_decision,
)


def _decision_clean() -> Decision:
    return Decision(
        thesis="Stub decision for usage-persistence tests.",
        venues=[
            VenueAllocation(venue_id="cash_usdc", weight=0.5),
            VenueAllocation(
                venue_id="bybit_flex",
                weight=0.5,
                picks=[Pick(product_id="1131", weight=1.0)],
            ),
        ],
        hedges=[],
        confidence=0.7,
        risk_flags=[],
        notes=[],
        expected_blended_apr_pct=4.0,
    )


# ─── _estimate_cost_usd ────────────────────────────────────────────────────


def test_estimate_cost_known_model_full_breakdown() -> None:
    """Sonnet 4.6 with 1h TTL cache (`.40`): 1k input + 10k cache_creation
    + 50k cache_read + 1k output → math against published pricing
    (×3 input / ×6 cache creation 1h / ×0.30 cache read / ×15 output,
    all per 1M tokens)."""
    cost = _estimate_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=1_000,
        cache_creation_input_tokens=10_000,
        cache_read_input_tokens=50_000,
        output_tokens=1_000,
    )
    # 1000×3 + 10000×6 + 50000×0.30 + 1000×15
    # = 3000 + 60000 + 15000 + 15000 = 93000 (USD × 1M scale)
    # = 93000 / 1_000_000 = $0.093
    assert cost == Decimal("0.093000")


def test_estimate_cost_unknown_model_returns_zero() -> None:
    """Unknown model → 0, surfacing the gap rather than fabricating
    a price from a default rate."""
    cost = _estimate_cost_usd(
        "claude-mystery-99",
        input_tokens=10_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=1_000,
    )
    assert cost == Decimal(0)


def test_estimate_cost_zero_tokens_zero_cost() -> None:
    cost = _estimate_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=0,
    )
    assert cost == Decimal(0)


def test_estimate_cost_cache_read_significantly_cheaper_than_input() -> None:
    """Cache read should be ~10% of input cost — the breakpoint that
    justifies prompt caching in the first place. Sanity check that the
    pricing table didn't drift to a flat rate."""
    input_only = _estimate_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=100_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=0,
    )
    cache_only = _estimate_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=100_000,
        output_tokens=0,
    )
    assert cache_only < input_only / 5  # at most 20% of input


def test_estimate_cost_opus_vs_sonnet_scale() -> None:
    """Opus input cost is 5× Sonnet — invariant the pricing table must
    hold so a model swap surfaces clearly in cycle metrics."""
    sonnet = _estimate_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=1_000_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=0,
    )
    opus = _estimate_cost_usd(
        "claude-opus-4-7",
        input_tokens=1_000_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=0,
    )
    assert opus == sonnet * 5


# ─── _usage_from_response ─────────────────────────────────────────────────


def test_usage_from_response_full_block() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=123,
            cache_creation_input_tokens=4_000,
            cache_read_input_tokens=10_000,
            output_tokens=456,
        )
    )
    usage = _usage_from_response(response, "claude-sonnet-4-6")
    assert usage.input_tokens == 123
    assert usage.cache_creation_input_tokens == 4_000
    assert usage.cache_read_input_tokens == 10_000
    assert usage.output_tokens == 456
    assert usage.model == "claude-sonnet-4-6"
    assert usage.estimated_cost_usd > Decimal(0)


def test_usage_from_response_missing_usage_block_zeros() -> None:
    """Defensive: response without a `usage` attribute (test mocks or
    partial SDK responses) must produce all-zero usage, not raise."""
    response = SimpleNamespace()  # no `usage` attribute
    usage = _usage_from_response(response, "claude-sonnet-4-6")
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.estimated_cost_usd == Decimal(0)


def test_usage_from_response_partial_fields_default_to_zero() -> None:
    """SDK sometimes omits the cache fields when caching is disabled.
    Missing attributes default to 0 rather than crashing on getattr."""
    response = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=999, output_tokens=10)
    )
    usage = _usage_from_response(response, "claude-sonnet-4-6")
    assert usage.input_tokens == 999
    assert usage.output_tokens == 10
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 0


# ─── DecisionUsage.to_dict ────────────────────────────────────────────────


def test_decision_usage_to_dict_round_trip() -> None:
    usage = DecisionUsage(
        model="claude-sonnet-4-6",
        input_tokens=100,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=300,
        output_tokens=50,
        estimated_cost_usd=Decimal("0.001234"),
    )
    d = usage.to_dict()
    assert d["model"] == "claude-sonnet-4-6"
    assert d["input_tokens"] == 100
    assert d["cache_creation_input_tokens"] == 200
    assert d["cache_read_input_tokens"] == 300
    assert d["output_tokens"] == 50
    # estimated_cost_usd is a string for JSON safety (Decimal isn't
    # natively serializable).
    assert d["estimated_cost_usd"] == "0.001234"
    # JSON round-trip
    blob = json.dumps(d)
    parsed = json.loads(blob)
    assert parsed == d


# ─── write_decision usage sidecar ─────────────────────────────────────────


def test_write_decision_persists_usage_sidecar(tmp_path: Path) -> None:
    """`.39`: when `usage` is passed, the persisted decision file must
    carry a `_usage` block so `.38` analyzer + post-mortem can join
    cost against outcome without re-reading every snapshot."""
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    decisions = tmp_path / "decisions"
    usage = DecisionUsage(
        model="claude-sonnet-4-6",
        input_tokens=15_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=14_500,
        output_tokens=320,
        estimated_cost_usd=Decimal("0.009350"),
    )
    out = write_decision(
        _decision_clean(),
        snap,
        decisions_dir=decisions,
        usage=usage,
        captured_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
    )
    payload = json.loads(out.read_text())
    assert payload["_usage"]["model"] == "claude-sonnet-4-6"
    assert payload["_usage"]["input_tokens"] == 15_000
    assert payload["_usage"]["cache_read_input_tokens"] == 14_500
    assert payload["_usage"]["output_tokens"] == 320
    assert payload["_usage"]["estimated_cost_usd"] == "0.009350"


def test_write_decision_without_usage_omits_sidecar(tmp_path: Path) -> None:
    """Auto-close path doesn't go through Anthropic — usage must be
    omitted entirely (not zeroed) so the analyzer can distinguish
    'LLM path with zero tokens' (a bug) from 'auto-close, no LLM call'
    (expected)."""
    snap = tmp_path / "snap.json"
    snap.write_text("{}")
    out = write_decision(
        _decision_clean(),
        snap,
        decisions_dir=tmp_path / "decisions",
        captured_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
    )
    payload = json.loads(out.read_text())
    assert "_usage" not in payload


def test_pricing_table_covers_models_referenced_by_decide() -> None:
    """Single source of truth invariant: the model decide() actually
    sends must have a pricing entry, otherwise every cycle silently
    reports $0 cost."""
    from agent.sandbox.decide import MODEL, _PRICING_PER_MTOK
    assert MODEL in _PRICING_PER_MTOK


# ─── .40 prompt-cache 1h TTL ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decide_sets_1h_cache_ttl_on_system_block() -> None:
    """`.40`: explicit 1h TTL on the system-block cache_control extends
    cache lifetime from 5min default to 60min, so event-driven cycles
    firing within an hour amortize the 2× write rate against
    cache-reads at 10% of input. Regression guard so a future refactor
    doesn't silently drop the TTL.
    """
    from unittest.mock import AsyncMock

    from agent.sandbox.decide import CACHE_TTL, TOOL_NAME, decide

    captured_kwargs: dict = {}

    async def _fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name=TOOL_NAME,
                    input={
                        "thesis": "stub thesis content for tool dispatch path",
                        "venues": [
                            {"venue_id": "cash_usdc", "weight": 1.0},
                        ],
                        "hedges": [],
                        "expected_blended_apr_pct": 0.0,
                        "confidence": 0.6,
                        "risk_flags": [],
                        "notes": [],
                    },
                )
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(
                input_tokens=10,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                output_tokens=5,
            ),
        )

    fake_client = AsyncMock()
    fake_client.messages.create = _fake_create

    snapshot = {"captured_at": "2026-06-04T00:00:00Z"}
    decision, usage = await decide(snapshot, client=fake_client)

    system_blocks = captured_kwargs.get("system") or []
    assert system_blocks, "decide() must pass a system block to Anthropic"
    cache_control = system_blocks[0].get("cache_control") or {}
    assert cache_control.get("type") == "ephemeral"
    assert cache_control.get("ttl") == CACHE_TTL == "1h"
