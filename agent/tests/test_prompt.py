"""System prompt coverage tests.

Light tests that guard against accidental section removal — they don't
assert specific wording (the prompt evolves), only that the structural
sections + event-reaction coverage stay intact.
"""

from __future__ import annotations

from agent.reason.prompt import build_system_prompt


def test_event_reactions_section_present() -> None:
    """`event-driven-rebalance.5` added `# Event reactions` so the LLM
    has guidance on how to respond when the user message includes a
    `## Wake reason` block. Removing it without replacement would
    silently regress wake-event behavior."""
    prompt = build_system_prompt()
    assert "# Event reactions" in prompt
    # The section explicitly references the user-message wake header,
    # so the two parts stay synchronized.
    assert "## Wake reason" in prompt


def test_event_reactions_covers_all_seven_event_kinds() -> None:
    """Each event class from `notes/event-taxonomy.md` MUST have a
    reaction line in the prompt — otherwise a fired event surfaces in
    the user message without any guidance on how to act."""
    prompt = build_system_prompt()
    expected_kinds = [
        "price_drift",
        "funding_flip",
        "peg_drift",
        "da_settlement_window",
        "new_hold_to_earn",
        "measured_yield_jump",
        "lm_liquidation_distance",
    ]
    for kind in expected_kinds:
        assert kind in prompt, f"Event kind {kind!r} missing from prompt"


def test_event_reactions_heartbeat_fallthrough_noted() -> None:
    """The section explicitly tells the model that a heartbeat-only
    cycle (no `## Wake reason`) means thresholds didn't fire and to
    proceed with normal allocation — this prevents the model from
    inventing a wake reason when none was provided."""
    prompt = build_system_prompt()
    assert "heartbeat" in prompt.lower()
    assert "no `## Wake reason`" in prompt or "no ## Wake reason" in prompt


def test_objective_mandates_concentration_on_best_net_yield() -> None:
    """(.66) The objective must tell the model to concentrate into the best
    risk-adjusted NET yield, not passively spread."""
    prompt = build_system_prompt().lower()
    assert "concentrat" in prompt
    assert "net yield" in prompt or "net-of-hedge" in prompt or "effective_apr_net_hedge" in prompt


def test_prompt_references_net_hedge_field() -> None:
    """The pre-computed net-of-hedge field must be surfaced so the model
    ranks on it instead of doing the funding math by hand."""
    prompt = build_system_prompt()
    assert "effective_apr_net_hedge" in prompt


def test_prompt_forbids_leveraged_lm() -> None:
    """(.66) LM leverage is forbidden — the prompt must say so, not the old
    'Leveraged LM is ALLOWED' framing."""
    prompt = build_system_prompt()
    assert "Leveraged LM is ALLOWED" not in prompt
    assert "unleveraged" in prompt.lower()


def test_prompt_source_quality_ladder_and_flex_default() -> None:
    """(.1/.6) The probe ladder must be graded by source quality (7% probe →
    30% on measured_yield → 60% on apr_history) and NEW stable yield must
    default to liquid Flex over slow-settle OnChain."""
    prompt = build_system_prompt()
    lower = prompt.lower()
    assert "source-quality" in lower or "source quality" in lower
    # measured_yield is the intermediate (~30%) tier, apr_history unlocks 60%.
    assert "30%" in prompt and "60%" in prompt
    assert "measured_yield" in prompt and "apr_history" in prompt
    # Flex is the default for NEW stable yield.
    assert "effective_apr_net_holding" in prompt
    assert "default" in lower and "flex" in lower
