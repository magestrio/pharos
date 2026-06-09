"""Durable cooldown for LM residual-redeem actions (wt-3).

`_lm_residual_redeem_actions` force-redeems a held LM whose un-hedgeable naked
base residual exceeds the floor. The LP redeem settles async, so the same
`removeRate=100` would re-fire every non-executing cycle until settlement and
just `180020`-spam Bybit. The raw LM payload doesn't reliably echo a `status`
field to gate on, so we gate on positionId + timestamp here instead: a
position redeemed within `LM_REDEEM_COOLDOWN` is skipped; after the window a
position still showing a naked residual retries (covers a redeem that silently
failed — the de-risk must not strand a naked long forever).

State file lives at `sandbox/state/lm_redeem_cooldown.json`, rewritten
atomically (tmp+rename) — same pattern as `carry_state` / `watcher`. Missing
or corrupt file → empty cooldown; losing it costs at most one extra redeem
attempt, which `180020`s harmlessly.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_LM_REDEEM_COOLDOWN_PATH = (
    Path(__file__).parent / "state" / "lm_redeem_cooldown.json"
)

# An LP `removeRate=100` settles well inside this window; long enough to skip
# the ~4h heartbeat + event-driven (~120s) cycles in between, short enough that
# a genuinely-failed redeem retries the same day.
LM_REDEEM_COOLDOWN = timedelta(hours=6)


class LMRedeemCooldown(BaseModel):
    """positionId → timestamp of the last residual-redeem we emitted."""

    model_config = ConfigDict(extra="ignore")

    entries: dict[str, datetime] = Field(default_factory=dict)

    def blocked_position_ids(self, now: datetime) -> set[str]:
        """positionIds redeemed within the cooldown window — skip these."""
        return {
            pid
            for pid, ts in self.entries.items()
            if now - ts < LM_REDEEM_COOLDOWN
        }

    def record(
        self, position_ids: set[str], now: datetime
    ) -> "LMRedeemCooldown":
        """Stamp `now` on every just-emitted positionId."""
        merged = dict(self.entries)
        for pid in position_ids:
            if pid:
                merged[pid] = now
        return LMRedeemCooldown(entries=merged)

    def prune(
        self, live_position_ids: set[str], now: datetime
    ) -> "LMRedeemCooldown":
        """Drop entries whose position is gone (settled) OR past the window —
        keeps the file bounded. A still-present, past-window position simply
        won't be in `blocked_position_ids`, so it retries naturally."""
        kept = {
            pid: ts
            for pid, ts in self.entries.items()
            if pid in live_position_ids and now - ts < LM_REDEEM_COOLDOWN
        }
        return LMRedeemCooldown(entries=kept)


def read_lm_redeem_cooldown(
    path: Path = DEFAULT_LM_REDEEM_COOLDOWN_PATH,
) -> LMRedeemCooldown:
    """Load cooldown from disk. Missing file / parse error → empty (same
    truncate-on-corruption stance as `read_carry_state`: derived state whose
    worst-case loss is one extra, harmless redeem attempt)."""
    if not path.exists():
        return LMRedeemCooldown()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return LMRedeemCooldown()
    try:
        return LMRedeemCooldown.model_validate(raw)
    except Exception:  # noqa: BLE001
        return LMRedeemCooldown()


def write_lm_redeem_cooldown(
    state: LMRedeemCooldown, path: Path = DEFAULT_LM_REDEEM_COOLDOWN_PATH
) -> None:
    """Atomic write — tmp+rename so a partial write never leaves half-parsed
    JSON on disk (same pattern as `write_carry_state`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2))
    os.replace(tmp, path)
