"""Lightweight event watcher — polls cheap signals + emits events
when a value crosses a threshold defined in
`brain/01-projects/vault8004/notes/event-taxonomy.md` (epic
`event-driven-rebalance.1`).

Scope (.2): owns the polling + event emission. NEVER calls Anthropic,
NEVER rebuilds a full snapshot. The signal channel that wakes the main
loop on a P0 event is wired in `.3`; for now we just write event records
to `events/<ts>.jsonl` and let `.3` consume that stream.

Baseline file (`state/watcher-baseline.json`) is updated by the main
loop after every decided cycle via `update_baseline_from_snapshot` —
the watcher only READS it during polling.

CLI:
    # one shot (for smoke + tests)
    python -m agent.sandbox.watcher --once

    # standalone poll loop (2-min cadence)
    python -m agent.sandbox.watcher --interval 120
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from agent.bybit_oracle.bybit_client import BybitClient

log = logging.getLogger(__name__)


# ───────────────────────── thresholds (from .1 taxonomy) ──────────────

class Thresholds:
    """Numeric thresholds locked in `event-taxonomy.md`. Mutable here
    only via empirical tuning after `event-driven-rebalance.8` cost
    tracking ships."""

    PRICE_DRIFT_PCT = Decimal("0.05")  # ±5% on hedged non-stable
    FUNDING_EPSILON = Decimal("0.0001")  # |rate| must exceed this to count as flip
    PEG_DEVIATION_BPS = Decimal("50")  # ±50 bps from $1.00
    DA_SETTLEMENT_WINDOW_SEC = 30 * 60  # P1
    DA_SETTLEMENT_URGENT_SEC = 10 * 60  # P0 bump
    YIELD_JUMP_MULTIPLIER = Decimal("2.0")
    YIELD_JUMP_MIN_BASELINE_BPS = Decimal("500")  # noise floor — ignore tiny APRs
    LM_LIQ_DISTANCE_THRESHOLD = Decimal("0.10")
    # Perp short distance-to-liquidation: (liqPrice - mark) / mark.
    # At 1x leverage liqPrice ≈ 1.95 × mark on entry → initial distance
    # ≈ 0.95. Threshold 0.50 trips when liqPrice ≤ 1.50 × mark, i.e.
    # the underlying has moved ~+30% against the short — perp lost ~30%
    # of margin and ~70% still recoverable on voluntary close. Tight
    # enough to preserve material margin in a tail event, loose enough
    # to not churn on routine 5-10% moves. Mirror LM_LIQ_DISTANCE_THRESHOLD
    # but per-leverage-class: LM caps leverage at 7, so 10% is tight;
    # hedge perps are 1x so we can afford the looser cap.
    PERP_LIQ_DISTANCE_THRESHOLD = Decimal("0.50")


# Defaults for `Pick.invalidate_at` when the LLM didn't set one. Indexed
# by (category, is_stable). Stables only get a peg-deviation default;
# non-stables get a price-drift + funding floor pair. The fields here
# mirror `agent.reason.schema.InvalidateAt` semantics: price_drift_pct
# is "fraction adverse move from entry mark" (not absolute price),
# funding_7d_below is per-8h signed Decimal, peg_dev_above_bps is
# absolute deviation from $1.00. 2026-06-03 introduction; tuned vs the
# existing `check_price_drift` (5% heads-up) and `check_funding_flip`
# (sign-change) so the invalidate fires at a strictly more conservative
# level — those events suggest action, invalidate forces a close.
DEFAULT_INVALIDATE_BY_CATEGORY: dict[tuple[str, str], dict[str, Decimal]] = {
    ("FlexibleSaving", "STABLE"): {"peg_dev_above_bps": Decimal("200")},
    ("OnChain", "STABLE"): {"peg_dev_above_bps": Decimal("200")},
    ("FlexibleSaving", "NON_STABLE"): {
        "price_drift_pct": Decimal("0.30"),
        "funding_7d_below": Decimal("-0.0002"),
    },
    ("OnChain", "NON_STABLE"): {
        "price_drift_pct": Decimal("0.30"),
        "funding_7d_below": Decimal("-0.0002"),
    },
    # LM and Alpha rely on their own checkers (lm_liq_distance,
    # price_drift on alpha). No invalidate defaults — operator can set
    # explicit `invalidate_at.liq_distance_below` per LM pick if they
    # want tighter than the global 0.10 threshold.
}


# Stable coins — peg drift checked against $1, never flagged for funding
# flip or price drift (a USDC mark moving 5% is itself depeg).
STABLE_COINS: frozenset[str] = frozenset({"USDC", "USDT", "USDE", "USD1", "USDTB", "DAI"})


# ───────────────────────── models ─────────────────────────────────────

Severity = Literal["P0", "P1", "P2"]


class EventRecord(BaseModel):
    """One detected event. Written one-per-line to `events/<date>.jsonl`."""
    ts: datetime
    kind: str
    severity: Severity
    position_id: str | None = None
    coin: str | None = None
    baseline: dict[str, Any] = Field(default_factory=dict)
    current: dict[str, Any] = Field(default_factory=dict)
    threshold: dict[str, Any] = Field(default_factory=dict)
    message: str


class HeldPosition(BaseModel):
    """One row in the baseline file — a position we're watching.

    `position_id` is a venue-qualified string ("earn:1131",
    "advance_earn:DA-123", "lm:LM-456") — keeps cross-venue ids
    unambiguous when written into events.
    """
    position_id: str
    venue: Literal["earn", "advance_earn", "lm", "alpha", "perp", "hold_to_earn"]
    coin: str
    entry_mark_price: Decimal | None = None
    last_funding_rate: Decimal | None = None
    last_measured_yield_bps: Decimal | None = None
    last_liq_distance: Decimal | None = None
    settle_time_ts: int | None = None


class WatcherBaseline(BaseModel):
    """Snapshot of reference values the watcher compares against."""
    captured_at: datetime
    snapshot_filename: str | None = None
    positions: list[HeldPosition] = Field(default_factory=list)
    known_h2e_product_ids: list[str] = Field(default_factory=list)


# ───────────────────────── paths ──────────────────────────────────────

DEFAULT_BASELINE_PATH = Path(__file__).parent / "state" / "watcher-baseline.json"
DEFAULT_EVENTS_DIR = Path(__file__).parent / "events"
DEFAULT_DECISIONS_DIR = Path(__file__).parent / "decisions"


def _read_latest_decision(
    decisions_dir: Path = DEFAULT_DECISIONS_DIR,
) -> dict[str, Any] | None:
    """Return the most recent decision JSON in `decisions_dir`, or None.
    Mirrors `agent.sandbox.decide._load_latest_prior_decision` but kept
    self-contained to avoid the watcher importing the LLM-orchestration
    module (and its anthropic dependency)."""
    if not decisions_dir.is_dir():
        return None
    files = sorted(p for p in decisions_dir.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ───────────────────────── baseline IO ────────────────────────────────

def read_baseline(path: Path = DEFAULT_BASELINE_PATH) -> WatcherBaseline | None:
    """Read baseline state. Returns None if the file doesn't exist —
    caller treats this as "no positions to watch, just poll global
    signals (peg, new H2E)".
    """
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return WatcherBaseline.model_validate(raw)


def write_baseline(b: WatcherBaseline, path: Path = DEFAULT_BASELINE_PATH) -> None:
    """Atomic write: serialize to a temp file in the same directory and
    rename over the target. Prevents the watcher reading a half-written
    file mid-update.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(b.model_dump_json(indent=2))
    os.replace(tmp, path)


def update_baseline_from_snapshot(
    snap: dict[str, Any],
    path: Path = DEFAULT_BASELINE_PATH,
    snapshot_filename: str | None = None,
) -> WatcherBaseline:
    """Build a fresh baseline from a just-completed decided snapshot
    and write it. Called by the main loop AFTER a successful
    `run_one_cycle` (the hook itself lands in `.3`).

    Only fields the watcher actually compares against are extracted —
    everything else stays in the snapshot file as the source of truth.
    """
    captured_at = _parse_dt(snap.get("captured_at")) or datetime.now(UTC)
    positions: list[HeldPosition] = []

    # Basic-Earn positions (FlexibleSaving / OnChain)
    for p in snap.get("earn_positions") or []:
        amount = _to_decimal(p.get("amount"))
        if amount is None or amount == 0:
            continue
        positions.append(
            HeldPosition(
                position_id=f"earn:{p.get('productId') or p.get('id') or ''}",
                venue="earn",
                coin=str(p.get("coin") or ""),
                last_measured_yield_bps=_to_decimal(p.get("measured_yield_bps")),
            )
        )

    # LM positions
    for p in snap.get("lm_positions") or []:
        positions.append(
            HeldPosition(
                position_id=f"lm:{p.get('positionId') or p.get('id') or ''}",
                venue="lm",
                coin=str(p.get("coin") or p.get("baseCoin") or ""),
                last_liq_distance=_to_decimal(p.get("liquidation_distance_pct")),
            )
        )

    # Alpha positions (held DEX tokens — price-drift candidates if
    # we ever hedge them; for now we still track the mark so the
    # cycle's `prior_decision` context has it).
    for p in snap.get("alpha_positions") or []:
        positions.append(
            HeldPosition(
                position_id=f"alpha:{p.get('tokenCode') or p.get('symbol') or ''}",
                venue="alpha",
                coin=str(p.get("symbol") or ""),
                entry_mark_price=_to_decimal(p.get("currentPrice") or p.get("price")),
            )
        )

    # Perp positions (the hedge leg). Funding flip + price drift
    # checked off the linear ticker for `symbol`; `last_liq_distance`
    # is the prior cycle's distance-to-liquidation so the watcher's
    # `perp_liq_distance` checker can detect "newly breached"
    # transitions (vs already-breached and noisy).
    for p in snap.get("perp_positions") or []:
        symbol = str(p.get("symbol") or "")
        coin = symbol.removesuffix("USDT") if symbol.endswith("USDT") else symbol
        mark_p = _to_decimal(p.get("markPrice") or p.get("entryPrice"))
        liq_p = _to_decimal(p.get("liqPrice"))
        last_dist: Decimal | None = None
        if (
            mark_p is not None and mark_p > 0
            and liq_p is not None and liq_p > 0
            and liq_p > mark_p
        ):
            last_dist = (liq_p - mark_p) / mark_p
        positions.append(
            HeldPosition(
                position_id=f"perp:{symbol}",
                venue="perp",
                coin=coin,
                entry_mark_price=mark_p,
                last_funding_rate=_to_decimal(p.get("fundingRate")),
                last_liq_distance=last_dist,
            )
        )

    # Hold-to-Earn product ID set (for new-product diff).
    known_h2e_ids: list[str] = []
    for prod in (snap.get("products") or {}).get("HoldToEarn") or []:
        pid = prod.get("product_id") or prod.get("productId")
        if pid:
            known_h2e_ids.append(str(pid))

    baseline = WatcherBaseline(
        captured_at=captured_at,
        snapshot_filename=snapshot_filename,
        positions=positions,
        known_h2e_product_ids=known_h2e_ids,
    )
    write_baseline(baseline, path)
    return baseline


# ───────────────────────── checkers (pure) ────────────────────────────

def check_price_drift(
    position: HeldPosition, current_mark: Decimal
) -> EventRecord | None:
    """Event #1: hedged non-stable mark drift > ±5% vs baseline."""
    if position.coin.upper() in STABLE_COINS:
        return None
    if position.entry_mark_price is None or position.entry_mark_price <= 0:
        return None
    drift = (current_mark / position.entry_mark_price) - Decimal(1)
    if abs(drift) < Thresholds.PRICE_DRIFT_PCT:
        return None
    return EventRecord(
        ts=datetime.now(UTC),
        kind="price_drift",
        severity="P0",
        position_id=position.position_id,
        coin=position.coin,
        baseline={"entry_mark_price": str(position.entry_mark_price)},
        current={"mark_price": str(current_mark)},
        threshold={"max_drift_pct": str(Thresholds.PRICE_DRIFT_PCT)},
        message=(
            f"{position.coin} mark drifted "
            f"{drift * Decimal(100):+.2f}% "
            f"(entry {position.entry_mark_price} → current {current_mark})"
        ),
    )


def check_funding_flip(
    position: HeldPosition, current_rate: Decimal
) -> EventRecord | None:
    """Event #2: funding rate flipped sign on a held non-stable.

    Filters out sub-epsilon noise around zero (so a 0.00001 → -0.00001
    blip doesn't fire).
    """
    if position.coin.upper() in STABLE_COINS:
        return None
    if position.last_funding_rate is None:
        return None
    base = position.last_funding_rate
    if abs(base) <= Thresholds.FUNDING_EPSILON or abs(current_rate) <= Thresholds.FUNDING_EPSILON:
        return None
    if (base > 0) == (current_rate > 0):
        return None
    return EventRecord(
        ts=datetime.now(UTC),
        kind="funding_flip",
        severity="P0",
        position_id=position.position_id,
        coin=position.coin,
        baseline={"funding_rate": str(base)},
        current={"funding_rate": str(current_rate)},
        threshold={"epsilon": str(Thresholds.FUNDING_EPSILON)},
        message=(
            f"{position.coin} funding flipped "
            f"{base} → {current_rate}"
        ),
    )


def check_peg_drift(current_price: Decimal, coin: str = "USDC") -> EventRecord | None:
    """Event #3: stable coin price > ±50 bps from $1.00."""
    deviation_bps = (current_price - Decimal(1)) * Decimal(10_000)
    if abs(deviation_bps) < Thresholds.PEG_DEVIATION_BPS:
        return None
    return EventRecord(
        ts=datetime.now(UTC),
        kind="peg_drift",
        severity="P0",
        coin=coin,
        baseline={"target_price": "1.0"},
        current={"price_usd": str(current_price), "deviation_bps": str(deviation_bps)},
        threshold={"max_deviation_bps": str(Thresholds.PEG_DEVIATION_BPS)},
        message=f"{coin} peg drifted {deviation_bps:+.2f} bps from $1.00",
    )


def check_da_settlement(
    position: HeldPosition, now_ts: int
) -> EventRecord | None:
    """Event #4: advance-Earn settlement window approaching.

    P1 when ≤ 30min, bumps to P0 when ≤ 10min (a 4h heartbeat would miss
    the latter entirely).
    """
    if position.settle_time_ts is None:
        return None
    seconds_to_settle = position.settle_time_ts - now_ts
    if seconds_to_settle > Thresholds.DA_SETTLEMENT_WINDOW_SEC:
        return None
    if seconds_to_settle <= 0:
        return None  # already settled — main loop will reconcile on next cycle
    severity: Severity = (
        "P0" if seconds_to_settle <= Thresholds.DA_SETTLEMENT_URGENT_SEC else "P1"
    )
    return EventRecord(
        ts=datetime.now(UTC),
        kind="da_settlement_window",
        severity=severity,
        position_id=position.position_id,
        coin=position.coin,
        baseline={"settle_time_ts": position.settle_time_ts},
        current={"now_ts": now_ts, "seconds_remaining": seconds_to_settle},
        threshold={
            "window_sec": Thresholds.DA_SETTLEMENT_WINDOW_SEC,
            "urgent_sec": Thresholds.DA_SETTLEMENT_URGENT_SEC,
        },
        message=(
            f"advance-Earn {position.position_id} settles in "
            f"{seconds_to_settle}s ({severity})"
        ),
    )


def check_new_hold_to_earn(
    known_ids: list[str], current_ids: list[str]
) -> EventRecord | None:
    """Event #5: any new Hold-to-Earn product ID."""
    known = set(known_ids)
    current = set(current_ids)
    new = sorted(current - known)
    if not new:
        return None
    return EventRecord(
        ts=datetime.now(UTC),
        kind="new_hold_to_earn",
        severity="P1",
        baseline={"known_count": len(known_ids)},
        current={"new_ids": new, "total_count": len(current_ids)},
        threshold={"min_new": 1},
        message=f"{len(new)} new Hold-to-Earn product(s): {new}",
    )


def check_yield_jump(
    position: HeldPosition, current_yield_bps: Decimal
) -> EventRecord | None:
    """Event #6: measured APR jumped ≥ 2x vs baseline, AND baseline was
    already ≥ 500 bps (noise floor — a 1bp → 2bp move is meaningless)."""
    if position.last_measured_yield_bps is None:
        return None
    base = position.last_measured_yield_bps
    if base < Thresholds.YIELD_JUMP_MIN_BASELINE_BPS:
        return None
    if base <= 0:
        return None
    if current_yield_bps / base < Thresholds.YIELD_JUMP_MULTIPLIER:
        return None
    return EventRecord(
        ts=datetime.now(UTC),
        kind="measured_yield_jump",
        severity="P1",
        position_id=position.position_id,
        coin=position.coin,
        baseline={"yield_bps": str(base)},
        current={"yield_bps": str(current_yield_bps)},
        threshold={
            "multiplier": str(Thresholds.YIELD_JUMP_MULTIPLIER),
            "min_baseline_bps": str(Thresholds.YIELD_JUMP_MIN_BASELINE_BPS),
        },
        message=(
            f"{position.coin} measured APR jumped "
            f"{base}bps → {current_yield_bps}bps "
            f"({(current_yield_bps / base):.2f}x)"
        ),
    )


def check_lm_liq_distance(
    position: HeldPosition, current_distance: Decimal
) -> EventRecord | None:
    """Event #7: leveraged-LM distance-to-liquidation ≤ 10%."""
    if current_distance > Thresholds.LM_LIQ_DISTANCE_THRESHOLD:
        return None
    last_dist = (
        str(position.last_liq_distance)
        if position.last_liq_distance is not None
        else None
    )
    return EventRecord(
        ts=datetime.now(UTC),
        kind="lm_liquidation_distance",
        severity="P0",
        position_id=position.position_id,
        coin=position.coin,
        baseline={"last_distance": last_dist},
        current={"distance": str(current_distance)},
        threshold={"min_distance": str(Thresholds.LM_LIQ_DISTANCE_THRESHOLD)},
        message=(
            f"LM {position.position_id} liq distance "
            f"{current_distance * Decimal(100):.2f}% ≤ "
            f"{Thresholds.LM_LIQ_DISTANCE_THRESHOLD * Decimal(100):.0f}%"
        ),
    )


def check_perp_liq_distance(
    position: HeldPosition,
    mark_price: Decimal,
    liq_price: Decimal,
) -> EventRecord | None:
    """Event #8: hedge perp's distance-to-liquidation breaches threshold.

    For short positions, liqPrice > mark; distance = (liq - mark) / mark.
    A smaller distance means a rising underlying has eaten into the
    short's margin. We fire when distance ≤ PERP_LIQ_DISTANCE_THRESHOLD
    so the next cycle's LLM closes both legs (perp + paired spot Earn
    on the same coin) before Bybit force-liquidates.

    Skips:
      - non-short positions (we only auto-hedge with shorts),
      - missing or zero liqPrice (Bybit didn't compute one yet —
        fresh position, flat row, or isolated-margin with cross-only data).
    """
    if mark_price <= 0 or liq_price <= 0:
        return None
    # Only shorts are at risk on the upside; longs (we don't open them
    # as hedges) would have liq < mark and a different formula. Bybit's
    # liqPrice > mark unambiguously implies short with rising-mark risk.
    if liq_price <= mark_price:
        return None
    distance = (liq_price - mark_price) / mark_price
    if distance > Thresholds.PERP_LIQ_DISTANCE_THRESHOLD:
        return None
    last_dist = (
        str(position.last_liq_distance)
        if position.last_liq_distance is not None
        else None
    )
    return EventRecord(
        ts=datetime.now(UTC),
        kind="perp_liquidation_distance",
        severity="P0",
        position_id=position.position_id,
        coin=position.coin,
        baseline={"last_distance": last_dist},
        current={
            "distance": str(distance),
            "mark_price": str(mark_price),
            "liq_price": str(liq_price),
        },
        threshold={"min_distance": str(Thresholds.PERP_LIQ_DISTANCE_THRESHOLD)},
        message=(
            f"perp {position.coin} short liq distance "
            f"{distance * Decimal(100):.1f}% ≤ "
            f"{Thresholds.PERP_LIQ_DISTANCE_THRESHOLD * Decimal(100):.0f}% "
            f"(mark ${mark_price}, liq ${liq_price})"
        ),
    )


def check_pick_invalidation(
    decision: dict[str, Any] | None,
    baseline: WatcherBaseline,
    snapshot_signals: dict[str, dict[str, Decimal | None]],
    peg_dev_bps: Decimal | None = None,
) -> list[EventRecord]:
    """Event #9: per-pick invalidation thresholds (operator-defined OR
    category default) breached against current signals.

    Inputs:
      decision         — latest decision dict (None ⇒ no defaults to
                         apply, no events fire).
      baseline         — watcher baseline; uses entry_mark_price for
                         per-pick price drift calculation.
      snapshot_signals — `{coin_upper: {mark_price, funding_7d}}` keyed
                         per coin we want to check. Caller assembles it
                         from the linear tickers it already pulled.
      peg_dev_bps      — current USDC peg deviation (signed bps from $1).
                         Only meaningful for the USDC ⇄ $1.00 invalidate
                         since stables-other-than-USDC aren't tracked on
                         CoinGecko in this snapshot layer; non-USDC
                         stable invalidate uses USDC as a proxy here.

    Each fired event references the pick's category + product_id + the
    specific threshold breached. Severity P0 — next cycle MUST close.
    """
    if not decision or not isinstance(decision, dict):
        return []
    events: list[EventRecord] = []
    entry_by_coin = {
        p.coin.upper(): p.entry_mark_price
        for p in baseline.positions
        if p.coin and p.venue == "perp" and p.entry_mark_price
    }
    venue_to_category = {
        "bybit_flex": "FlexibleSaving",
        "bybit_onchain": "OnChain",
    }
    for v in decision.get("venues", []) or []:
        venue_id = v.get("venue_id") or v.get("venueId")
        category = venue_to_category.get(venue_id)
        if not category:
            # Only flex / onchain wired — LM has its own dedicated check,
            # advance-Earn invalidates via the settlement window.
            continue
        for pick in v.get("picks", []) or []:
            product_id = str(pick.get("product_id") or pick.get("productId") or "")
            if not product_id:
                continue
            # Resolve the pick's coin via the baseline (it's the same
            # productId-to-coin link captured in `update_baseline_from_snapshot`).
            # When baseline doesn't carry it, skip — we can't compute
            # price drift without an entry mark to compare against.
            held = next(
                (
                    p for p in baseline.positions
                    if p.position_id == f"earn:{product_id}"
                ),
                None,
            )
            if held is None:
                continue
            coin = (held.coin or "").upper()
            if not coin:
                continue
            is_stable = coin in STABLE_COINS
            custom = pick.get("invalidate_at") or {}
            defaults = DEFAULT_INVALIDATE_BY_CATEGORY.get(
                (category, "STABLE" if is_stable else "NON_STABLE"), {}
            )

            def _eff(key: str) -> Decimal | None:
                v_custom = custom.get(key)
                if v_custom is not None:
                    try:
                        return Decimal(str(v_custom))
                    except (InvalidOperation, TypeError):
                        return None
                return defaults.get(key)

            signals = snapshot_signals.get(coin, {}) or {}
            mark = signals.get("mark_price")
            funding = signals.get("funding_7d")

            # Peg deviation (stables only). USDC peg comes from CoinGecko
            # via `_fetch_peg_usd`; for non-USDC stables we don't have a
            # second peg source so we proxy with USDC's deviation —
            # imperfect but the same anchor catches systemic stable stress.
            peg_thresh = _eff("peg_dev_above_bps")
            if is_stable and peg_thresh is not None and peg_dev_bps is not None:
                if abs(peg_dev_bps) > peg_thresh:
                    events.append(
                        EventRecord(
                            ts=datetime.now(UTC),
                            kind="pick_invalidated",
                            severity="P0",
                            position_id=f"earn:{product_id}",
                            coin=coin,
                            baseline={"peg_dev_bps_baseline": "0"},
                            current={"peg_dev_bps": str(peg_dev_bps)},
                            threshold={"peg_dev_above_bps": str(peg_thresh)},
                            message=(
                                f"pick {category}/{product_id} ({coin}) "
                                f"invalidated — peg dev {peg_dev_bps:+} bps "
                                f"|exceeds| {peg_thresh} bps"
                            ),
                        )
                    )

            # Non-stable price drift (fraction adverse from entry mark).
            drift_thresh = _eff("price_drift_pct")
            entry_mark = entry_by_coin.get(coin)
            if (
                not is_stable
                and drift_thresh is not None
                and mark is not None
                and entry_mark is not None
                and entry_mark > 0
            ):
                # Earn picks are LONG exposure on `coin`; an adverse
                # move is DOWN. Fire when mark fell by >= threshold.
                drop = (entry_mark - mark) / entry_mark
                if drop >= drift_thresh:
                    events.append(
                        EventRecord(
                            ts=datetime.now(UTC),
                            kind="pick_invalidated",
                            severity="P0",
                            position_id=f"earn:{product_id}",
                            coin=coin,
                            baseline={"entry_mark_price": str(entry_mark)},
                            current={"mark_price": str(mark)},
                            threshold={"price_drift_pct": str(drift_thresh)},
                            message=(
                                f"pick {category}/{product_id} ({coin}) "
                                f"invalidated — mark fell "
                                f"{drop * Decimal(100):.1f}% from entry "
                                f"(threshold {drift_thresh * Decimal(100):.0f}%)"
                            ),
                        )
                    )

            # Absolute price thresholds (override of drift-based default).
            price_below = _eff("price_below")
            if (
                not is_stable
                and price_below is not None
                and mark is not None
                and mark < price_below
            ):
                events.append(
                    EventRecord(
                        ts=datetime.now(UTC),
                        kind="pick_invalidated",
                        severity="P0",
                        position_id=f"earn:{product_id}",
                        coin=coin,
                        baseline={},
                        current={"mark_price": str(mark)},
                        threshold={"price_below": str(price_below)},
                        message=(
                            f"pick {category}/{product_id} ({coin}) "
                            f"invalidated — mark ${mark} below operator "
                            f"floor ${price_below}"
                        ),
                    )
                )

            price_above = _eff("price_above")
            if (
                not is_stable
                and price_above is not None
                and mark is not None
                and mark > price_above
            ):
                events.append(
                    EventRecord(
                        ts=datetime.now(UTC),
                        kind="pick_invalidated",
                        severity="P0",
                        position_id=f"earn:{product_id}",
                        coin=coin,
                        baseline={},
                        current={"mark_price": str(mark)},
                        threshold={"price_above": str(price_above)},
                        message=(
                            f"pick {category}/{product_id} ({coin}) "
                            f"invalidated — mark ${mark} above operator "
                            f"ceiling ${price_above}"
                        ),
                    )
                )

            # Funding 7d sustained below threshold (non-stable hedged).
            funding_thresh = _eff("funding_7d_below")
            if (
                not is_stable
                and funding_thresh is not None
                and funding is not None
                and funding < funding_thresh
            ):
                events.append(
                    EventRecord(
                        ts=datetime.now(UTC),
                        kind="pick_invalidated",
                        severity="P0",
                        position_id=f"earn:{product_id}",
                        coin=coin,
                        baseline={},
                        current={"funding_7d": str(funding)},
                        threshold={"funding_7d_below": str(funding_thresh)},
                        message=(
                            f"pick {category}/{product_id} ({coin}) "
                            f"invalidated — funding 7d avg {funding}/8h "
                            f"below {funding_thresh}/8h (hedge net cost)"
                        ),
                    )
                )
    return events


# ───────────────────────── polling orchestration ──────────────────────

async def _fetch_peg_usd(timeout: float = 5.0) -> Decimal | None:
    """Mirrors `snapshot._fetch_usdc_peg` but light + return-only-price.
    Open question from `.1`: confirm CoinGecko free-tier rate-limit
    (~30 req/min) covers the watcher's max cadence (60s = 1 req/min). It
    does.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "usd-coin", "vs_currencies": "usd"},
            )
            resp.raise_for_status()
            data = resp.json()
            return Decimal(str(data["usd-coin"]["usd"]))
    except (httpx.HTTPError, KeyError, ValueError, InvalidOperation) as e:
        log.warning("peg fetch failed: %s", e)
        return None


def _coin_to_perp_symbol(coin: str) -> str:
    """Bybit linear convention: <COIN>USDT (USDT-settled perps).
    Caller is responsible for skipping stables (no perp exists).
    """
    return f"{coin.upper()}USDT"


def _perp_to_earn_product_id(symbol: str, baseline: WatcherBaseline) -> str:
    """Resolve the held Earn productId for a given perp symbol's coin.
    Used by the perp-stopped-out detector to attach the right
    position_id (`earn:<pid>`) so the auto-close path can match it back
    to the pick in the decision file. Returns empty string when no
    matching Earn position is held — caller falls back to the perp
    position_id."""
    coin = symbol.removesuffix("USDT") if symbol.endswith("USDT") else symbol
    coin_u = coin.upper()
    for p in baseline.positions:
        if p.venue == "earn" and (p.coin or "").upper() == coin_u:
            return p.position_id.removeprefix("earn:")
    return ""


async def poll_once(
    client: BybitClient, baseline: WatcherBaseline
) -> list[EventRecord]:
    """Run one polling cycle: gather signals, run all checkers, return
    the list of fired events.

    Per-signal failures are swallowed with a warning — a CoinGecko 429
    must not stop the price-drift checker from running off the Bybit
    tickers. Returned event list may be empty (no thresholds crossed).
    """
    events: list[EventRecord] = []
    now_ts = int(datetime.now(UTC).timestamp())

    # ── Global signals (no held-position prerequisite) ──────────────

    # Peg (event #3)
    peg_price = await _fetch_peg_usd()
    if peg_price is not None and (ev := check_peg_drift(peg_price, coin="USDC")):
        events.append(ev)

    # New Hold-to-Earn product (event #5)
    try:
        h2e_now = await client.list_hold_to_earn_products()
        current_ids = [
            str(p.get("productId") or p.get("id") or "") for p in h2e_now
        ]
        current_ids = [pid for pid in current_ids if pid]
        if ev := check_new_hold_to_earn(baseline.known_h2e_product_ids, current_ids):
            events.append(ev)
    except Exception as e:  # noqa: BLE001 — defensive: H2E may 401/rate-limit
        log.warning("hold-to-earn poll failed: %s", e)

    # ── Per-position signals (skip if no positions held) ─────────────

    non_stable_coins = {
        p.coin
        for p in baseline.positions
        if p.coin and p.coin.upper() not in STABLE_COINS
    }

    # Pull linear tickers once for ALL non-stable coins we hold — single
    # endpoint call returns mark + funding for every symbol.
    tickers_by_symbol: dict[str, dict[str, Any]] = {}
    if non_stable_coins:
        try:
            tickers = await client.get_tickers(category="linear")
            for t in tickers:
                sym = getattr(t, "symbol", None) or (
                    t.get("symbol") if isinstance(t, dict) else None
                )
                if sym:
                    tickers_by_symbol[sym] = t if isinstance(t, dict) else t.model_dump()
        except Exception as e:  # noqa: BLE001
            log.warning("tickers poll failed: %s", e)

    for pos in baseline.positions:
        coin_upper = pos.coin.upper() if pos.coin else ""

        # Events #1 + #2 — non-stable mark + funding
        if coin_upper and coin_upper not in STABLE_COINS:
            symbol = _coin_to_perp_symbol(pos.coin)
            ticker = tickers_by_symbol.get(symbol)
            if ticker:
                mark = _to_decimal(ticker.get("markPrice") or ticker.get("lastPrice"))
                funding = _to_decimal(ticker.get("fundingRate"))
                if mark is not None and (ev := check_price_drift(pos, mark)):
                    events.append(ev)
                if funding is not None and (ev := check_funding_flip(pos, funding)):
                    events.append(ev)

        # Event #4 — advance-Earn settle window
        if pos.venue == "advance_earn" and (ev := check_da_settlement(pos, now_ts)):
            events.append(ev)

        # Event #7 — LM liq distance (single endpoint, fan-out across all
        # LM positions; only fetch if at least one held).
        # (Handled below in a single batched call.)

    # Event #8 batch — re-fetch perp positions only when we hold at
    # least one short hedge. One `/v5/position/list?category=linear`
    # call returns liqPrice + markPrice for every open position; we
    # match by symbol back to the baseline.
    perp_positions = [p for p in baseline.positions if p.venue == "perp"]
    if perp_positions:
        try:
            live_perps = await client.get_positions(category="linear", settle_coin="USDT")
            live_by_symbol = {
                p.symbol: p
                for p in live_perps
                if p.side == "Sell" and p.size and Decimal(p.size) > 0
            }
            for pos in perp_positions:
                symbol = pos.position_id.removeprefix("perp:")
                live = live_by_symbol.get(symbol)
                if live is None:
                    # Perp WAS in baseline but no longer open. Two cases:
                    #   1. We held a paired Earn long on the same coin —
                    #      Bybit (stop / TP / liq) closed the perp out
                    #      from under us, paired Earn is now naked, fire
                    #      pick_invalidated so auto-close redeems.
                    #   2. No paired Earn — the perp was closed by our
                    #      own planner this cycle, or it's a stale
                    #      baseline entry. Nothing to clean up, skip.
                    earn_pid = _perp_to_earn_product_id(symbol, baseline)
                    if not earn_pid:
                        continue
                    events.append(
                        EventRecord(
                            ts=datetime.now(UTC),
                            kind="pick_invalidated",
                            severity="P0",
                            position_id=f"earn:{earn_pid}",
                            coin=pos.coin,
                            baseline={"perp_symbol": symbol},
                            current={"open_size": "0"},
                            threshold={},
                            message=(
                                f"perp {symbol} closed outside the agent "
                                f"(Bybit stop / TP / liquidation) — paired "
                                f"Earn long is now naked, auto-closing"
                            ),
                        )
                    )
                    continue
                mark = _to_decimal(live.markPrice)
                liq = _to_decimal(live.liqPrice)
                if mark is None or liq is None:
                    continue
                if ev := check_perp_liq_distance(pos, mark, liq):
                    events.append(ev)
        except Exception as e:  # noqa: BLE001
            log.warning("perp positions poll failed: %s", e)

    # Event #9 — per-pick invalidation (operator-set or category default).
    # Reads the latest decision from the standard decisions dir; if none
    # exists or the file is unreadable, no events fire (safe no-op).
    try:
        decision = _read_latest_decision()
    except Exception as e:  # noqa: BLE001
        log.warning("decision read failed: %s", e)
        decision = None
    if decision is not None:
        # Assemble per-coin signal map from the tickers we already pulled
        # (price drift + funding) — no extra Bybit round-trips.
        signals: dict[str, dict[str, Decimal | None]] = {}
        for sym, t in tickers_by_symbol.items():
            coin = sym.removesuffix("USDT") if sym.endswith("USDT") else sym
            signals[coin.upper()] = {
                "mark_price": _to_decimal(
                    t.get("markPrice") or t.get("lastPrice")
                ),
                "funding_7d": _to_decimal(t.get("fundingRate")),
            }
        peg_dev_bps: Decimal | None = None
        if peg_price is not None:
            peg_dev_bps = (peg_price - Decimal("1.0")) * Decimal("10000")
        events.extend(
            check_pick_invalidation(
                decision=decision,
                baseline=baseline,
                snapshot_signals=signals,
                peg_dev_bps=peg_dev_bps,
            )
        )

    # Event #7 batch — re-fetch LM positions only when we hold at least one
    lm_positions = [p for p in baseline.positions if p.venue == "lm"]
    if lm_positions:
        try:
            live_lm = await client.get_liquidity_mining_positions()
            live_by_id = {
                str(item.get("positionId") or item.get("id") or ""): item
                for item in live_lm
            }
            for pos in lm_positions:
                raw_id = pos.position_id.removeprefix("lm:")
                live = live_by_id.get(raw_id)
                if not live:
                    continue
                dist = _lm_liq_distance(live)
                if dist is not None and (ev := check_lm_liq_distance(pos, dist)):
                    events.append(ev)
        except Exception as e:  # noqa: BLE001
            log.warning("lm positions poll failed: %s", e)

    # Event #6 — measured-yield jump. Per `.1` open question, hourly-yield
    # is one Bybit call per held Earn position. We poll it here only on
    # held Earn rows (already filtered by `update_baseline_from_snapshot`).
    # Conservative cadence: only when baseline has at least one Earn pos.
    # NOTE: deferred to `.3` once we confirm the rate-limit budget — the
    # checker is wired but the data fetch is stubbed. Tests still exercise
    # `check_yield_jump` directly.

    return events


# ───────────────────────── event sink ─────────────────────────────────

def write_events(events: list[EventRecord], events_dir: Path = DEFAULT_EVENTS_DIR) -> Path | None:
    """Append events to today's `<YYYYMMDD>.jsonl` file. Returns the file
    path written to, or None on empty input.
    """
    if not events:
        return None
    events_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y%m%d")
    target = events_dir / f"{day}.jsonl"
    with target.open("a") as f:
        for ev in events:
            f.write(ev.model_dump_json() + "\n")
    return target


# ───────────────────────── runner ─────────────────────────────────────

async def run_watcher(
    *,
    interval_seconds: float,
    once: bool = False,
    baseline_path: Path = DEFAULT_BASELINE_PATH,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Standalone watcher loop. Polls every `interval_seconds`, writes
    fired events to `events_dir`. Exits cleanly between cycles on SIGINT
    / SIGTERM.

    Co-process model (watcher + main loop in separate processes) is the
    `.3` decision point — for now this runner is the smoke-test entry
    point + future production fallback if we keep them separate.
    """
    stop_event = stop_event or asyncio.Event()
    _install_signal_handlers(stop_event)

    async with BybitClient.from_settings() as client:
        while not stop_event.is_set():
            baseline = read_baseline(baseline_path)
            if baseline is None:
                log.warning(
                    "no baseline at %s — polling global signals only "
                    "(main loop hasn't completed a decided cycle yet)",
                    baseline_path,
                )
                baseline = WatcherBaseline(captured_at=datetime.now(UTC))
            events = await poll_once(client, baseline)
            if events:
                written = write_events(events, events_dir)
                log.info(
                    "polled: %d event(s) fired → %s",
                    len(events),
                    written.name if written else "(none)",
                )
                for ev in events:
                    log.info("  %s [%s] %s", ev.kind, ev.severity, ev.message)
            else:
                log.info("polled: no events")
            if once or stop_event.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            return


# ───────────────────────── helpers ────────────────────────────────────

def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _lm_liq_distance(pos: dict[str, Any]) -> Decimal | None:
    """Local copy of `snapshot._lm_liquidation_distance_pct` — the
    snapshot helper is private and importing it would re-execute the
    snapshot module's heavy init. (currentPrice - liquidationPrice) /
    currentPrice. Positive = headroom; ≤0 = past liq.
    """
    cur = _to_decimal(pos.get("currentPrice"))
    liq = _to_decimal(pos.get("liquidationPrice"))
    if cur is None or liq is None or cur <= 0:
        return None
    return (cur - liq) / cur


# ───────────────────────── CLI ────────────────────────────────────────

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Vault8004 event watcher (lightweight, no LLM)."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=120.0,
        help="Seconds between polls (default 120). Per .1 taxonomy: 60-300s.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll then exit (smoke test).",
    )
    parser.add_argument(
        "--baseline",
        default=str(DEFAULT_BASELINE_PATH),
        help="Path to baseline state file.",
    )
    parser.add_argument(
        "--events-dir",
        default=str(DEFAULT_EVENTS_DIR),
        help="Directory to write events JSONL.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(
        run_watcher(
            interval_seconds=args.interval,
            once=args.once,
            baseline_path=Path(args.baseline),
            events_dir=Path(args.events_dir),
        )
    )


if __name__ == "__main__":
    _main()
