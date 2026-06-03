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
                    continue
                mark = _to_decimal(live.markPrice)
                liq = _to_decimal(live.liqPrice)
                if mark is None or liq is None:
                    continue
                if ev := check_perp_liq_distance(pos, mark, liq):
                    events.append(ev)
        except Exception as e:  # noqa: BLE001
            log.warning("perp positions poll failed: %s", e)

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
