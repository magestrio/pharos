"""Persistent state for funding-carry positions (`.5`).

Carry positions look identical to Earn-derived hedges on the wire — a
spot long + perp short on a non-stable coin. To reconcile them in the
diff layer without double-counting (Earn-hedge code would otherwise try
to CLOSE a carry perp it didn't open), the executor tags each carry
open and persists the `(coin, order_link_ids, target_pick_usd)` tuple
here. The hedge reconciliation reads `active_coins()` and skips any
coin owned by carry.

State file lives at `sandbox/state/funding_carry.json` and is rewritten
atomically (tmp + rename) — same pattern as `watcher.py`. Empty file /
missing path treated as "no carry positions"; the loop will create the
parent dir on first write.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


DEFAULT_CARRY_STATE_PATH = Path(__file__).parent / "state" / "funding_carry.json"


class CarryPositionRecord(BaseModel):
    """One open carry position. Coin is the key (one position per coin
    by design — validator `check_no_double_carry_hedge` enforces no
    overlap with Earn hedges, and the diff layer no-ops same-coin
    targets that already exist)."""

    model_config = ConfigDict(extra="ignore")

    coin: str  # uppercase, e.g. "TON"
    opened_at: datetime
    target_pick_usd: Decimal  # at-open target sizing in USD
    spot_qty_base: Decimal  # spot base-coin holding at open (= pick_usd / mark)
    perp_qty_base: Decimal  # perp short notional in base coin (= spot_qty)
    mark_price_at_open: Decimal  # for audit / paired-notional verification
    spot_order_link_id: str
    perp_order_link_id: str
    # CLOSE retry counter (.5 fix 2026-06-04). Incremented every cycle
    # the CLOSE_FUNDING_CARRY dispatch returns `status="orphan"` (spot
    # leg unwound, perp leg failed e.g. on persistent margin shortfall).
    # When the counter hits `MAX_CARRY_CLOSE_ATTEMPTS` the diff layer
    # stops emitting CLOSE on this coin — the orphan needs operator
    # attention instead of unbounded retry that just spams Bybit. Reset
    # to 0 on every successful CLOSE (which removes the record entirely).
    close_attempts: int = Field(default=0, ge=0)


class CarryState(BaseModel):
    """Container — list rather than dict for stable JSON ordering on
    disk + cheap iteration in the diff layer. Lookups are O(N) but N is
    bounded by the venue `max_weight` × min_pick_size (typically ≤ 5)."""

    model_config = ConfigDict(extra="ignore")

    positions: list[CarryPositionRecord] = Field(default_factory=list)

    def active_coins(self) -> set[str]:
        return {p.coin.upper() for p in self.positions}

    def get(self, coin: str) -> CarryPositionRecord | None:
        target = coin.upper()
        for p in self.positions:
            if p.coin.upper() == target:
                return p
        return None

    def upsert(self, record: CarryPositionRecord) -> "CarryState":
        out = [p for p in self.positions if p.coin.upper() != record.coin.upper()]
        out.append(record)
        return CarryState(positions=out)

    def remove(self, coin: str) -> "CarryState":
        target = coin.upper()
        return CarryState(
            positions=[p for p in self.positions if p.coin.upper() != target]
        )


def read_carry_state(path: Path = DEFAULT_CARRY_STATE_PATH) -> CarryState:
    """Load state from disk. Missing file / parse error → empty state
    (loop will recreate on next write). Truncate-on-corruption is
    intentional: this is derived state — losing it just means the next
    cycle treats any in-flight carry perp as orphaned by the hedge
    layer, which is loud and recoverable."""
    if not path.exists():
        return CarryState()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return CarryState()
    try:
        return CarryState.model_validate(raw)
    except Exception:  # noqa: BLE001
        return CarryState()


def write_carry_state(
    state: CarryState, path: Path = DEFAULT_CARRY_STATE_PATH
) -> None:
    """Atomic write — same tmp+rename pattern as `watcher.write_baseline`
    so a partial write never leaves a half-parsed JSON on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2))
    os.replace(tmp, path)
