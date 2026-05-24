import json
from typing import Any

import anthropic

from agent.reason.prompt import SYSTEM_PROMPT, USER_PROMPT_HEADER
from agent.reason.schema import Decision

MODEL = "claude-opus-4-7"
MAX_TOKENS = 1500
TOOL_NAME = "submit_decision"

_BYBIT_SUB_SCHEMA = {
    "type": "object",
    "description": "Sub-allocation within bybit_attestor. Required when bybit_attestor > 0. Four fields must sum to 1.0.",
    "properties": {
        "flexible_usdc": {"type": "number", "minimum": 0, "maximum": 1},
        "sol_basis_trade": {"type": "number", "minimum": 0, "maximum": 1},
        "eth_basis_trade": {"type": "number", "minimum": 0, "maximum": 1},
        "buffer_cash": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["flexible_usdc", "sol_basis_trade", "eth_basis_trade", "buffer_cash"],
    "additionalProperties": False,
}

_DECISION_TOOL = {
    "name": TOOL_NAME,
    "description": "Submit the allocation decision for this cycle. The validator will reject any output that violates hard caps.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thesis": {
                "type": "string",
                "description": "Rationale for the allocation. Under 200 words. Cite which inputs drove the decision.",
                "minLength": 20,
            },
            "target_allocation": {
                "type": "object",
                "description": "Top-level venue allocation. Must sum to 1.0 ± 0.001.",
                "properties": {
                    "cash_usdc": {"type": "number", "minimum": 0.03, "maximum": 1},
                    "aave_v3_usdc": {"type": "number", "minimum": 0, "maximum": 1},
                    "aave_v3_weth": {"type": "number", "minimum": 0, "maximum": 1},
                    "bybit_attestor": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["cash_usdc", "aave_v3_usdc", "aave_v3_weth", "bybit_attestor"],
                "additionalProperties": False,
            },
            "bybit_sub_allocation": _BYBIT_SUB_SCHEMA,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "risk_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Show-stopping conditions you spotted. Any non-empty list skips the cycle.",
            },
            "expected_blended_apr_pct": {
                "type": "number",
                "minimum": 0,
                "description": "Honest weighted APR estimate at the proposed allocation.",
            },
        },
        "required": ["thesis", "target_allocation", "confidence", "risk_flags", "expected_blended_apr_pct"],
        "additionalProperties": False,
    },
}


def _build_user_message(state: dict[str, Any]) -> str:
    payload = json.dumps(state, default=str, indent=2, sort_keys=True)
    return f"{USER_PROMPT_HEADER}\n\n```json\n{payload}\n```"


def _extract_tool_input(response: anthropic.types.Message) -> dict[str, Any]:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return block.input
    text_blocks = [getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text"]
    detail = " | ".join(t for t in text_blocks if t) or "no text content"
    raise RuntimeError(
        f"Reason call did not return a {TOOL_NAME} tool call "
        f"(stop_reason={response.stop_reason}, content: {detail})"
    )


async def reason(state: dict[str, Any], client: anthropic.AsyncAnthropic | None = None) -> Decision:
    client = client or anthropic.AsyncAnthropic()

    response = await client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[_DECISION_TOOL],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": _build_user_message(state)}],
    )

    tool_input = _extract_tool_input(response)
    return Decision.model_validate(tool_input)
