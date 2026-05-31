"""Decision runner for the Vault8004 sandbox loop.

CLI:
    python -m agent.sandbox.decide --snapshot agent/sandbox/snapshots/<ts>.json

Reads one snapshot JSON, passes it to Claude Opus 4.7 with the
production system prompt (`agent.reason.prompt.build_system_prompt`),
extracts the `submit_decision` tool call, validates the output shape,
runs the deterministic validator (`agent.validate.rules.validate`),
and writes the resulting decision to
`agent/sandbox/decisions/<ts>.json` alongside the validator outcome.

The runner does NOT execute anything on-chain or against Bybit; it is
the iteration loop for prompt + validator tuning.

Structured output is enforced via tool use
(`tool_choice={"type": "tool", "name": "submit_decision"}`).
Prompt-cache is set on the system prompt so 4h cycles keep the prompt
warm in the Anthropic cache (5min default TTL).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

from agent.reason.prompt import build_system_prompt
from agent.reason.schema import Decision
from agent.reason.venues import VENUE_REGISTRY
from agent.sandbox.snapshot import Snapshot
from agent.validate.rules import validate

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096
TOOL_NAME = "submit_decision"
DECISION_DIR = Path(__file__).parent / "decisions"

_PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "product_id": {"type": "string"},
        "weight": {"type": "number", "minimum": 0, "maximum": 1},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["product_id", "weight"],
    "additionalProperties": False,
}

_VENUE_SCHEMA = {
    "type": "object",
    "properties": {
        "venue_id": {"type": "string", "enum": sorted(VENUE_REGISTRY.keys())},
        "weight": {"type": "number", "minimum": 0, "maximum": 1},
        "picks": {"type": "array", "items": _PICK_SCHEMA},
    },
    "required": ["venue_id", "weight"],
    "additionalProperties": False,
}

_HEDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "coin": {"type": "string"},
        "notional_usd": {"type": "number"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["coin", "notional_usd"],
    "additionalProperties": False,
}

_DECISION_TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Submit your allocation decision for this cycle. The downstream "
        "validator rejects anything that violates the hard caps in the "
        "system prompt — but you should still submit a decision so the "
        "operator can review your reasoning."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "thesis": {"type": "string", "minLength": 1},
            "venues": {"type": "array", "items": _VENUE_SCHEMA},
            "hedges": {"type": "array", "items": _HEDGE_SCHEMA},
            "expected_blended_apr_pct": {"type": "number", "minimum": 0},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "thesis",
            "venues",
            "expected_blended_apr_pct",
            "confidence",
        ],
        "additionalProperties": False,
    },
}


def _trim_snapshot_for_llm(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Strip fields the LLM has no use for to shrink the input token bill.

    Snapshot on disk stays full-shape (for replay / debugging) — trimming
    happens only at the LLM serialization boundary. Current trims:
    - `usdc_peg.source` (always "coingecko" — static)
    - `usdc_peg.fetched_at` (redundant — top-level `captured_at` exists)
    Both are operational metadata; peg-stress logic in the prompt only
    reads `deviation_bps`. Token saving: ~15 per cycle.

    Future trim targets — products top-K downsize, perp_market subset for
    disabled venues, wallet account raw-array compaction — plug in here.
    """
    trimmed = {**snapshot}
    peg = trimmed.get("usdc_peg")
    if isinstance(peg, dict):
        trimmed["usdc_peg"] = {
            k: v for k, v in peg.items() if k not in ("source", "fetched_at")
        }
    return trimmed


def _format_wake_events(events: list[dict[str, Any]]) -> str:
    """Render the watcher-fired events that triggered this cycle as the
    "## Wake reason" section of the user message. Caller passes the raw
    `EventRecord.model_dump()` dicts — we surface `severity`, `kind`,
    `message` for each (the per-event `message` field is built in
    `agent.sandbox.watcher` and already explains what crossed the
    threshold)."""
    lines = ["## Wake reason", ""]
    lines.append(
        "This cycle was triggered by the event watcher (not the heartbeat). "
        "The following thresholds crossed since the last decided cycle — "
        "re-evaluate the affected positions:"
    )
    for ev in events:
        severity = ev.get("severity", "?")
        kind = ev.get("kind", "?")
        message = ev.get("message", "")
        lines.append(f"- [{severity} {kind}] {message}")
    return "\n".join(lines)


def _build_user_message(
    snapshot: dict[str, Any],
    prior_decision: dict[str, Any] | None = None,
    wake_events: list[dict[str, Any]] | None = None,
) -> str:
    snapshot = _trim_snapshot_for_llm(snapshot)
    payload = json.dumps(snapshot, indent=2, sort_keys=True, default=str)
    parts: list[str] = []
    # Wake reason goes FIRST when present — we want Claude to see why
    # this cycle exists before reading the snapshot, so the re-decide
    # context frames the rest of the input.
    if wake_events:
        parts.append(_format_wake_events(wake_events))
    parts.append(
        "Allocate the vault for the next cycle. The current snapshot follows "
        f"as JSON. Submit your decision via the `{TOOL_NAME}` tool — do not "
        "output free text outside the tool call."
    )
    if prior_decision is not None:
        prior_summary = _summarize_prior_decision(prior_decision)
        if prior_summary:
            parts.append(
                "Last cycle's decision (for whipsaw discipline — large "
                "reshuffles need a clear signal change to justify):\n"
                + prior_summary
            )
    parts.append(f"```json\n{payload}\n```")
    return "\n\n".join(parts)


def _summarize_prior_decision(decision: dict[str, Any]) -> str:
    """One-paragraph human-readable digest of a prior decision: the
    allocation, the picks (id only), confidence, validator outcome (so
    Claude can correct rejected decisions instead of repeating them),
    and the thesis. Keeps the user message short while giving the model
    enough to detect whipsaw vs informed shifts."""
    venues = decision.get("venues", [])
    if not venues:
        return ""
    venue_lines = []
    for v in venues:
        vid = v.get("venue_id")
        w = v.get("weight", 0)
        picks = v.get("picks", []) or []
        pick_str = (
            "[" + ",".join(f"{p['product_id']}@{p['weight']:.2f}" for p in picks) + "]"
            if picks
            else ""
        )
        venue_lines.append(f"  - {vid}={w:.2%}{(' picks=' + pick_str) if pick_str else ''}")
    conf = decision.get("confidence")
    thesis = (decision.get("thesis") or "").strip()
    if len(thesis) > 400:
        thesis = thesis[:400] + "…"
    # Validator outcome — `_meta` sidecar is the source of truth. If the
    # prior was rejected, surface the errors prominently so Claude
    # corrects rather than repeats. (`.47` follow-up 2026-05-29: cycles
    # were repeating the same min_notional/funding violations because
    # this summary hid the failure.)
    meta = decision.get("_meta") or {}
    validator = meta.get("_validator") or decision.get("_validator") or {}
    validator_ok = validator.get("ok")
    validator_errors = validator.get("errors") or []
    validator_line = ""
    if validator_ok is False or validator_errors:
        validator_line = (
            "\n  ❌ VALIDATOR REJECTED prior decision — DO NOT repeat the "
            "same picks/sizing:\n    - "
            + "\n    - ".join(str(e) for e in validator_errors)
        )
    elif validator_ok is True:
        validator_line = "\n  ✓ validator passed"
    return (
        "\n".join(venue_lines)
        + (f"\n  confidence={conf}" if conf is not None else "")
        + validator_line
        + (f"\n  thesis: {thesis}" if thesis else "")
    )


def _load_latest_prior_decision(
    decisions_dir: Path = DECISION_DIR,
) -> dict[str, Any] | None:
    """Return the most recent decision file under `decisions_dir`, or
    None if the directory is empty. The decision files are named
    `<UTC-ts>.json` so a lexicographic sort matches chronological order."""
    if not decisions_dir.is_dir():
        return None
    files = sorted(p for p in decisions_dir.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _extract_tool_input(response: anthropic.types.Message) -> dict[str, Any]:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return block.input  # type: ignore[return-value]
    texts = [
        getattr(b, "text", "")
        for b in response.content
        if getattr(b, "type", None) == "text"
    ]
    detail = " | ".join(t for t in texts if t) or "no text content"
    raise RuntimeError(
        f"decide call did not return a {TOOL_NAME} tool call "
        f"(stop_reason={response.stop_reason}, content: {detail})"
    )


async def decide(
    snapshot: dict[str, Any],
    client: anthropic.AsyncAnthropic | None = None,
    system_prompt: str | None = None,
    prior_decision: dict[str, Any] | None = None,
    wake_events: list[dict[str, Any]] | None = None,
) -> Decision:
    """Run one decision cycle. Returns a validated Decision.

    `prior_decision`, when provided, is summarized and prepended to the
    user message so the model can detect whipsaw and respect the "move
    slowly" discipline in the prompt. Pass `None` to skip (first cycle
    or intentional cold start).

    `wake_events`, when present, signals that this cycle was triggered
    by the event watcher (`event-driven-rebalance.2`) rather than the
    heartbeat. Plumbed in `.3`; rendered in the user message in `.4`.
    """
    client = client or anthropic.AsyncAnthropic()
    system_prompt = system_prompt or build_system_prompt()

    response = await client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[_DECISION_TOOL],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[
            {
                "role": "user",
                "content": _build_user_message(
                    snapshot, prior_decision, wake_events=wake_events
                ),
            }
        ],
    )

    tool_input = _extract_tool_input(response)
    return Decision.model_validate(tool_input)


def write_decision(
    decision: Decision,
    snapshot_path: Path,
    *,
    validator_result: tuple[bool, list[str]] | None = None,
    captured_at: datetime | None = None,
    decisions_dir: Path = DECISION_DIR,
    prompt_version: str = "reason.prompt",
    wake_events: list[dict[str, Any]] | None = None,
) -> Path:
    """Persist the decision (and its validator outcome, if known) next to
    the snapshot. File naming pairs decision↔snapshot via shared UTC
    timestamp: `decisions/<snapshot-ts>.json`.

    `wake_events`, when provided, stamps the wake reason into `_meta` so
    a downstream operator (and `.8` cost tracking) can attribute the
    cycle to "event:<kind>" vs "heartbeat".
    """
    decisions_dir.mkdir(parents=True, exist_ok=True)
    ts = (captured_at or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    out = decisions_dir / f"{ts}.json"
    payload = decision.model_dump(mode="json")
    payload["_meta"] = {
        "snapshot_filename": snapshot_path.name,
        "written_at": datetime.now(UTC).isoformat(),
        "model": MODEL,
        "prompt_version": prompt_version,
    }
    if wake_events:
        payload["_meta"]["wake_events"] = wake_events
        payload["_meta"]["wake_reason"] = (
            "event:" + ",".join(sorted({e.get("kind", "?") for e in wake_events}))
        )
    else:
        payload["_meta"]["wake_reason"] = "heartbeat"
    if validator_result is not None:
        ok, errors = validator_result
        payload["_validator"] = {"ok": ok, "errors": errors}
    out.write_text(json.dumps(payload, indent=2))
    return out


def _main() -> None:
    parser = argparse.ArgumentParser(description="Run one Vault8004 decision cycle.")
    parser.add_argument(
        "--snapshot",
        type=Path,
        required=True,
        help="Path to a snapshot JSON produced by agent.sandbox.snapshot",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="dotenv to load (e.g. .env at repo root)",
    )
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(args.env_file, override=True)

    raw_snapshot = json.loads(args.snapshot.read_text())
    snap = Snapshot.model_validate(raw_snapshot)
    prior = _load_latest_prior_decision()

    async def run() -> None:
        async with anthropic.AsyncAnthropic() as client:
            decision = await decide(
                raw_snapshot, client=client, prior_decision=prior
            )
        result = validate(decision, snap)
        path = write_decision(decision, args.snapshot, validator_result=result)
        ok, errs = result
        print(f"decision → {path}")
        print(
            f"  confidence={decision.confidence}  "
            f"expected_apr={decision.expected_blended_apr_pct}%  "
            f"risk_flags={decision.risk_flags}"
        )
        for v in decision.venues:
            picks = [(p.product_id, round(p.weight, 4)) for p in v.picks]
            print(f"  {v.venue_id}={v.weight:.2%}  picks={picks}")
        if decision.hedges:
            for h in decision.hedges:
                print(f"  hedge {h.coin} notional_usd={h.notional_usd}")
        print(f"  validator: ok={ok}")
        for e in errs:
            print(f"    ERR: {e}")
        for line in decision.thesis.splitlines()[:3]:
            print(f"  thesis: {line}")

    asyncio.run(run())


if __name__ == "__main__":
    _main()
