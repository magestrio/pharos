"""Tests for the human-voice cycle reflection (agent.sandbox.reflect)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import pytest

from agent.sandbox.reflect import (
    _summarize_decision,
    _summarize_execution,
    reflect_on_cycle,
)

_DECISION = {
    "thesis": "Held — USD1 flex underperforming, TON funding marginal.",
    "confidence": 0.62,
    "expected_blended_apr_pct": 7.54,
    "venues": [
        {
            "venue_id": "bybit_flex",
            "weight": 0.49,
            "picks": [{"product_id": "1131", "weight": 0.816}],
        },
        {"venue_id": "cash_usdc", "weight": 0.30},
    ],
    "hedges": [{"coin": "TON", "notional_usd": -15.91}],
}


def test_summarize_decision_includes_venues_and_hedges():
    out = _summarize_decision(_DECISION)
    assert "confidence: 0.62" in out
    assert "bybit_flex 0.49" in out
    assert "1131 0.816" in out
    assert "cash_usdc 0.3" in out
    assert "TON" in out  # hedge surfaced


def test_summarize_execution_held_vs_executed():
    held = _summarize_execution({"result": "no_actions", "actions_executed": 0})
    assert "no_actions" in held

    executed = _summarize_execution(
        {
            "result": "executed",
            "actions_executed": 2,
            "actions_failed": 0,
            "actions": [
                {"kind": "SUBSCRIBE_EARN", "coin": "TON", "status": "ok"},
            ],
        }
    )
    assert "executed" in executed
    assert "SUBSCRIBE_EARN" in executed
    assert "TON" in executed


def _mock_client(text: str) -> AsyncMock:
    async def _create(**_kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)]
        )

    client = AsyncMock()
    client.messages.create = _create
    return client


@pytest.mark.asyncio
async def test_reflect_returns_text_on_success():
    client = _mock_client("I held this cycle; the marginal pickup wasn't worth it.")
    out = await reflect_on_cycle(
        _DECISION,
        {"result": "no_actions", "actions_executed": 0, "actions": []},
        client=client,
    )
    assert out == "I held this cycle; the marginal pickup wasn't worth it."


@pytest.mark.asyncio
async def test_reflect_passes_outcome_into_prompt():
    captured: dict = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])

    client = AsyncMock()
    client.messages.create = _create
    await reflect_on_cycle(
        _DECISION,
        {"result": "executed", "actions_executed": 1, "actions": []},
        client=client,
    )
    user_msg = captured["messages"][0]["content"]
    assert "executed" in user_msg  # the real outcome reached the model
    assert "USD1 flex underperforming" in user_msg  # thesis context included


@pytest.mark.asyncio
async def test_reflect_degrades_to_none_on_api_error():
    async def _boom(**_kwargs):
        raise anthropic.APIConnectionError(request=SimpleNamespace())

    client = AsyncMock()
    client.messages.create = _boom
    out = await reflect_on_cycle(
        _DECISION, {"result": "no_actions", "actions": []}, client=client
    )
    assert out is None


@pytest.mark.asyncio
async def test_reflect_degrades_to_none_on_empty_response():
    client = _mock_client("   ")  # whitespace-only → treated as empty
    out = await reflect_on_cycle(
        _DECISION, {"result": "no_actions", "actions": []}, client=client
    )
    assert out is None


@pytest.mark.asyncio
async def test_attach_reflection_writes_into_decision_file(tmp_path, monkeypatch):
    from agent.sandbox import loop

    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    monkeypatch.setattr(loop, "DECISION_DIR", decisions_dir)

    dec_path = decisions_dir / "20260608T000000Z.json"
    dec_path.write_text(json.dumps(dict(_DECISION)))

    client = _mock_client("Held the book; watching TON funding next cycle.")
    outcome = {
        "decision_filename": dec_path.name,
        "result": "no_actions",
        "actions_executed": 0,
        "actions": [],
        "stages": ["decide", "validate"],
    }
    await loop._attach_reflection(outcome, client)

    saved = json.loads(dec_path.read_text())
    assert saved["reflection"] == "Held the book; watching TON funding next cycle."
    assert outcome["reflection"] == saved["reflection"]
    assert "reflect" in outcome["stages"]


@pytest.mark.asyncio
async def test_attach_reflection_is_idempotent(tmp_path, monkeypatch):
    from agent.sandbox import loop

    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    monkeypatch.setattr(loop, "DECISION_DIR", decisions_dir)

    dec_path = decisions_dir / "20260608T000000Z.json"
    seeded = dict(_DECISION)
    seeded["reflection"] = "Existing note — must not be overwritten."
    dec_path.write_text(json.dumps(seeded))

    client = _mock_client("NEW note that should never be written.")
    outcome = {"decision_filename": dec_path.name, "result": "executed", "actions": []}
    await loop._attach_reflection(outcome, client)

    saved = json.loads(dec_path.read_text())
    assert saved["reflection"] == "Existing note — must not be overwritten."


@pytest.mark.asyncio
async def test_attach_reflection_no_double_call_on_write_failure(tmp_path, monkeypatch):
    """If the decision-file write fails, the note must still land on the
    outcome so the run_loop backstop does NOT re-run the paid Haiku call."""
    from agent.sandbox import loop

    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    monkeypatch.setattr(loop, "DECISION_DIR", decisions_dir)
    dec_path = decisions_dir / "20260608T000000Z.json"
    dec_path.write_text(json.dumps(dict(_DECISION)))

    calls = {"n": 0}

    async def _counting_create(**_kwargs):
        calls["n"] += 1
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="note")])

    client = AsyncMock()
    client.messages.create = _counting_create

    # Force the file write to fail (simulate a read-only volume / IO error).
    import pathlib

    real_write = pathlib.Path.write_text

    def _boom_write(self, *a, **k):
        if self == dec_path:
            raise OSError("disk full")
        return real_write(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "write_text", _boom_write)

    outcome = {"decision_filename": dec_path.name, "result": "executed", "actions": []}
    # First call (executed-path): generates, file write fails, but outcome carries it.
    await loop._attach_reflection(outcome, client)
    assert outcome["reflection"] == "note"
    assert calls["n"] == 1

    # Backstop call: must no-op via the outcome-level guard — no second Haiku call.
    await loop._attach_reflection(outcome, client)
    assert calls["n"] == 1


def test_ipfs_pin_preserves_reflection(monkeypatch):
    """The IPFS pin embeds the human note: `pin_decision_rationale` must
    carry a top-level `reflection` into the pinned payload (only the
    self-referential `_meta.ipfs_cid` is stripped). This is the guarantee
    that the on-chain-referenced rationale includes the diary note — the
    reason the loop attaches the reflection BEFORE `_anchor_onchain`."""
    from agent.sandbox import ipfs_pin

    captured: dict = {}

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": {"cid": "bafytestcid"}}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, _url, headers=None, files=None, data=None):
            captured["body"] = files["file"][1].read().decode("utf-8")
            return _FakeResp()

    monkeypatch.setenv("PINATA_JWT", "test-jwt")
    monkeypatch.setattr(ipfs_pin.httpx, "Client", _FakeClient)

    decision = dict(_DECISION)
    decision["reflection"] = "I held the book this cycle, watching funding."
    decision["_meta"] = {"ipfs_cid": "stale-self-reference", "written_at": "x"}

    cid = ipfs_pin.pin_decision_rationale(decision, "20260608T000000Z.json")
    assert cid == "bafytestcid"

    pinned = json.loads(captured["body"])
    assert pinned["reflection"] == "I held the book this cycle, watching funding."
    # self-referential cid stripped, rest of _meta survives
    assert "ipfs_cid" not in pinned["_meta"]
    assert pinned["_meta"]["written_at"] == "x"
