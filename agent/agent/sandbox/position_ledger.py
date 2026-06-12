"""Min-hold position ledger (anti-churn).

Tracks WHEN we first started holding each non-stable coin's exposure so the
deterministic validator can block a voluntary exit from a position too young
to have earned back its round-trip friction (spot swap + perp open/close).

Why a separate file from the watcher baseline: that baseline is rebuilt every
cycle from the live snapshot, so it can't carry an entry timestamp forward.
This ledger persists first-seen across cycles — preserve existing coins, stamp
new ones, drop coins no longer held — keyed by COIN. The coin is the unit that
actually pays the round-trip friction: rotating between two USDC products is
~free, so stables are never tracked.

The watcher's danger-exit path (peg / 30% drift / funding flip / liq distance,
via `loop._build_auto_close_decision`) intentionally bypasses the min-hold gate
(`validate(..., allow_exits=True)`): "don't lose principal" always overrides
"hold to recoup friction".
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.reason.venues import STABLES

log = logging.getLogger(__name__)

# Hours a non-stable exposure is protected from a VOLUNTARY exit. 24h = 6
# heartbeat cycles — long enough that a hedged non-stable's ~1.8% round-trip
# friction is amortized by funding + APR over a realistic hold, short enough
# that the book isn't trapped in a decaying position before the watcher's
# danger gates fire. Complements `decide.PICK_INVALIDATE_COOLDOWN_MIN` (2h):
# that is the narrow "don't re-pick what we force-closed" floor; this is the
# broader "hold what you chose" floor.
MIN_HOLD_HOURS: float = 24.0

DEFAULT_LEDGER_PATH = Path(__file__).parent / "state" / "position-ledger.json"


def _is_stable(coin: str) -> bool:
    return coin.strip().upper() in STABLES


def _nonzero(value: Any) -> bool:
    """True unless `value` cleanly parses to 0. Missing / unparseable amounts
    count as held — we'd rather over-track (a spurious min-hold) than miss a
    real position."""
    if value is None:
        return True
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return True


def held_nonstable_coins(snap: Any) -> set[str]:
    """Set of UPPERCASE non-stable coins with a live exposure in `snap`.

    Unions the principal-leg holdings (Earn / LM base / Alpha) with the perp
    short legs — every open short in this book is the hedge of an Earn pick or
    the short side of a carry, so a perp on COIN means COIN exposure is held
    (and would pay a round-trip to unwind)."""
    coins: set[str] = set()

    for p in getattr(snap, "earn_positions", None) or []:
        coin = str(p.get("coin") or "").upper()
        if coin and not _is_stable(coin) and _nonzero(p.get("amount")):
            coins.add(coin)

    for p in getattr(snap, "lm_positions", None) or []:
        raw = str(p.get("coin") or p.get("baseCoin") or "")
        base = raw.split("/", 1)[0].upper()
        if base and not _is_stable(base):
            coins.add(base)

    for p in getattr(snap, "alpha_positions", None) or []:
        coin = str(p.get("symbol") or p.get("tokenSymbol") or "").upper()
        if coin and not _is_stable(coin):
            coins.add(coin)

    for p in getattr(snap, "perp_positions", None) or []:
        symbol = str(getattr(p, "symbol", "") or "")
        side = str(getattr(p, "side", "") or "")
        if side not in ("Buy", "Sell") or not _nonzero(getattr(p, "size", None)):
            continue
        coin = symbol.removesuffix("USDT").upper()
        if coin and not _is_stable(coin):
            coins.add(coin)

    return coins


def read_ledger(path: Path = DEFAULT_LEDGER_PATH) -> dict[str, str]:
    """coin → first-seen ISO timestamp. Missing/corrupt → empty (fail-open:
    the gate then sees no ages and blocks nothing — degrade, don't crash)."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("position ledger unreadable (%s) — degrading to empty", e)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k.upper(): v
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def write_ledger(state: dict[str, str], path: Path = DEFAULT_LEDGER_PATH) -> None:
    """Atomic write (tmp + rename), mirroring the watcher baseline writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def update_ledger_and_ages(
    snap: Any,
    *,
    now: datetime,
    path: Path = DEFAULT_LEDGER_PATH,
) -> dict[str, float]:
    """Reconcile the ledger against the live snapshot and return per-coin age
    in HOURS for every currently-held non-stable coin.

    - existing coin → keep its stored first-seen (age accrues across cycles)
    - new coin → stamp `now` (age 0)
    - coin no longer held → dropped (a later re-entry pays friction again, so
      it correctly restarts the clock)
    """
    prior = read_ledger(path)
    held = held_nonstable_coins(snap)
    next_ledger: dict[str, str] = {}
    ages: dict[str, float] = {}
    for coin in held:
        first_dt = _parse_iso(prior.get(coin)) or now
        next_ledger[coin] = first_dt.isoformat()
        ages[coin] = max(0.0, (now - first_dt).total_seconds() / 3600.0)
    write_ledger(next_ledger, path)
    return ages
