"""Tests for the on-chain DecisionLog writer — specifically that the
anchored `actionHash` reflects ACTUAL execution (not just intent).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent.sandbox.onchain_writer import (
    OnchainWriter,
    derive_execution_hash,
    derive_ids,
)


def _decision() -> dict:
    return {
        "_meta": {"written_at": "2026-06-07T00:00:00Z"},
        "venues": [
            {
                "venue_id": "bybit_flex",
                "weight": 0.5,
                "picks": [{"product_id": "1", "weight": 1.0}],
            }
        ],
        "hedges": [],
    }


def _action(status: str = "ok", **over) -> dict:
    base = {
        "kind": "SUBSCRIBE_EARN",
        "category": "FlexibleSaving",
        "product_id": "1",
        "coin": "USDC",
        "amount": "50",
        "status": status,
    }
    base.update(over)
    return base


def test_execution_hash_differs_from_intent_hash() -> None:
    """The whole point: a clean execution commits to a different hash
    than the intent hash so the two are distinguishable on-chain."""
    d = _decision()
    _, intent_hash = derive_ids(d, "snap.json")
    exec_hash = derive_execution_hash([_action("ok")])
    assert exec_hash != intent_hash


def test_execution_hash_changes_with_status() -> None:
    """A partial failure (status=error) must yield a different hash than
    a clean batch — otherwise on-chain can't tell them apart."""
    assert derive_execution_hash([_action("ok")]) != derive_execution_hash(
        [_action("error")]
    )


def test_execution_hash_ignores_error_text() -> None:
    """`error` free-text is excluded from the hash — only the `status`
    enum matters, so transient Bybit message wording is deterministic."""
    a1 = [_action("error", error="retCode=180016 insufficient balance")]
    a2 = [_action("error", error="connection timeout")]
    assert derive_execution_hash(a1) == derive_execution_hash(a2)


def _writer_with_mock_log() -> tuple[OnchainWriter, MagicMock]:
    writer = OnchainWriter.__new__(OnchainWriter)  # bypass from_env/__init__
    writer.agent_id = 99
    dlog = MagicMock()
    dlog.functions.exists.return_value.call.return_value = False
    writer.decision_log = dlog
    writer._send = lambda fn, **kw: "0xdeadbeef"  # type: ignore[method-assign]
    return writer, dlog


def test_record_decision_uses_execution_hash_when_actions_given() -> None:
    writer, dlog = _writer_with_mock_log()
    actions = [_action("ok")]
    writer.record_decision(
        _decision(), "snap.json", ipfs_cid="cid123", executed_actions=actions
    )
    _agent, _did, called_cid, called_hash = dlog.functions.recordDecision.call_args[0]
    assert called_hash == derive_execution_hash(actions)
    assert called_cid == "cid123"


def test_record_decision_falls_back_to_intent_hash_when_no_actions() -> None:
    writer, dlog = _writer_with_mock_log()
    d = _decision()
    _, intent_hash = derive_ids(d, "snap.json")
    writer.record_decision(d, "snap.json", ipfs_cid="", executed_actions=None)
    _agent, _did, _cid, called_hash = dlog.functions.recordDecision.call_args[0]
    assert called_hash == intent_hash
