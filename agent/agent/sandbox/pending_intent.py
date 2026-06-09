"""Durable pending-intent marker for crash recovery (ah.7 / state-2).

`execute_actions` writes each per-action execution row only AFTER the legs land
on Bybit (`execute.py`: `res = await _execute_one(...)` THEN `log_file.write`).
So a hard crash (SIGKILL / OOM) BETWEEN placing an order and writing its row
leaves a real position with no log row AND no `carry_state` record — the next
cycle's diff would re-open it (double position). `detect_unfinished_cycles`
only catches crashes AFTER the row is flushed; it can't see the in-flight one.

This marker closes that window: the loop persists the confirmable
`order_link_id`s of a cycle BEFORE dispatch, then clears them once execute +
the `carry_state` write complete. Startup verifies any SURVIVING marker against
Bybit order-history (`verify_order_links`) and HALTs on a landed-but-
unreconciled order, so the operator reconciles before new positions open.

One JSON file at `sandbox/state/pending_intent.json`, atomic tmp+rename — same
pattern as `carry_state` / `watcher`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


DEFAULT_PENDING_INTENT_PATH = (
    Path(__file__).parent / "state" / "pending_intent.json"
)


class PendingOrderLink(BaseModel):
    """One confirmable order the cycle is about to place. `category` is the
    Bybit order-history category (`"spot"` / `"linear"`) so startup can query
    `/v5/order/history` by `order_link_id`."""

    model_config = ConfigDict(extra="ignore")

    order_link_id: str
    category: str  # "spot" | "linear"
    symbol: str | None = None
    kind: str  # originating ActionKind value, for the operator log
    coin: str | None = None


class PendingIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    snapshot_ts: str
    links: list[PendingOrderLink] = Field(default_factory=list)


def read_pending_intent(
    path: Path = DEFAULT_PENDING_INTENT_PATH,
) -> PendingIntent | None:
    """Load the marker. Missing / corrupt → None (treated as "no in-flight
    cycle"). Corruption is non-fatal: the executions-log scan is the
    second line of defence."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return PendingIntent.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None


def write_pending_intent(
    intent: PendingIntent, path: Path = DEFAULT_PENDING_INTENT_PATH
) -> None:
    """Atomic write — tmp+rename so a crash mid-write never leaves a
    half-parsed marker that would itself read as corrupt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(intent.model_dump_json(indent=2))
    os.replace(tmp, path)


def clear_pending_intent(path: Path = DEFAULT_PENDING_INTENT_PATH) -> None:
    """Remove the marker once the cycle's execute + state write are durable.
    Idempotent — a missing file is the normal steady state."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
