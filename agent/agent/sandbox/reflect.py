"""Generate a human-voice 'diary' note for one finished decision cycle.

The structured `thesis` (produced by the main `submit_decision` tool call)
stays the canonical, quant-style audit rationale. This module adds a
separate first-person `reflection` — a calm 2-4 sentence note in the voice
of a portfolio manager, written AFTER the cycle's real outcome is known
(executed / held / skipped + which orders landed). It is purely for human
reading (web UI, IPFS), never validated and never fed back to the decision
prompt.

Best-effort, mirroring `ipfs_pin.pin_decision_rationale`: missing
`ANTHROPIC_API_KEY`, a network blip, or a malformed response → `None`, and
the agent loop continues with no reflection attached.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic

log = logging.getLogger(__name__)

# Cheap model — the reflection is a short prose note, not a reasoning task.
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 400
_TIMEOUT_SECONDS = 30.0

_SYSTEM_PROMPT = """\
You are the portfolio manager of vUSDC, an AI-managed USDC yield vault. \
You have just finished one allocation cycle. Write a SHORT first-person \
note — 2 to 4 sentences — in the calm, plain voice of a professional fund \
manager jotting down what just happened: what you saw in the market, what \
you decided and why, and what you're wary of or watching next.

Rules:
- First person ("I"), present/past tense, like a private trading journal.
- 2-4 sentences. No bullet lists, no headers, no markdown.
- Do NOT restate every number or repeat the structured thesis verbatim. \
Pull out only what mattered to the call.
- Reflect the REAL outcome you are given: if nothing was executed (a hold \
or a skip), say so honestly and humanly — a deliberate hold is a valid, \
confident outcome, not a failure.
- No emoji, no hype words ("blazing", "robust", "production-ready"). \
Sound like a person, not a report."""


def _summarize_decision(decision: dict[str, Any]) -> str:
    """Compact, human-skimmable digest of the structured decision —
    venue weights, picks, hedges, confidence — for the reflection prompt."""
    lines: list[str] = []
    conf = decision.get("confidence")
    apr = decision.get("expected_blended_apr_pct")
    if conf is not None:
        lines.append(f"confidence: {conf}")
    if apr is not None:
        lines.append(f"expected blended APR: {apr}%")

    for v in decision.get("venues") or []:
        vid = v.get("venue_id", "?")
        w = v.get("weight", 0)
        picks = v.get("picks") or []
        if picks:
            pick_str = ", ".join(
                f"{p.get('product_id', '?')} {p.get('weight', 0)}"
                for p in picks
            )
            lines.append(f"{vid} {w} [{pick_str}]")
        else:
            lines.append(f"{vid} {w}")

    hedges = decision.get("hedges") or []
    if hedges:
        lines.append(
            "hedges: "
            + ", ".join(
                f"{h.get('coin', '?')} {h.get('notional_usd', 0)}"
                for h in hedges
            )
        )
    return "\n".join(lines)


def _summarize_execution(execution: dict[str, Any]) -> str:
    """One-block summary of what actually happened this cycle."""
    result = execution.get("result")
    executed = execution.get("actions_executed")
    failed = execution.get("actions_failed")
    parts = [f"result: {result}"]
    if executed is not None:
        parts.append(f"actions executed: {executed}")
    if failed:
        parts.append(f"actions failed: {failed}")

    action_lines: list[str] = []
    for a in execution.get("actions") or []:
        kind = a.get("kind", "?")
        coin = a.get("coin") or a.get("product_id") or ""
        status = a.get("status", "?")
        action_lines.append(f"  {kind} {coin} → {status}")
    summary = "; ".join(parts)
    if action_lines:
        summary += "\nactions:\n" + "\n".join(action_lines)
    return summary


def _key_signals(snapshot: dict[str, Any] | None) -> str:
    """A couple of headline market signals (USDC peg) for color. Kept tiny
    — the decision digest already carries the substance."""
    if not snapshot:
        return ""
    peg = snapshot.get("usdc_peg") or {}
    price = peg.get("price") or peg.get("usd")
    if price is not None:
        return f"USDC peg: {price}"
    return ""


async def reflect_on_cycle(
    decision: dict[str, Any],
    execution: dict[str, Any],
    snapshot: dict[str, Any] | None = None,
    prior_reflection: str | None = None,
    client: anthropic.AsyncAnthropic | None = None,
) -> str | None:
    """Return a 2-4 sentence first-person reflection on the finished cycle,
    or `None` on any failure (missing API key, network error, empty output).

    `execution` is the same block the loop builds for the IPFS pin
    (`result`, `actions_executed`, `actions_failed`, `actions`)."""
    user_parts = [
        "DECISION (structured):",
        _summarize_decision(decision),
        "",
        "THESIS (my quant rationale this cycle):",
        str(decision.get("thesis", "")).strip(),
        "",
        "WHAT ACTUALLY HAPPENED:",
        _summarize_execution(execution),
    ]
    signals = _key_signals(snapshot)
    if signals:
        user_parts += ["", "MARKET:", signals]
    if prior_reflection:
        user_parts += ["", "MY NOTE LAST CYCLE:", prior_reflection.strip()]
    user_parts += [
        "",
        "Write my journal note for this cycle now.",
    ]
    user_message = "\n".join(user_parts)

    client = client or anthropic.AsyncAnthropic()
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            timeout=_TIMEOUT_SECONDS,
        )
    except anthropic.AnthropicError as e:
        log.warning("reflect: Anthropic call failed: %s", e)
        return None

    text = "".join(
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        log.warning("reflect: empty response for decision")
        return None
    return text
