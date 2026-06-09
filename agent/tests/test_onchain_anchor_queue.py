"""Tests for the on-chain anchor retry queue (state-6)."""

from __future__ import annotations

from datetime import UTC, datetime

from agent.sandbox.onchain_anchor_queue import (
    AnchorQueue,
    PendingAnchor,
    read_anchor_queue,
    write_anchor_queue,
)

_TS = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


def _pending(decision_id: str = "aa", **over) -> PendingAnchor:
    base = dict(
        decision_id=decision_id,
        snapshot_filename=f"snap-{decision_id}.json",
        ipfs_cid="cid",
        action_hash="bb",
        enqueued_at=_TS,
    )
    base.update(over)
    return PendingAnchor(**base)


def test_upsert_dedupes_by_decision_id() -> None:
    q = AnchorQueue().upsert(_pending("aa", attempts=1))
    q = q.upsert(_pending("aa", attempts=2))
    assert len(q.entries) == 1
    assert q.entries[0].attempts == 2


def test_remove_drops_entry() -> None:
    q = AnchorQueue(entries=[_pending("aa"), _pending("bb")])
    assert {e.decision_id for e in q.remove("aa").entries} == {"bb"}


def test_roundtrip_read_write(tmp_path) -> None:
    path = tmp_path / "onchain_anchor_queue.json"
    write_anchor_queue(AnchorQueue(entries=[_pending("aa")]), path)
    back = read_anchor_queue(path)
    assert [e.decision_id for e in back.entries] == ["aa"]


def test_read_missing_returns_empty(tmp_path) -> None:
    assert read_anchor_queue(tmp_path / "absent.json").entries == []


def test_read_corrupt_returns_empty(tmp_path) -> None:
    path = tmp_path / "onchain_anchor_queue.json"
    path.write_text("{ not json")
    assert read_anchor_queue(path).entries == []
