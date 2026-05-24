import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.reason.client import _build_user_message, _extract_tool_input, reason
from agent.reason.prompt import SYSTEM_PROMPT, USER_PROMPT_HEADER
from agent.reason.schema import BybitSubAllocation, Decision, TargetAllocation


# --- helpers ---

def _tool_use_block(name: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=payload)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _message(content: list, stop_reason: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason)


def _mock_client(content: list, stop_reason: str = "tool_use"):
    client = SimpleNamespace()
    client.messages = SimpleNamespace()
    client.messages.create = AsyncMock(return_value=_message(content, stop_reason))
    return client


def _full_payload(**overrides) -> dict:
    base = {
        "thesis": "Aave V3 USDC supply APY is the highest risk-adjusted yield this cycle; modest Bybit basis sleeve for diversification.",
        "target_allocation": {
            "cash_usdc": 0.05,
            "aave_v3_usdc": 0.55,
            "aave_v3_weth": 0.10,
            "bybit_attestor": 0.30,
        },
        "bybit_sub_allocation": {
            "flexible_usdc": 0.40,
            "sol_basis_trade": 0.30,
            "eth_basis_trade": 0.20,
            "buffer_cash": 0.10,
        },
        "confidence": 0.7,
        "risk_flags": [],
        "expected_blended_apr_pct": 7.2,
    }
    base.update(overrides)
    return base


def _state_fixture() -> dict:
    return {
        "vault": {"total_assets_usd": 100_000.0, "cash_pct": 0.05},
        "market": {"aave_usdc_apy": 0.062, "funding_rate_8h": 0.0001},
        "allora": {"eth_24h": 3500.0},
        "risk": {"red_flags": []},
        "past_theses": ["prior thesis line one", "prior thesis line two"],
    }


# --- _build_user_message ---

def test_build_user_message_contains_header_and_state_keys():
    state = _state_fixture()
    msg = _build_user_message(state)
    assert USER_PROMPT_HEADER in msg
    for key in ("vault", "market", "allora", "risk", "past_theses"):
        assert key in msg
    assert "prior thesis line one" in msg


def test_build_user_message_serializes_non_json_safely():
    """default=str must avoid TypeError on datetime or other objects."""
    from datetime import datetime

    state = {"timestamp": datetime(2026, 5, 24, 12, 0, 0)}
    msg = _build_user_message(state)
    assert "2026-05-24" in msg


# --- _extract_tool_input ---

def test_extract_tool_input_picks_correct_tool():
    payload = _full_payload()
    response = _message([_tool_use_block("submit_decision", payload)])
    assert _extract_tool_input(response) == payload


def test_extract_tool_input_skips_text_block_then_finds_tool():
    payload = _full_payload()
    response = _message([_text_block("thinking..."), _tool_use_block("submit_decision", payload)])
    assert _extract_tool_input(response) == payload


def test_extract_tool_input_raises_when_no_tool_use():
    response = _message([_text_block("I refuse")], stop_reason="end_turn")
    with pytest.raises(RuntimeError, match="did not return"):
        _extract_tool_input(response)


def test_extract_tool_input_raises_when_wrong_tool_name():
    response = _message([_tool_use_block("other_tool", {})])
    with pytest.raises(RuntimeError, match="did not return"):
        _extract_tool_input(response)


# --- reason() integration via mock ---

async def test_reason_happy_path():
    payload = _full_payload()
    client = _mock_client([_tool_use_block("submit_decision", payload)])
    decision = await reason(_state_fixture(), client=client)

    assert isinstance(decision, Decision)
    assert decision.thesis.startswith("Aave V3 USDC")
    assert decision.target_allocation.aave_v3_usdc == 0.55
    assert decision.bybit_sub_allocation.sol_basis_trade == 0.30
    assert decision.expected_blended_apr_pct == 7.2


async def test_reason_invokes_api_with_caching_and_forced_tool():
    payload = _full_payload()
    client = _mock_client([_tool_use_block("submit_decision", payload)])
    await reason(_state_fixture(), client=client)

    kwargs = client.messages.create.await_args.kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_decision"}
    system = kwargs["system"]
    assert isinstance(system, list) and len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == SYSTEM_PROMPT
    assert kwargs["tools"][0]["name"] == "submit_decision"


async def test_reason_no_bybit_allows_missing_sub_allocation():
    payload = _full_payload(
        target_allocation={
            "cash_usdc": 0.05,
            "aave_v3_usdc": 0.60,
            "aave_v3_weth": 0.35,
            "bybit_attestor": 0.00,
        },
    )
    payload.pop("bybit_sub_allocation")
    client = _mock_client([_tool_use_block("submit_decision", payload)])
    decision = await reason(_state_fixture(), client=client)
    assert decision.bybit_sub_allocation is None


async def test_reason_bybit_active_without_sub_allocation_rejected():
    payload = _full_payload()
    payload.pop("bybit_sub_allocation")
    client = _mock_client([_tool_use_block("submit_decision", payload)])
    with pytest.raises(Exception):
        await reason(_state_fixture(), client=client)


async def test_reason_bad_sub_allocation_sum_rejected():
    payload = _full_payload(
        bybit_sub_allocation={
            "flexible_usdc": 0.40,
            "sol_basis_trade": 0.30,
            "eth_basis_trade": 0.20,
            "buffer_cash": 0.20,  # sums to 1.10
        },
    )
    client = _mock_client([_tool_use_block("submit_decision", payload)])
    with pytest.raises(Exception, match="bybit_sub_allocation"):
        await reason(_state_fixture(), client=client)


async def test_reason_invalid_top_level_sum_rejected_by_strict_field_bounds():
    """The validator catches sum drift; pydantic catches per-field bounds.
    A payload with negative cash_usdc fails at the model level."""
    payload = _full_payload(
        target_allocation={
            "cash_usdc": 0.02,  # below 0.03 floor
            "aave_v3_usdc": 0.58,
            "aave_v3_weth": 0.10,
            "bybit_attestor": 0.30,
        },
    )
    client = _mock_client([_tool_use_block("submit_decision", payload)])
    with pytest.raises(Exception):
        await reason(_state_fixture(), client=client)


# --- standalone schema sanity (pivot-era invariants) ---

def test_bybit_sub_allocation_valid():
    sub = BybitSubAllocation(flexible_usdc=0.40, sol_basis_trade=0.30, eth_basis_trade=0.20, buffer_cash=0.10)
    assert sub.flexible_usdc == 0.40


def test_bybit_sub_allocation_bad_sum_rejected():
    with pytest.raises(Exception, match="1.0"):
        BybitSubAllocation(flexible_usdc=0.40, sol_basis_trade=0.30, eth_basis_trade=0.20, buffer_cash=0.20)


def test_decision_requires_sub_alloc_when_bybit_active():
    with pytest.raises(Exception, match="bybit_sub_allocation"):
        Decision(
            thesis="A reasonably long thesis explaining the position rationale here.",
            target_allocation=TargetAllocation(
                cash_usdc=0.05, aave_v3_usdc=0.45, aave_v3_weth=0.20, bybit_attestor=0.30
            ),
            confidence=0.7,
            expected_blended_apr_pct=6.5,
        )


def test_decision_allows_missing_sub_alloc_when_bybit_zero():
    d = Decision(
        thesis="A reasonably long thesis explaining the position rationale here.",
        target_allocation=TargetAllocation(
            cash_usdc=0.05, aave_v3_usdc=0.60, aave_v3_weth=0.35, bybit_attestor=0.00
        ),
        confidence=0.7,
        expected_blended_apr_pct=5.8,
    )
    assert d.bybit_sub_allocation is None
