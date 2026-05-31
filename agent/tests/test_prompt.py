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
