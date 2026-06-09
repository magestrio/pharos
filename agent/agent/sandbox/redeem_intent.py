"""Durable exit-intent store for hedged non-stable Earn positions.

When the agent exits a hedged non-stable Earn pick it issues a `REDEEM_EARN`,
but the freed coin only lands on the balance AFTER settlement — fast for
`FlexibleSaving`, ~4 days (`Processing`) for `OnChain`. The paired perp short
must stay open the whole time (delta-neutral), and the moment the coin arrives
we must swap EXACTLY the freed amount to a stable AND close the short, together,
deterministically (no LLM).

This file records that intent durably so:
  - the watcher can detect arrival (`baseline_wallet_native` + `expected` give
    it the delta to watch, `product_id` the Earn row to watch disappear), and
  - the loop can size the exit from the RECORDED redeem amount instead of a
    re-derived delta-excess, and clear the intent only once both legs land.

The intent does NOT drive correctness on its own — stateless re-derivation
(`_orphan_perp_close_actions` / `_orphan_spot_sell_actions`) remains the
idempotent backstop, exactly as `carry_state` coexists with the diff layer. A
lost / corrupt intent degrades to "handled at heartbeat speed, delta-neutral
sizing", never a stranded position.

State file at `sandbox/state/redeem_intent.json`, atomic tmp+rename — same
pattern as `carry_state` / `pending_intent` / `watcher`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


DEFAULT_REDEEM_INTENT_PATH = Path(__file__).parent / "state" / "redeem_intent.json"


class RedeemExitIntent(BaseModel):
    """One in-flight hedged-Earn exit, keyed by `product_id` (one redeem per
    Earn product in flight at a time). `paired_perp_symbol` is None for an
    unhedged exit (stable pick that somehow lacked a hedge) — the handler then
    only swaps, no perp close."""

    model_config = ConfigDict(extra="ignore")

    coin: str  # uppercase, e.g. "TON"
    product_id: str  # Earn productId being redeemed
    category: str  # "FlexibleSaving" | "OnChain"
    opened_at: datetime
    expected_redeem_native: Decimal  # native coin amount we expect to free
    baseline_wallet_native: Decimal  # coin wallet balance (UNIFIED+FUND) at redeem time
    redeem_order_link_id: str
    paired_perp_symbol: str | None = None  # e.g. "TONUSDT"
    perp_qty_base: Decimal = Decimal(0)  # short size to close on exit (base coin)


class RedeemIntentState(BaseModel):
    """Container — list for stable on-disk ordering. N bounded by the number
    of concurrent non-stable Earn exits (typically ≤ a handful)."""

    model_config = ConfigDict(extra="ignore")

    intents: list[RedeemExitIntent] = Field(default_factory=list)

    def active_product_ids(self) -> set[str]:
        return {i.product_id for i in self.intents}

    def get(self, product_id: str) -> RedeemExitIntent | None:
        for i in self.intents:
            if i.product_id == product_id:
                return i
        return None

    def upsert(self, intent: RedeemExitIntent) -> "RedeemIntentState":
        out = [i for i in self.intents if i.product_id != intent.product_id]
        out.append(intent)
        return RedeemIntentState(intents=out)

    def remove(self, product_id: str) -> "RedeemIntentState":
        return RedeemIntentState(
            intents=[i for i in self.intents if i.product_id != product_id]
        )


def read_redeem_intents(
    path: Path = DEFAULT_REDEEM_INTENT_PATH,
) -> RedeemIntentState:
    """Load state. Missing / corrupt → empty (the orphan re-derivation path is
    the backstop, so losing this just reverts to heartbeat-speed cleanup).
    Two-stage degrade mirrors `carry_state.read_carry_state`."""
    if not path.exists():
        return RedeemIntentState()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return RedeemIntentState()
    try:
        return RedeemIntentState.model_validate(raw)
    except Exception:  # noqa: BLE001 — schema drift degrades, not crashes
        return RedeemIntentState()


def write_redeem_intents(
    state: RedeemIntentState, path: Path = DEFAULT_REDEEM_INTENT_PATH
) -> None:
    """Atomic write — tmp+rename so a crash mid-write never leaves a
    half-parsed file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2))
    os.replace(tmp, path)
