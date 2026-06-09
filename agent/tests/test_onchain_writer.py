"""Tests for the on-chain DecisionLog writer — specifically that the
anchored `actionHash` reflects ACTUAL execution (not just intent).
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, call

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


def test_send_uses_pending_nonce() -> None:
    """state-7: nonce comes from the `pending` count, so a tx still in the
    mempool doesn't make the next one reuse a consumed nonce."""
    writer = OnchainWriter.__new__(OnchainWriter)
    writer.account = MagicMock(address="0xAGENT")
    signed = MagicMock(raw_transaction=b"raw")
    writer.account.sign_transaction.return_value = signed
    w3 = MagicMock()
    w3.eth.get_transaction_count.return_value = 7
    w3.eth.wait_for_transaction_receipt.return_value = MagicMock(status=1)
    writer.w3 = w3
    fn = MagicMock()

    writer._send(fn)

    assert w3.eth.get_transaction_count.call_args == call("0xAGENT", "pending")
    assert fn.build_transaction.call_args[0][0]["nonce"] == 7


def test_decision_exists_reflects_contract() -> None:
    writer, dlog = _writer_with_mock_log()
    dlog.functions.exists.return_value.call.return_value = True
    assert writer.decision_exists(b"\x01" * 32) is True
    dlog.functions.exists.return_value.call.return_value = False
    assert writer.decision_exists(b"\x01" * 32) is False


def test_decision_exists_false_on_rpc_error() -> None:
    writer, dlog = _writer_with_mock_log()
    dlog.functions.exists.return_value.call.side_effect = RuntimeError("rpc down")
    # Conservative: an unreadable contract → treat as not-anchored (stays queued).
    assert writer.decision_exists(b"\x01" * 32) is False


def test_anchor_prepared_skips_when_already_on_chain() -> None:
    writer, dlog = _writer_with_mock_log()
    dlog.functions.exists.return_value.call.return_value = True
    assert writer.anchor_prepared(b"\x02" * 32, "cid", b"\x03" * 32) is None
    dlog.functions.recordDecision.assert_not_called()


def test_gas_balance_mnt_converts_wei() -> None:
    writer = OnchainWriter.__new__(OnchainWriter)
    writer.account = MagicMock(address="0xAGENT")
    w3 = MagicMock()
    w3.eth.get_balance.return_value = 2 * 10**18
    writer.w3 = w3
    assert writer.gas_balance_mnt() == Decimal("2")
