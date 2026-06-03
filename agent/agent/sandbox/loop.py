"""Sandbox loop driver — orchestrates the full Phase C cycle on a timer.

    snapshot → decide → validate → (approval) → execute → log

Single-process MVP per `bybit-sandbox.13`. No persistent state DB —
the snapshot / decision / execution files under `agent/sandbox/` are
the only state. A `cycle_log.jsonl` next to them records every cycle's
outcome (good or bad) so an operator can grep `result` after a 24h run.

CLI:
    # one shot for smoke testing
    python -m agent.sandbox.loop --once

    # 4-hour timer, dry-run only (default)
    python -m agent.sandbox.loop

    # live execution with auto-approve when confidence >= 0.7
    python -m agent.sandbox.loop --live --yes --min-confidence 0.7

Crash discipline:
- Per-cycle exceptions are caught and logged into the outcome; the
  outer loop keeps running. A bad snapshot fetch one cycle doesn't
  block the next one.
- SIGINT / SIGTERM exit cleanly between cycles (graceful shutdown
  on systemd timer or `kill <pid>` against a long-running process).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

    from agent.bybit_oracle.config import OracleSettings

import anthropic
from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import BybitClient
from agent.reason.schema import Decision
from agent.sandbox.decide import (
    DECISION_DIR,
    _load_latest_prior_decision,
    decide,
    write_decision,
)
from agent.sandbox.execute import (
    DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE,
    diff_to_actions,
    execute_actions,
    request_approval,
)
from agent.sandbox.snapshot import SNAPSHOT_DIR, collect_snapshot, write_snapshot
from agent.sandbox.store import (
    apply_migrations,
    open_pool,
    record_cycle,
    record_event,
)
from agent.sandbox.watcher import (
    DEFAULT_BASELINE_PATH as WATCHER_BASELINE_PATH,
)
from agent.sandbox.watcher import (
    DEFAULT_EVENTS_DIR as WATCHER_EVENTS_DIR,
)
from agent.sandbox.watcher import (
    EventRecord,
    update_baseline_from_snapshot,
)
from agent.sandbox.watcher import (
    poll_once as watcher_poll_once,
)
from agent.sandbox.watcher import (
    read_baseline as read_watcher_baseline,
)
from agent.sandbox.watcher import (
    write_events as write_watcher_events,
)
from agent.validate.rules import validate

# Default cycle cadence. Tightened to 30min in `.47` follow-up because
# leveraged LM could liquidate in minutes; relaxed back to 4h in
# `event-driven-rebalance.6` because the watcher (`event-driven-rebalance.2/.3`)
# now covers fast-moving signals via `wake_event` — including
# `lm_liquidation_distance ≤ 10%` (P0) which is the original reason the
# heartbeat was tightened. Pair this default with `--enable-watcher`;
# running without the watcher means a fast LM liquidation can land in
# the gap between two heartbeats. Cost economics: 4h × 6 cycles/day ×
# $0.14 ≈ $0.84/day baseline API (vs $6.72/day at 30min), reactive
# cycles add on top.
DEFAULT_INTERVAL_SECONDS = 4 * 60 * 60  # 4h
CYCLE_LOG = Path(__file__).parent / "cycle_log.jsonl"

# Endpoints whose failure aborts the loop at startup. If any of these
# come back !=ok from `permission_probe`, the loop refuses to start —
# we'd just be writing snapshots that miss critical data, or worse,
# planning execution against a broken auth scope. Informational probes
# (advance-Earn, LM, linear tickers) print warnings but don't block:
# the loop still runs without them, just with reduced surface.
_CRITICAL_PROBE_ENDPOINTS: frozenset[str] = frozenset(
    {
        "wallet_balance[UNIFIED]",
        "list_earn_products[FlexibleSaving]",
        "list_earn_products[OnChain]",
        "earn_positions[FlexibleSaving]",
    }
)

log = logging.getLogger(__name__)


def _build_auto_close_decision(
    prior: dict[str, Any] | None,
    wake_events: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Build a deterministic close-only decision from `pick_invalidated`
    wake events — bypasses the LLM entirely so a tripped stop-loss
    closes within seconds, not minutes (LLM round-trip + validator pass).

    Returns:
      - decision dict (mutated copy of `prior` with affected picks
        removed and freed weight rolled into cash_usdc), OR
      - None when no auto-close-eligible event is present (caller falls
        through to the normal LLM decide path).

    The mutation logic:
      • For each `pick_invalidated` event, collect the closed product_id
        (from `position_id="earn:<pid>"`) + coin.
      • Walk the prior decision's venues. For each venue with any of
        those picks: drop the closed picks, rescale remaining picks
        within the venue to sum to 1.0, scale venue weight down
        proportionally. If all picks are closed, drop the venue.
      • All freed venue weight goes to cash_usdc (added to its current
        weight, or appended as a new entry if absent).
      • `hedges` array zeroed — auto-hedge derives from picks, so
        removing the pick auto-closes the paired perp via `diff_to_actions`.

    The output is structurally a valid Decision dict (sums to 1.0,
    only known venue_ids, picks well-formed). Validator still runs on
    it as a sanity net; with deterministic mutation the only realistic
    failure is the cash_usdc venue exceeding its max_weight (1.0), so
    in practice it always passes.
    """
    if not prior or not wake_events:
        return None
    close_pids: set[str] = set()
    close_coins: set[str] = set()
    for e in wake_events:
        if (e.get("kind") or "") != "pick_invalidated":
            continue
        pid = e.get("position_id") or ""
        if pid.startswith("earn:"):
            close_pids.add(pid.removeprefix("earn:"))
        coin = e.get("coin") or ""
        if coin:
            close_coins.add(coin.upper())
    if not close_pids:
        return None

    new_venues: list[dict[str, Any]] = []
    cash_addition = 0.0
    for v in prior.get("venues", []) or []:
        picks = v.get("picks", []) or []
        venue_weight = float(v.get("weight", 0))
        if not picks:
            new_venues.append(dict(v))
            continue
        kept = [
            p for p in picks
            if str(p.get("product_id", "")) not in close_pids
        ]
        if len(kept) == len(picks):
            new_venues.append(dict(v))
            continue
        if not kept:
            cash_addition += venue_weight
            continue
        # Rescale kept picks within the venue + scale venue weight down
        # by the fraction that was kept.
        kept_sum = sum(float(p.get("weight", 0)) for p in kept)
        if kept_sum <= 0:
            cash_addition += venue_weight
            continue
        new_venue_weight = venue_weight * kept_sum
        cash_addition += venue_weight - new_venue_weight
        rescaled_picks = [
            {**p, "weight": float(p.get("weight", 0)) / kept_sum}
            for p in kept
        ]
        new_venues.append(
            {**v, "weight": new_venue_weight, "picks": rescaled_picks}
        )

    # Roll freed weight into cash_usdc (create if missing).
    cash_seen = False
    for v in new_venues:
        if v.get("venue_id") == "cash_usdc":
            v["weight"] = float(v.get("weight", 0)) + cash_addition
            cash_seen = True
            break
    if not cash_seen and cash_addition > 0:
        new_venues.append(
            {"venue_id": "cash_usdc", "weight": cash_addition, "picks": []}
        )

    coin_list = ", ".join(sorted(close_coins)) or "<none>"
    pid_list = ", ".join(sorted(close_pids))
    out: dict[str, Any] = {
        "thesis": (
            f"AUTO-CLOSE (no LLM): pick_invalidated fired for {coin_list}. "
            f"Closed product(s) {pid_list}; freed venue weight rolled into "
            f"cash_usdc. Next LLM cycle decides whether to re-enter once "
            f"the invalidation condition has recovered."
        ),
        "venues": new_venues,
        "hedges": [],
        "confidence": 1.0,
        "risk_flags": [],
        "notes": [f"auto_close:{pid}" for pid in sorted(close_pids)],
        "expected_blended_apr_pct": 0.0,
    }
    return out


async def run_one_cycle(
    bybit_client: BybitClient,
    anthropic_client: anthropic.AsyncAnthropic,
    *,
    live: bool,
    yes: bool,
    min_confidence: float,
    mantle_rpc_url: str | None = None,
    mantle_vault_address: str | None = None,
    wake_events: list[dict[str, Any]] | None = None,
    watcher_baseline_path: Path | None = None,
) -> dict[str, Any]:
    """Execute one full snapshot→decide→validate→approval→execute pass.

    Returns a JSON-serializable outcome dict for cycle-log purposes:

        {"started_at", "finished_at",
         "snapshot_filename"?, "decision_filename"?,
         "confidence"?, "expected_apr_pct"?,
         "validator_ok"?, "validator_errors"?,
         "actions_planned"?, "actions_executed"?, "actions"?,
         "approved"?,
         "result": "ok"|"executed"|"skipped:invalid"|"no_actions"|"error",
         "error"?}

    NEVER raises — all exceptions surface in `result="error"` with a
    text `error` field so the timer loop can keep running.
    """
    started = datetime.now(UTC).isoformat()
    outcome: dict[str, Any] = {"started_at": started, "stages": []}
    outcome["wake_reason"] = (
        "event:"
        + ",".join(sorted({e.get("kind", "?") for e in wake_events}))
        if wake_events
        else "heartbeat"
    )

    try:
        # 1. Snapshot
        snap = await collect_snapshot(
            bybit_client,
            mantle_rpc_url=mantle_rpc_url,
            mantle_vault_address=mantle_vault_address,
        )
        snap_path = write_snapshot(snap)
        outcome["snapshot_filename"] = snap_path.name
        outcome["stages"].append("snapshot")

        raw_snapshot = json.loads(snap_path.read_text())

        # 1a. Watcher baseline refresh — happens regardless of later
        # validator outcome, because Bybit holdings are real even when
        # the LLM is rejected (`event-driven-rebalance.3`). On any IO
        # failure here, log and continue — the baseline staleness
        # degrades watcher precision but doesn't break the cycle.
        try:
            update_baseline_from_snapshot(
                raw_snapshot,
                path=watcher_baseline_path or WATCHER_BASELINE_PATH,
                snapshot_filename=snap_path.name,
            )
        except Exception as e:  # noqa: BLE001 — best effort
            log.warning("watcher baseline update failed: %s", e)

        # 2. Decide — auto-close fast-path when ANY pick_invalidated
        # event is in the wake set. Deterministic close from the prior
        # decision; skips LLM entirely so the stop-loss closes in
        # seconds rather than waiting for a Claude round-trip + token
        # cost. Per-coin events fan out to the matching pick(s) and
        # roll freed weight to cash. Falls through to the LLM path
        # when no eligible event is present.
        prior = _load_latest_prior_decision()
        auto_close = _build_auto_close_decision(prior, wake_events)
        if auto_close is not None:
            log.info(
                "auto-close path: pick_invalidated event(s) — "
                "skipping LLM, deterministic close"
            )
            decision = Decision.model_validate(auto_close)
            outcome["auto_close"] = True
        else:
            decision = await decide(
                raw_snapshot,
                client=anthropic_client,
                prior_decision=prior,
                wake_events=wake_events,
            )
        decision_path = write_decision(
            decision, snap_path, wake_events=wake_events
        )
        outcome["decision_filename"] = decision_path.name
        outcome["confidence"] = float(decision.confidence)
        outcome["expected_apr_pct"] = float(decision.expected_blended_apr_pct)
        outcome["stages"].append("decide")

        # 3. Validate
        ok, errors = validate(decision, snap)
        outcome["validator_ok"] = ok
        outcome["validator_errors"] = errors
        outcome["stages"].append("validate")
        # Persist validator outcome alongside the decision so the next
        # cycle's `_summarize_prior_decision` can surface rejection
        # reasons to Claude (`.47` feedback-loop fix, 2026-05-29).
        # Without this Claude only sees the prior allocation and repeats
        # the same min_notional / funding violations cycle after cycle.
        try:
            raw_decision = json.loads(decision_path.read_text())
            meta = raw_decision.setdefault("_meta", {})
            meta["_validator"] = {"ok": ok, "errors": list(errors)}
            decision_path.write_text(json.dumps(raw_decision, indent=2))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("failed to attach validator outcome to decision: %s", e)
        if not ok:
            outcome["result"] = "skipped:invalid"
            return outcome

        # 4. Diff → actions
        snapshot_ts = snap_path.stem
        actions = diff_to_actions(snap, decision, snapshot_ts)
        outcome["actions_planned"] = len(actions)
        outcome["stages"].append("diff")
        if not actions:
            outcome["result"] = "no_actions"
            return outcome

        # 5. Approval (only on --live)
        effective_dry_run = not live
        if live:
            approved = request_approval(
                decision, actions, yes=yes, min_confidence=min_confidence
            )
            outcome["approved"] = approved
            if not approved:
                effective_dry_run = True
        outcome["stages"].append("approval")

        # 6. Execute
        results = await execute_actions(
            bybit_client,
            actions,
            snapshot_ts=snapshot_ts,
            dry_run=effective_dry_run,
        )
        outcome["actions_executed"] = sum(1 for r in results if r.status == "ok")
        outcome["actions"] = [
            {
                "kind": r.action.kind.value,
                "category": r.action.category,
                "product_id": r.action.product_id,
                "coin": r.action.coin,
                "amount": str(r.action.amount),
                "status": r.status,
                "error": r.error,
            }
            for r in results
        ]
        outcome["stages"].append("execute")
        outcome["result"] = "executed" if not effective_dry_run else "ok"
    except Exception as e:  # noqa: BLE001 — outermost guard
        outcome["error"] = f"{type(e).__name__}: {e}"
        outcome["result"] = "error"
        log.exception("cycle failed mid-flight")

    outcome["finished_at"] = datetime.now(UTC).isoformat()
    return outcome


async def run_loop(
    *,
    interval_seconds: float,
    live: bool,
    yes: bool,
    min_confidence: float,
    once: bool = False,
    cycle_log_path: Path = CYCLE_LOG,
    stop_event: asyncio.Event | None = None,
    mantle_rpc_url: str | None = None,
    mantle_vault_address: str | None = None,
    oracle_cfg: OracleSettings | None = None,
    enable_watcher: bool = False,
    watcher_interval_seconds: float = 120.0,
    watcher_baseline_path: Path = WATCHER_BASELINE_PATH,
    watcher_events_dir: Path = WATCHER_EVENTS_DIR,
    enable_store: bool = False,
    database_url: str | None = None,
) -> None:
    """Run cycles indefinitely (or once) at `interval_seconds` apart.

    The wait between cycles is cancellable: when the `stop_event` fires
    (set by SIGINT / SIGTERM in `_install_signal_handlers`), the current
    sleep wakes immediately and the loop exits at the top of the next
    iteration. In `--once` mode the interval is irrelevant — single
    cycle then return.

    `enable_watcher=True` spawns a concurrent watcher task
    (`event-driven-rebalance.3`) that polls cheap signals every
    `watcher_interval_seconds` and sets `wake_event` on any P0 event.
    The main loop's inter-cycle sleep then races against
    `wake_event.wait()`; on wake the queued events are passed into
    `run_one_cycle(wake_events=...)`. Default OFF for backwards
    compatibility with existing smoke runs.

    `enable_store=True` opens a Postgres pool via `DATABASE_URL` (or
    the explicit `database_url` kwarg) and dual-writes each cycle into
    the cycle store (`data-store.3`). Initialisation failures (DB
    unreachable, schema migration error) log a warning and disable
    the store for the run — files remain source of truth, agent
    continues. Default OFF so existing smoke configs without a DB
    keep working.
    """
    stop_event = stop_event or asyncio.Event()
    _install_signal_handlers(stop_event)
    cycle_log_path.parent.mkdir(parents=True, exist_ok=True)

    wake_event = asyncio.Event()
    pending_events: list[EventRecord] = []
    # P0 event DB ids — populated by the watcher when --enable-store
    # is on, drained alongside `pending_events` for the cross-link
    # in `record_cycle(..., triggered_event_ids=...)`. Parallel list
    # rather than tuple-pair so the wake-event prompt path stays
    # unchanged.
    pending_event_db_ids: list[int] = []

    async with (
        anthropic.AsyncAnthropic() as anthropic_client,
        BybitClient.from_settings(oracle_cfg) as bybit_client,
    ):
        # Startup permission probe (.26) — fail-fast if a critical
        # endpoint is denied. Don't write to cycle_log here; the probe
        # is a precondition, not a cycle.
        probe = await bybit_client.permission_probe()
        critical_failures = {
            ep: status
            for ep, status in probe.items()
            if ep in _CRITICAL_PROBE_ENDPOINTS and status != "ok"
        }
        for ep, status in probe.items():
            level = (
                logging.ERROR
                if ep in critical_failures
                else (logging.INFO if status == "ok" else logging.WARNING)
            )
            log.log(level, "probe %-36s %s", ep, status)
        if critical_failures:
            raise SystemExit(
                "permission probe failed on critical endpoints: "
                f"{sorted(critical_failures)}"
            )

        # Cycle store init (`data-store.3`). Failures degrade gracefully:
        # files are still the source of truth, the loop continues
        # without DB writes. Pool is owned by `store_stack` so it closes
        # in the outer finally block alongside the watcher teardown.
        store_pool = None
        store_stack = contextlib.AsyncExitStack()
        if enable_store:
            try:
                store_pool = await store_stack.enter_async_context(
                    open_pool(database_url)
                )
                applied = await apply_migrations(store_pool)
                log.info(
                    "DB store enabled (migrations applied this start: %s)",
                    applied or "none",
                )
            except Exception as e:  # noqa: BLE001 — degrade to file-only
                log.warning(
                    "DB store init failed — continuing file-only: %s", e
                )
                store_pool = None
                await store_stack.aclose()

        watcher_task: asyncio.Task[None] | None = None
        if enable_watcher:
            watcher_task = asyncio.create_task(
                _run_watcher_task(
                    bybit_client=bybit_client,
                    wake_event=wake_event,
                    pending_events=pending_events,
                    pending_event_db_ids=pending_event_db_ids,
                    stop_event=stop_event,
                    interval_seconds=watcher_interval_seconds,
                    baseline_path=watcher_baseline_path,
                    events_dir=watcher_events_dir,
                    store_pool=store_pool,
                ),
                name="watcher",
            )
            log.info(
                "watcher enabled — polling every %.0fs, baseline=%s",
                watcher_interval_seconds, watcher_baseline_path,
            )

        try:
            while not stop_event.is_set():
                # Drain any events the watcher queued while we slept,
                # together with their DB ids for the cross-link.
                cycle_wake_events: list[dict[str, Any]] = []
                cycle_event_db_ids: list[int] = []
                if pending_events:
                    cycle_wake_events = [
                        e.model_dump(mode="json") for e in pending_events
                    ]
                    pending_events.clear()
                if pending_event_db_ids:
                    cycle_event_db_ids = list(pending_event_db_ids)
                    pending_event_db_ids.clear()
                wake_event.clear()

                log.info(
                    "starting cycle (live=%s, yes=%s, min_confidence=%.2f, "
                    "wake=%s)",
                    live, yes, min_confidence,
                    "heartbeat" if not cycle_wake_events else (
                        "event:" + ",".join(sorted({
                            e.get("kind", "?") for e in cycle_wake_events
                        }))
                    ),
                )
                outcome = await run_one_cycle(
                    bybit_client,
                    anthropic_client,
                    live=live,
                    yes=yes,
                    min_confidence=min_confidence,
                    mantle_rpc_url=mantle_rpc_url,
                    mantle_vault_address=mantle_vault_address,
                    wake_events=cycle_wake_events or None,
                    watcher_baseline_path=watcher_baseline_path,
                )
                with cycle_log_path.open("a") as f:
                    f.write(json.dumps(outcome) + "\n")
                log.info("cycle result: %s", outcome.get("result"))
                if store_pool is not None:
                    try:
                        await _record_cycle_from_outcome(
                            store_pool,
                            outcome,
                            triggered_event_ids=cycle_event_db_ids or None,
                        )
                    except Exception as e:  # noqa: BLE001 — DB is best-effort
                        log.warning("DB record_cycle failed: %s", e)
                if once or stop_event.is_set():
                    break
                await _sleep_until_next_cycle(
                    interval_seconds=interval_seconds,
                    stop_event=stop_event,
                    wake_event=wake_event,
                )
        finally:
            if watcher_task is not None:
                stop_event.set()
                watcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher_task
            await store_stack.aclose()


async def _sleep_until_next_cycle(
    *,
    interval_seconds: float,
    stop_event: asyncio.Event,
    wake_event: asyncio.Event,
) -> None:
    """Race three signals: heartbeat timeout, watcher wake, shutdown.

    Returns as soon as ANY of them fires. Cancels the two losers so we
    don't leak tasks. The caller is responsible for clearing
    `wake_event` before the next sleep (so a stale wake doesn't
    short-circuit again immediately).
    """
    stop_task = asyncio.create_task(stop_event.wait())
    wake_task = asyncio.create_task(wake_event.wait())
    done, pending = await asyncio.wait(
        {stop_task, wake_task},
        timeout=interval_seconds,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t


async def _record_cycle_from_outcome(
    pool: asyncpg.Pool,
    outcome: dict[str, Any],
    *,
    triggered_event_ids: list[int] | None = None,
) -> None:
    """Re-load the snapshot/decision JSON files referenced by `outcome`
    and persist the cycle into the store. For "error" cycles that
    failed before writing a snapshot, calls `record_cycle` with
    `raw_snapshot=None` — the cycles row still lands as a record of
    the failure.

    `triggered_event_ids`, when provided, is the list of watcher event
    DB ids that woke this cycle. `record_cycle` stamps them with
    `triggered_cycle_ts` inside the same transaction (`data-store.4`).

    Any exception here is the caller's problem to log — this helper
    stays narrow so it's straightforward to unit-test.
    """
    raw_snapshot: dict[str, Any] | None = None
    raw_decision: dict[str, Any] | None = None
    snap_name = outcome.get("snapshot_filename")
    if snap_name:
        snap_path = SNAPSHOT_DIR / snap_name
        if snap_path.exists():
            raw_snapshot = json.loads(snap_path.read_text())
    decision_name = outcome.get("decision_filename")
    if decision_name:
        decision_path = DECISION_DIR / decision_name
        if decision_path.exists():
            raw_decision = json.loads(decision_path.read_text())
    await record_cycle(
        pool,
        outcome=outcome,
        raw_snapshot=raw_snapshot,
        raw_decision=raw_decision,
        triggered_event_ids=triggered_event_ids,
    )


async def _run_watcher_task(
    *,
    bybit_client: BybitClient,
    wake_event: asyncio.Event,
    pending_events: list[EventRecord],
    pending_event_db_ids: list[int],
    stop_event: asyncio.Event,
    interval_seconds: float,
    baseline_path: Path,
    events_dir: Path,
    store_pool: asyncpg.Pool | None = None,
) -> None:
    """Long-running task that polls the watcher every
    `interval_seconds`. Per-poll exceptions are swallowed with a log —
    a single Bybit hiccup must not stop subsequent polls. P0 events set
    `wake_event` and enqueue records into `pending_events`; P1/P2 are
    still written to the JSONL sink (and DB if enabled) but do not
    wake the main loop.

    When `store_pool` is provided, ALL events (P0/P1/P2) are also
    written to the DB via `record_event`. For P0 events the returned
    DB id is appended to `pending_event_db_ids` so the wake-driven
    cycle can cross-link them via `record_cycle(...,
    triggered_event_ids=...)`. DB write failures log a warning and
    leave the JSONL path untouched.
    """
    while not stop_event.is_set():
        try:
            baseline = read_watcher_baseline(baseline_path)
            if baseline is not None:
                events = await watcher_poll_once(bybit_client, baseline)
                if events:
                    write_watcher_events(events, events_dir)

                    # Dual-write to DB (`data-store.4`). Track the
                    # generated id alongside each event so P0 entries
                    # carry it into `pending_event_db_ids`.
                    db_ids: list[int | None] = [None] * len(events)
                    if store_pool is not None:
                        for i, ev in enumerate(events):
                            try:
                                db_ids[i] = await record_event(
                                    store_pool, ev.model_dump(mode="json")
                                )
                            except Exception as e:  # noqa: BLE001
                                log.warning(
                                    "DB record_event failed (kind=%s): %s",
                                    ev.kind, e,
                                )

                    p0_pairs = [
                        (ev, did)
                        for ev, did in zip(events, db_ids, strict=True)
                        if ev.severity == "P0"
                    ]
                    if p0_pairs:
                        pending_events.extend(ev for ev, _ in p0_pairs)
                        pending_event_db_ids.extend(
                            did for _, did in p0_pairs if did is not None
                        )
                        wake_event.set()
                        log.info(
                            "watcher: %d P0 event(s) → waking main loop",
                            len(p0_pairs),
                        )
        except Exception as e:  # noqa: BLE001 — keep the watcher alive
            log.warning("watcher poll failed: %s", e)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Wire SIGINT + SIGTERM to set `stop_event` so the loop exits
    cleanly between cycles. Silently no-ops on platforms where signal
    handlers aren't installable (Windows, nested event loops in tests).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            return


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Vault8004 sandbox loop on a timer."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=(
            f"Seconds between cycles (default {DEFAULT_INTERVAL_SECONDS} "
            "= 4h heartbeat). Pair with --enable-watcher for reactive "
            "wake-ups between heartbeats — the watcher is what catches "
            "fast moves at this cadence."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one cycle and exit (smoke test mode).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually place orders on Bybit. Default is dry-run.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Bypass interactive y/N approval when --live and "
            "decision.confidence >= --min-confidence."
        ),
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE,
        help=(
            f"Auto-approve floor for --yes (default "
            f"{DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE})."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="dotenv to load (e.g. .env at repo root).",
    )
    parser.add_argument(
        "--enable-watcher",
        action="store_true",
        help=(
            "Spawn the event watcher (`event-driven-rebalance.3`). When "
            "set, P0 events trigger an immediate cycle outside the "
            "--interval schedule. Default OFF."
        ),
    )
    parser.add_argument(
        "--watcher-interval",
        type=float,
        default=120.0,
        help="Seconds between watcher polls when --enable-watcher (default 120).",
    )
    parser.add_argument(
        "--enable-store",
        action="store_true",
        help=(
            "Dual-write every cycle into the Postgres store "
            "(`data-store.3`). Requires DATABASE_URL in env (or "
            "--database-url). DB init failures degrade to file-only "
            "with a warning. Default OFF."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "Override the Postgres DSN. Falls back to DATABASE_URL "
            "env var when omitted."
        ),
    )
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(args.env_file, override=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # Read Mantle on-chain config after env load so `.37a` Aave fetch
    # picks up the freshly-set values. Empty strings = on-chain leg off
    # (snapshot collector logs a warning and `on_chain_state` stays None).
    from agent.bybit_oracle.config import OracleSettings
    oracle_cfg = OracleSettings()
    mantle_rpc_url = oracle_cfg.MANTLE_RPC_URL or None
    mantle_vault_address = oracle_cfg.MANTLE_VAULT_ADDRESS or None

    asyncio.run(
        run_loop(
            interval_seconds=args.interval,
            live=args.live,
            yes=args.yes,
            min_confidence=args.min_confidence,
            once=args.once,
            mantle_rpc_url=mantle_rpc_url,
            mantle_vault_address=mantle_vault_address,
            oracle_cfg=oracle_cfg,
            enable_watcher=args.enable_watcher,
            watcher_interval_seconds=args.watcher_interval,
            enable_store=args.enable_store,
            database_url=args.database_url,
        )
    )


if __name__ == "__main__":
    _main()
