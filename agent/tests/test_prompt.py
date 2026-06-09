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


# ─── ah.17: stables-set + cadence rendered from code constants ───────────────

def test_prompt_renders_full_stables_set_from_constant() -> None:
    """prompt-1 / promptcode-3: the hedging-discipline stables-set is rendered
    from `STABLES`, so every canonical stable appears (the old literal was
    truncated to 6 of 9 — missing USDTB/PYUSD/RLUSD)."""
    from agent.reason.venues import STABLES
    prompt = build_system_prompt()
    for coin in STABLES:
        assert coin in prompt, f"{coin} missing from rendered prompt"


def test_stables_constant_is_single_source() -> None:
    """`snapshot.STABLES` is the same object as `venues.STABLES` — re-export,
    not a second copy that could drift."""
    from agent.reason.venues import STABLES as venues_stables
    from agent.sandbox.snapshot import STABLES as snapshot_stables
    assert snapshot_stables is venues_stables


def test_prompt_cadence_is_consistent_no_stale_30min() -> None:
    """prompt-2: cadence is rendered from `DEFAULT_CYCLE_INTERVAL_SECONDS`, so
    the prior-decision paragraph no longer says "30 min default" while the
    Allora paragraph says "4h". The only "30 min" left is the unrelated
    da_settlement_window event threshold."""
    from agent.reason.venues import DEFAULT_CYCLE_INTERVAL_SECONDS
    hours = DEFAULT_CYCLE_INTERVAL_SECONDS // 3600
    prompt = build_system_prompt()
    assert f"heartbeat is {hours}h" in prompt
    assert f"{hours}h heartbeat" in prompt
    assert "30 min default" not in prompt


def test_loop_interval_matches_prompt_cadence() -> None:
    """The loop's default --interval is the same shared constant the prompt
    narrates (ah.17 centralization)."""
    from agent.sandbox.loop import DEFAULT_INTERVAL_SECONDS
    from agent.reason.venues import DEFAULT_CYCLE_INTERVAL_SECONDS
    assert DEFAULT_INTERVAL_SECONDS == DEFAULT_CYCLE_INTERVAL_SECONDS


# ─── ah.18: LM recipe gates, small-vault soft, invalidated fallback, hedges ──

def test_lm_recipe_names_all_three_gates() -> None:
    """promptcode-2: the LM sizing recipe no longer claims the 30% venue cap
    is the ONLY gate — it names the stable-preference (≥1.5%/yr) and naked-
    residual (≤3%) gates a NEW LM pick must also clear."""
    p = build_system_prompt()
    assert "the only cap is the 30%" not in p
    assert "1.5%/yr on the net-of-hedge" in p
    assert "hedge_residual_pct_of_book` must stay" in p


def test_small_vault_rules_marked_strategy_not_all_hard() -> None:
    """promptcode-4: concentration-mode rules are flagged as strategy
    guidance, distinguished from the validator-enforced caps."""
    p = build_system_prompt()
    assert "STRONG STRATEGY guidance" in p
    assert "won't reject on their own" in p


def test_pick_invalidated_marked_fallback() -> None:
    """promptcode-6: the pick_invalidated handling is framed as the fallback
    to the deterministic no-LLM auto-close, not the primary path."""
    p = build_system_prompt()
    assert "_build_auto_close_decision" in p
    assert "FALLBACK" in p


def test_hedges_example_notional_zeroed() -> None:
    """prompt-5: the hedges JSON example shows notional_usd=0 (it's ignored),
    not a meaningful -42 that contradicts the 'ignored' note above."""
    p = build_system_prompt()
    assert "-42" not in p
    assert '"notional_usd": 0,' in p
    assert "notional_usd is IGNORED" in p
