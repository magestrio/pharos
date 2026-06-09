"""Durable retry queue for on-chain decision anchoring (state-6).

When `recordDecision` fails to land (RPC down, EOA out of gas, tx dropped),
the decision is still saved to file + DB but its on-chain audit anchor is
missing — and nothing retried it, leaving a permanent gap in the audit
trail. This queue persists the minimal anchor inputs (`decisionId`, cid,
`actionHash`, hex-encoded) so a later cycle re-submits them without needing
the original `executed_actions`. Entries drop on success (or if the tx
landed late) and after `MAX_ANCHOR_ATTEMPTS` — a persistently-failing
decision needs operator attention, not unbounded retry spam.

State file `sandbox/state/onchain_anchor_queue.json`, atomic read/write +
graceful degrade — same shape as `carry_state`. Losing it just means a
handful of un-anchored decisions stay un-anchored (file + DB still hold the
full record), so truncate-on-corruption is acceptable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_ANCHOR_QUEUE_PATH = (
    Path(__file__).parent / "state" / "onchain_anchor_queue.json"
)

# Drop a pending anchor after this many failed re-submits — past this it's a
# standing problem (bad CID, contract state, chronic gas) the operator must
# resolve, and retrying every cycle just wastes gas-estimation RPC calls.
MAX_ANCHOR_ATTEMPTS = 10


class PendingAnchor(BaseModel):
    """One decision whose on-chain anchor hasn't landed yet."""

    model_config = ConfigDict(extra="ignore")

    decision_id: str  # hex, no 0x
    snapshot_filename: str
    ipfs_cid: str
    action_hash: str  # hex, no 0x
    enqueued_at: datetime
    attempts: int = Field(default=0, ge=0)


class AnchorQueue(BaseModel):
    """Container — list for stable JSON ordering; N is bounded by how many
    cycles can fail back-to-back, typically 0."""

    model_config = ConfigDict(extra="ignore")

    entries: list[PendingAnchor] = Field(default_factory=list)

    def upsert(self, pending: PendingAnchor) -> "AnchorQueue":
        """Add or replace by `decision_id` (a re-failed re-anchor refreshes
        the entry rather than duplicating it)."""
        kept = [
            e for e in self.entries if e.decision_id != pending.decision_id
        ]
        kept.append(pending)
        return AnchorQueue(entries=kept)

    def remove(self, decision_id: str) -> "AnchorQueue":
        return AnchorQueue(
            entries=[e for e in self.entries if e.decision_id != decision_id]
        )


def read_anchor_queue(
    path: Path = DEFAULT_ANCHOR_QUEUE_PATH,
) -> AnchorQueue:
    """Load the queue. Missing file / parse error → empty (truncate-on-
    corruption: derived recovery state, not the audit trail itself)."""
    if not path.exists():
        return AnchorQueue()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return AnchorQueue()
    try:
        return AnchorQueue.model_validate(raw)
    except Exception:  # noqa: BLE001
        return AnchorQueue()


def write_anchor_queue(
    queue: AnchorQueue, path: Path = DEFAULT_ANCHOR_QUEUE_PATH
) -> None:
    """Atomic write — tmp+rename (same pattern as `write_carry_state`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(queue.model_dump_json(indent=2))
    os.replace(tmp, path)
