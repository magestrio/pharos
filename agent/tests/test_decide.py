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


def _stub_create_capturing(captured_kwargs: dict):
    """Build a fake `messages.create` that records its kwargs and returns
    a minimal valid `submit_decision` tool call."""
    from agent.sandbox.decide import TOOL_NAME

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

    return _fake_create


@pytest.mark.asyncio
async def test_decide_sets_1h_cache_ttl_on_event_driven_cycle() -> None:
    """`.40`: event-driven cycles cache the system block at the 1h TTL so
    a burst firing within the hour amortizes the 2× write against
    cache-reads at 10% of input. Gated on `wake_events` (2026-06-19).
    """
    from unittest.mock import AsyncMock

    from agent.sandbox.decide import CACHE_TTL, decide

    captured_kwargs: dict = {}
    fake_client = AsyncMock()
    fake_client.messages.create = _stub_create_capturing(captured_kwargs)

    snapshot = {"captured_at": "2026-06-04T00:00:00Z"}
    await decide(
        snapshot,
        client=fake_client,
        wake_events=[{"severity": "P0", "kind": "peg_drift", "message": "x"}],
    )

    system_blocks = captured_kwargs.get("system") or []
    assert system_blocks, "decide() must pass a system block to Anthropic"
    cache_control = system_blocks[0].get("cache_control") or {}
    assert cache_control.get("type") == "ephemeral"
    assert cache_control.get("ttl") == CACHE_TTL == "1h"


@pytest.mark.asyncio
async def test_decide_skips_cache_on_heartbeat_cycle() -> None:
    """Heartbeat cycles (no `wake_events`) are 8h apart — they never read
    the cache back, so writing it would just double the system-block
    input cost. Guard that no cache_control is sent (2026-06-19)."""
    from unittest.mock import AsyncMock

    from agent.sandbox.decide import decide

    captured_kwargs: dict = {}
    fake_client = AsyncMock()
    fake_client.messages.create = _stub_create_capturing(captured_kwargs)

    snapshot = {"captured_at": "2026-06-04T00:00:00Z"}
    await decide(snapshot, client=fake_client)

    system_blocks = captured_kwargs.get("system") or []
    assert system_blocks, "decide() must pass a system block to Anthropic"
    assert "cache_control" not in system_blocks[0]


# ─── .4 memory layer (multi-cycle prior decisions) ────────────────────────


def _decision_blob(
    *,
    ts: str,
    venues: list[dict],
    confidence: float = 0.7,
    thesis: str = "test thesis",
    validator_ok: bool | None = True,
    validator_errors: list[str] | None = None,
) -> dict:
    """Mimic the on-disk shape `write_decision` produces — venues + meta
    + validator sidecar. Used to seed `_load_recent_prior_decisions`
    fixture files and to feed `_summarize_prior_decisions` directly."""
    blob: dict = {
        "thesis": thesis,
        "venues": venues,
        "hedges": [],
        "confidence": confidence,
        "risk_flags": [],
        "notes": [],
        "expected_blended_apr_pct": 4.0,
        "_meta": {
            "snapshot_filename": f"{ts}.json",
            "written_at": "2026-06-04T12:00:00+00:00",
            "model": "claude-sonnet-4-6",
            "prompt_version": "reason.prompt",
            "wake_reason": "heartbeat",
        },
    }
    if validator_ok is not None or validator_errors:
        blob["_validator"] = {"ok": validator_ok, "errors": validator_errors or []}
    return blob


def test_load_recent_prior_decisions_empty_dir_returns_empty(tmp_path: Path) -> None:
    """Missing or empty directory → []. First-cycle cold start path."""
    from agent.sandbox.decide import _load_recent_prior_decisions

    # Missing dir
    assert _load_recent_prior_decisions(tmp_path / "nope") == []
    # Empty dir
    (tmp_path / "decisions").mkdir()
    assert _load_recent_prior_decisions(tmp_path / "decisions") == []


def test_load_recent_prior_decisions_returns_oldest_to_newest(tmp_path: Path) -> None:
    """Files named `<ts>.json` sort lexicographically = chronologically;
    loader returns them oldest → newest so concatenating reads as a
    trajectory."""
    from agent.sandbox.decide import _load_recent_prior_decisions

    d = tmp_path / "decisions"
    d.mkdir()
    for ts in ["20260604T080000Z", "20260604T120000Z", "20260604T160000Z"]:
        (d / f"{ts}.json").write_text(
            json.dumps(_decision_blob(ts=ts, venues=[{"venue_id": "cash_usdc", "weight": 1.0}]))
        )
    out = _load_recent_prior_decisions(d, n=3)
    timestamps = [b["_meta"]["snapshot_filename"] for b in out]
    assert timestamps == [
        "20260604T080000Z.json",
        "20260604T120000Z.json",
        "20260604T160000Z.json",
    ]


def test_load_recent_prior_decisions_caps_at_n(tmp_path: Path) -> None:
    """5 files, n=2 → return the 2 most recent, still oldest → newest."""
    from agent.sandbox.decide import _load_recent_prior_decisions

    d = tmp_path / "decisions"
    d.mkdir()
    for hh in [8, 10, 12, 14, 16]:
        ts = f"20260604T{hh:02d}0000Z"
        (d / f"{ts}.json").write_text(
            json.dumps(_decision_blob(ts=ts, venues=[{"venue_id": "cash_usdc", "weight": 1.0}]))
        )
    out = _load_recent_prior_decisions(d, n=2)
    timestamps = [b["_meta"]["snapshot_filename"] for b in out]
    assert timestamps == ["20260604T140000Z.json", "20260604T160000Z.json"]


def test_load_recent_prior_decisions_skips_corrupt_files(tmp_path: Path) -> None:
    """A corrupt JSON row should not break the cycle — skip silently and
    keep accumulating valid rows up to n."""
    from agent.sandbox.decide import _load_recent_prior_decisions

    d = tmp_path / "decisions"
    d.mkdir()
    (d / "20260604T080000Z.json").write_text(
        json.dumps(_decision_blob(ts="20260604T080000Z", venues=[{"venue_id": "cash_usdc", "weight": 1.0}]))
    )
    (d / "20260604T120000Z.json").write_text("{ not valid json")  # corrupt
    (d / "20260604T160000Z.json").write_text(
        json.dumps(_decision_blob(ts="20260604T160000Z", venues=[{"venue_id": "cash_usdc", "weight": 1.0}]))
    )
    out = _load_recent_prior_decisions(d, n=3)
    timestamps = [b["_meta"]["snapshot_filename"] for b in out]
    assert timestamps == ["20260604T080000Z.json", "20260604T160000Z.json"]


def test_load_latest_prior_decision_wraps_recent_loader(tmp_path: Path) -> None:
    """`_load_latest_prior_decision` is a thin wrapper used by the
    auto-close fast-path which only needs the most recent prior; must
    return the same dict as `_load_recent_prior_decisions(n=1)[-1]`."""
    from agent.sandbox.decide import (
        _load_latest_prior_decision,
        _load_recent_prior_decisions,
    )

    d = tmp_path / "decisions"
    d.mkdir()
    for ts in ["20260604T080000Z", "20260604T120000Z"]:
        (d / f"{ts}.json").write_text(
            json.dumps(_decision_blob(ts=ts, venues=[{"venue_id": "cash_usdc", "weight": 1.0}]))
        )
    latest = _load_latest_prior_decision(d)
    recent = _load_recent_prior_decisions(d, n=1)
    assert latest is not None
    assert recent == [latest]
    assert latest["_meta"]["snapshot_filename"] == "20260604T120000Z.json"


def test_summarize_prior_decisions_empty_list_returns_empty_string() -> None:
    """Cold start (no priors) renders nothing — `_build_user_message` then
    skips the entire 'Recent decisions' section."""
    from agent.sandbox.decide import _summarize_prior_decisions

    assert _summarize_prior_decisions([]) == ""


def test_summarize_prior_decisions_renders_each_cycle_with_separator() -> None:
    """Multi-cycle digest = per-decision summaries joined by blank lines.
    Each cycle's `_summarize_prior_decision` head ([ts]) must appear,
    preserving input order (oldest → newest)."""
    from agent.sandbox.decide import _summarize_prior_decisions

    d1 = _decision_blob(
        ts="20260604T080000Z",
        venues=[{"venue_id": "cash_usdc", "weight": 1.0}],
        thesis="cold start, all cash",
    )
    d2 = _decision_blob(
        ts="20260604T120000Z",
        venues=[
            {"venue_id": "cash_usdc", "weight": 0.5},
            {
                "venue_id": "bybit_flex",
                "weight": 0.5,
                "picks": [{"product_id": "1131", "weight": 1.0}],
            },
        ],
        thesis="rotate into Bybit USDC Flex",
    )
    out = _summarize_prior_decisions([d1, d2])
    # Order preserved: oldest first.
    idx1 = out.index("[20260604T080000Z]")
    idx2 = out.index("[20260604T120000Z]")
    assert idx1 < idx2
    # Each thesis carried.
    assert "cold start, all cash" in out
    assert "rotate into Bybit USDC Flex" in out
    # Pick details serialized on the multi-pick cycle.
    assert "1131@1.00" in out
    # Separator between cycles is a blank line.
    assert "\n\n" in out


def test_summarize_prior_decision_surfaces_validator_rejection() -> None:
    """When prior was rejected, the summary must shout the errors so
    Claude doesn't repeat the same picks/sizing (`.47` feedback loop)."""
    from agent.sandbox.decide import _summarize_prior_decision

    rejected = _decision_blob(
        ts="20260604T120000Z",
        venues=[{"venue_id": "bybit_onchain", "weight": 0.95}],
        thesis="overweight OnChain",
        validator_ok=False,
        validator_errors=[
            "bybit_onchain weight 0.95 > max_weight 0.70",
            "cash_usdc 0.05 below min_weight 0.10",
        ],
    )
    out = _summarize_prior_decision(rejected)
    assert "VALIDATOR REJECTED" in out
    assert "bybit_onchain weight 0.95 > max_weight 0.70" in out
    assert "cash_usdc 0.05 below min_weight 0.10" in out


def test_summarize_prior_decision_marks_passed_validator() -> None:
    """Happy path renders a quiet `✓ validator passed` so Claude knows
    the prior shape was acceptable (vs absent validator info)."""
    from agent.sandbox.decide import _summarize_prior_decision

    ok = _decision_blob(
        ts="20260604T120000Z",
        venues=[{"venue_id": "cash_usdc", "weight": 1.0}],
    )
    out = _summarize_prior_decision(ok)
    assert "validator passed" in out


def test_summarize_prior_decision_truncates_long_thesis() -> None:
    """Per-cycle thesis cap (300 chars) keeps the multi-cycle digest
    bounded; bigger N × full thesis would balloon the user message."""
    from agent.sandbox.decide import _summarize_prior_decision

    long_thesis = "x" * 500
    blob = _decision_blob(
        ts="20260604T120000Z",
        venues=[{"venue_id": "cash_usdc", "weight": 1.0}],
        thesis=long_thesis,
    )
    out = _summarize_prior_decision(blob)
    # Must contain the truncation marker and not the full original.
    assert "…" in out
    assert "x" * 500 not in out


def test_trim_snapshot_drops_missing_apr_products() -> None:
    """`apr_source == "missing"` products are un-pickable (validator
    rejects any non-zero weight), so they're stripped from the LLM
    payload to save input tokens. Pickable rows and category keys
    survive; the on-disk snapshot is untouched (function returns a copy).
    """
    from agent.sandbox.decide import _trim_snapshot_for_llm

    snapshot = {
        "captured_at": "2026-06-05T00:00:00Z",
        "products": {
            "FlexibleSaving": [
                {"product_id": "1", "apr_source": "list", "effective_apr": "0.05"},
                {"product_id": "2", "apr_source": "missing", "effective_apr": "0"},
            ],
            "DoubleWin": [
                {"product_id": "9", "apr_source": "missing", "effective_apr": "0"},
            ],
        },
    }
    trimmed = _trim_snapshot_for_llm(snapshot)

    flex = trimmed["products"]["FlexibleSaving"]
    assert [p["product_id"] for p in flex] == ["1"]
    # Entirely-missing category collapses to an empty list but the key
    # stays so the model still sees the family exists.
    assert trimmed["products"]["DoubleWin"] == []
    # Original snapshot must not be mutated.
    assert len(snapshot["products"]["FlexibleSaving"]) == 2


def test_build_user_message_serializes_snapshot_compactly() -> None:
    """The snapshot JSON must be minified (no indent) — pretty-printing
    roughly doubles the input token bill on whitespace alone."""
    from agent.sandbox.decide import _build_user_message

    snapshot = {
        "captured_at": "2026-06-05T00:00:00Z",
        "products": {"FlexibleSaving": [{"product_id": "1", "apr_source": "list"}]},
    }
    msg = _build_user_message(snapshot)
    # Compact separators leave no ", " or ": " spacing and no newline-indent.
    assert '"product_id":"1"' in msg
    assert '\n    "' not in msg


def test_build_user_message_includes_recent_decisions_section() -> None:
    """The 'Recent decisions' header must appear when prior_decisions is
    a non-empty list, and the contained timestamps must be present.
    This is the integration point Claude reads each cycle."""
    from agent.sandbox.decide import _build_user_message

    priors = [
        _decision_blob(
            ts="20260604T080000Z",
            venues=[{"venue_id": "cash_usdc", "weight": 1.0}],
            thesis="first cycle, all cash",
        ),
        _decision_blob(
            ts="20260604T120000Z",
            venues=[
                {"venue_id": "cash_usdc", "weight": 0.3},
                {
                    "venue_id": "bybit_flex",
                    "weight": 0.7,
                    "picks": [{"product_id": "1131", "weight": 1.0}],
                },
            ],
            thesis="rotate into Flex USDC at 5.12% APR",
        ),
    ]
    msg = _build_user_message({"captured_at": "2026-06-04T16:00:00Z"}, priors)
    assert "Recent decisions" in msg
    assert "20260604T080000Z" in msg
    assert "20260604T120000Z" in msg


def test_build_user_message_skips_section_when_no_priors() -> None:
    """Cold start path: empty list / None → no 'Recent decisions' header
    in the user message at all (keeps first-cycle context clean)."""
    from agent.sandbox.decide import _build_user_message

    msg_none = _build_user_message({"captured_at": "x"}, None)
    msg_empty = _build_user_message({"captured_at": "x"}, [])
    assert "Recent decisions" not in msg_none
    assert "Recent decisions" not in msg_empty


# ─── cycle_log outcome join (`mainnet-operations.4`) ───────────────────────


def _cycle_log_entry(
    *,
    decision_filename: str,
    result: str,
    actions_planned: int | None = None,
    actions_executed: int | None = None,
    wake_reason: str = "heartbeat",
) -> dict:
    """Minimal cycle_log.jsonl row — mirrors what `run_one_cycle` writes."""
    entry: dict = {
        "decision_filename": decision_filename,
        "snapshot_filename": decision_filename,
        "result": result,
        "wake_reason": wake_reason,
    }
    if actions_planned is not None:
        entry["actions_planned"] = actions_planned
    if actions_executed is not None:
        entry["actions_executed"] = actions_executed
    return entry


def test_load_recent_prior_decisions_joins_cycle_outcome(tmp_path: Path) -> None:
    """When a cycle_log.jsonl entry matches a decision file by name, the
    loader attaches a `_cycle_outcome` slice (result, actions counts,
    wake_reason). Validator outcome lives in the decision _meta sidecar;
    cycle outcome adds the executor's perspective — what actually fired."""
    from agent.sandbox.decide import _load_recent_prior_decisions

    d = tmp_path / "decisions"
    d.mkdir()
    for ts in ["20260604T080000Z", "20260604T120000Z"]:
        (d / f"{ts}.json").write_text(
            json.dumps(_decision_blob(ts=ts, venues=[{"venue_id": "cash_usdc", "weight": 1.0}]))
        )
    cycle_log = tmp_path / "cycle_log.jsonl"
    cycle_log.write_text(
        json.dumps(
            _cycle_log_entry(
                decision_filename="20260604T080000Z.json",
                result="executed",
                actions_planned=3,
                actions_executed=3,
            )
        )
        + "\n"
        + json.dumps(
            _cycle_log_entry(
                decision_filename="20260604T120000Z.json",
                result="executed_partial",
                actions_planned=5,
                actions_executed=3,
                wake_reason="event:funding_flip",
            )
        )
        + "\n"
    )
    out = _load_recent_prior_decisions(d, n=2, cycle_log_path=cycle_log)
    assert len(out) == 2
    assert out[0]["_cycle_outcome"] == {
        "result": "executed",
        "actions_planned": 3,
        "actions_executed": 3,
        "actions_failed": None,
        "wake_reason": "heartbeat",
    }
    assert out[1]["_cycle_outcome"] == {
        "result": "executed_partial",
        "actions_planned": 5,
        "actions_executed": 3,
        "actions_failed": None,
        "wake_reason": "event:funding_flip",
    }


def test_load_recent_prior_decisions_missing_cycle_log_no_join(tmp_path: Path) -> None:
    """No cycle_log.jsonl present (fresh deployment, cleared logs) →
    decisions returned without `_cycle_outcome` annotation; loader does
    not raise."""
    from agent.sandbox.decide import _load_recent_prior_decisions

    d = tmp_path / "decisions"
    d.mkdir()
    (d / "20260604T120000Z.json").write_text(
        json.dumps(_decision_blob(ts="20260604T120000Z", venues=[{"venue_id": "cash_usdc", "weight": 1.0}]))
    )
    out = _load_recent_prior_decisions(d, n=1, cycle_log_path=tmp_path / "nope.jsonl")
    assert len(out) == 1
    assert "_cycle_outcome" not in out[0]


def test_load_recent_prior_decisions_cycle_log_without_match_no_join(tmp_path: Path) -> None:
    """cycle_log.jsonl present but no entry matches the decision filename
    (rare race: decision file written, cycle_log line not yet flushed) →
    decision is still returned without `_cycle_outcome`."""
    from agent.sandbox.decide import _load_recent_prior_decisions

    d = tmp_path / "decisions"
    d.mkdir()
    (d / "20260604T120000Z.json").write_text(
        json.dumps(_decision_blob(ts="20260604T120000Z", venues=[{"venue_id": "cash_usdc", "weight": 1.0}]))
    )
    cycle_log = tmp_path / "cycle_log.jsonl"
    cycle_log.write_text(
        json.dumps(_cycle_log_entry(decision_filename="20260603T120000Z.json", result="executed"))
        + "\n"
    )
    out = _load_recent_prior_decisions(d, n=1, cycle_log_path=cycle_log)
    assert len(out) == 1
    assert "_cycle_outcome" not in out[0]


def test_summarize_prior_decision_surfaces_cycle_outcome() -> None:
    """When `_cycle_outcome` is annotated, the digest surfaces the
    executor result so Claude reasons about what actually fired — not
    just what was planned. Critical for the case where the validator
    passed but the executor halted (drawdown) or partially filled."""
    from agent.sandbox.decide import _summarize_prior_decision

    blob = _decision_blob(
        ts="20260604T120000Z",
        venues=[
            {"venue_id": "cash_usdc", "weight": 0.5},
            {"venue_id": "bybit_flex", "weight": 0.5, "picks": [{"product_id": "1131", "weight": 1.0}]},
        ],
        thesis="rotate into Flex",
    )
    blob["_cycle_outcome"] = {
        "result": "executed_partial",
        "actions_planned": 5,
        "actions_executed": 3,
        "wake_reason": "heartbeat",
    }
    out = _summarize_prior_decision(blob)
    assert "cycle outcome" in out
    assert "result=executed_partial" in out
    assert "3/5 actions filled" in out


def test_summarize_prior_decision_no_outcome_line_when_unannotated() -> None:
    """Cold-start cycles or older decisions without cycle_log join must
    NOT render an empty outcome line — the digest stays clean."""
    from agent.sandbox.decide import _summarize_prior_decision

    blob = _decision_blob(
        ts="20260604T120000Z",
        venues=[{"venue_id": "cash_usdc", "weight": 1.0}],
    )
    out = _summarize_prior_decision(blob)
    assert "cycle outcome" not in out


def test_collect_recently_invalidated_keys_by_family():
    """ah.23: `auto_close:<family>:<pid>` notes key by (family, pid); a legacy
    bare `auto_close:<pid>` note reads under the wildcard family ""."""
    from datetime import UTC, datetime
    from agent.sandbox.decide import _collect_recently_invalidated
    now = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    priors = [{
        "_meta": {"written_at": "2026-06-09T11:30:00+00:00"},
        "notes": ["auto_close:earn:6", "auto_close:lm:7", "auto_close:9"],
    }]
    cd = _collect_recently_invalidated(priors, now=now)
    assert ("earn", "6") in cd
    assert ("lm", "7") in cd
    assert ("", "9") in cd  # legacy bare → wildcard family
