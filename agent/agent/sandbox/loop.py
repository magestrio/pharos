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
import os
import signal
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

    from agent.bybit_oracle.config import OracleSettings

import anthropic
from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import BybitClient
from agent.reason.schema import Decision
from agent.reason.venues import (
    CARRY_CATEGORY,
    CARRY_VENUE_ID,
    DEFAULT_CYCLE_INTERVAL_SECONDS,
    VENUE_REGISTRY,
)
from agent.sandbox.decide import (
    DECISION_DIR,
    _collect_recently_invalidated,
    _load_recent_prior_decisions,
    decide,
    write_decision,
)
from agent.sandbox.ipfs_pin import pin_decision_rationale
from agent.sandbox.onchain_writer import (
    MIN_GAS_MNT,
    OnchainWriter,
    derive_execution_hash,
    derive_ids,
)
from agent.sandbox.onchain_anchor_queue import (
    MAX_ANCHOR_ATTEMPTS,
    AnchorQueue,
    PendingAnchor,
    read_anchor_queue,
    write_anchor_queue,
)
from agent.sandbox.reflect import reflect_on_cycle
from agent.sandbox.carry_state import (
    CarryState,
    DEFAULT_CARRY_STATE_PATH,
    read_carry_state,
    write_carry_state,
)
from agent.sandbox.lm_redeem_cooldown import (
    read_lm_redeem_cooldown,
    write_lm_redeem_cooldown,
)
from agent.sandbox.execute import (
    DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE,
    EXECUTIONS_DIR,
    _carry_liq_close_actions,
    _confirmable_order_links,
    _lm_residual_redeem_actions,
    _orphan_perp_close_actions,
    _orphan_spot_sell_actions,
    _reconcile_hedge_to_earn_actions,
    _reindex_order_link_ids,
    _stable_consolidate_actions,
    _coin_wallet_native,
    apply_carry_results_to_state,
    build_redeem_exit_intents,
    diff_to_actions,
    exit_actions_from_intent,
    execute_actions,
    reconcile_executions,
    request_approval,
    verify_executions_against_bybit,
    verify_order_links,
)
from agent.sandbox.redeem_intent import (
    read_redeem_intents,
    write_redeem_intents,
)
from agent.sandbox.pending_intent import (
    PendingIntent,
    clear_pending_intent,
    read_pending_intent,
    write_pending_intent,
)
from agent.sandbox.reputation import (
    compute_realized_apr_bps,
    record_push,
    should_push,
)
from agent.sandbox.safety import (
    check_daily_drawdown,
    clear_halt,
    halt,
    halt_trigger,
    is_halted,
    read_equity_history,
    record_equity,
)
from agent.sandbox.position_ledger import update_ledger_and_ages
from agent.sandbox.snapshot import (
    SNAPSHOT_DIR,
    STABLES,
    Snapshot,
    collect_snapshot,
    write_snapshot,
)
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
    PRE_SETTLE_TARGET_S,
    EventRecord,
    check_earn_redeem_settled,
    prune_closed_positions,
    seconds_until_close_target,
    update_baseline_from_snapshot,
    write_baseline as write_watcher_baseline,
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
from agent.validate.rules import (
    MIN_CONFIDENCE,
    NET_HEDGE_YIELD_FLOOR,
    _held_earn_detail,
    _held_usd_by_product,
    _snapshot_index,
    net_hedge_yield,
    validate,
)

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
# Re-export the shared cadence (ah.17) under the loop's local name so the
# argparse default + the system prompt's "heartbeat" narrative stay in sync.
DEFAULT_INTERVAL_SECONDS = DEFAULT_CYCLE_INTERVAL_SECONDS  # 4h
# Lives under state/ (a persisted Docker volume) — NOT the package root, which
# is ephemeral container FS wiped on every `compose up --build`. The crash-
# recovery gate cross-references this ledger against the persisted executions/
# volume; if it resets on rebuild while executions survive, every prior cycle
# looks unfinished and the gate HALTs the loop forever.
CYCLE_LOG = Path(__file__).parent / "state" / "cycle_log.jsonl"


def detect_unfinished_cycles(
    cycle_log_path: Path = CYCLE_LOG,
    executions_dir: Path = EXECUTIONS_DIR,
) -> list[dict[str, Any]]:
    """Scan for cycles whose `executions/<ts>.jsonl` exists but whose
    cycle outcome was never written to `cycle_log.jsonl` (`.42`).

    Crash signature: systemd OOM / SIGKILL between `execute_actions`
    writing per-action lines and `run_one_cycle` returning. The per-
    action log persists (it's flushed line-by-line); the cycle entry
    doesn't, because it's written by the outer loop AFTER `run_one_cycle`
    completes.

    Returns one summary dict per unfinished cycle (sorted oldest →
    newest), each carrying the `reconcile_executions` output. Empty
    list when everything is clean. Read-only — does NOT mutate state
    or replay actions.
    """
    if not executions_dir.is_dir():
        return []

    completed_ts: set[str] = set()
    if cycle_log_path.is_file():
        for raw in cycle_log_path.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts = entry.get("snapshot_filename")
            if isinstance(ts, str) and ts.endswith(".json"):
                completed_ts.add(ts[:-5])

    unfinished: list[dict[str, Any]] = []
    for path in sorted(executions_dir.glob("*.jsonl")):
        ts = path.stem
        if ts in completed_ts:
            continue
        summary = reconcile_executions(ts, executions_dir=executions_dir)
        if summary.get("total", 0) > 0:
            unfinished.append(summary)
    return unfinished


# Verifier classifications that mean "a real or unconfirmable order may be open
# that the agent never reconciled" — the startup gate HALTs on any of these.
# `no-trace` (order never landed) is the only safe-to-ignore outcome.
_CRASH_GATE_BLOCKING = ("confirmed-landed", "desync", "query-error")


async def _startup_crash_recovery_gate(
    cycle_log_path: Path,
    client: BybitClient,
    *,
    live: bool,
) -> bool:
    """Block startup when a prior cycle may have left an unreconciled position
    on Bybit (ah.7 — dispatch-4 + state-2). Two complementary sources:

      1. Unfinished cycles — executions log flushed but no cycle_log entry
         (`detect_unfinished_cycles`); confirmable rows cross-checked with
         `verify_executions_against_bybit`. Catches SWAP/perp single actions.
      2. A surviving pending-intent marker — a crash mid-`execute_actions`,
         BEFORE the row was flushed, so the scan above can't see it. Its
         recorded leg links (incl. carry spot+perp, which the executions scan
         treats as unconfirmable) are cross-checked with `verify_order_links`.

    Any `confirmed-landed` / `desync` / `query-error` → `halt()` so every cycle
    no-ops until the operator reconciles. A clean (all `no-trace`) marker is
    cleared. Verification needs live Bybit order-history (IP-bound key), so a
    non-live start only warns and leaves any marker for the next live start.

    Returns True when a HALT was tripped.
    """
    unfinished = detect_unfinished_cycles(cycle_log_path)
    for u in unfinished:
        log.warning(
            "unfinished prior cycle detected (no cycle_log entry): "
            "ts=%s total=%d counts=%s last_finished=%s — verifying against "
            "Bybit before next cycle's diff opens new positions",
            u["snapshot_ts"], u["total"], u["counts"],
            u.get("last_finished_at"),
        )
    pending = read_pending_intent()

    if not live:
        if unfinished or pending is not None:
            log.warning(
                "crash-recovery gate skipped (not live): %d unfinished "
                "cycle(s), pending_intent=%s — will verify on the next live "
                "start",
                len(unfinished),
                pending.snapshot_ts if pending else None,
            )
        return False

    halt_reasons: list[str] = []
    for u in unfinished:
        try:
            v = await verify_executions_against_bybit(u["snapshot_ts"], client)
        except Exception as e:  # noqa: BLE001 — a failed verify must block, not crash
            halt_reasons.append(f"verify {u['snapshot_ts']} raised: {e}")
            continue
        bad = {k: c for k, c in v["counts"].items() if k in _CRASH_GATE_BLOCKING}
        if bad:
            halt_reasons.append(f"unfinished cycle {u['snapshot_ts']}: {bad}")
            log.error(
                "crash-recovery: unfinished cycle %s has unreconciled "
                "orders on Bybit %s", u["snapshot_ts"], bad
            )

    if pending is not None:
        try:
            pv = await verify_order_links(
                client, [link.model_dump() for link in pending.links]
            )
        except Exception as e:  # noqa: BLE001
            halt_reasons.append(f"pending-intent verify raised: {e}")
            pv = None
        if pv is not None:
            bad = {
                k: c for k, c in pv["counts"].items()
                if k in _CRASH_GATE_BLOCKING
            }
            if bad:
                halt_reasons.append(f"pending intent {pending.snapshot_ts}: {bad}")
                log.error(
                    "crash-recovery: pending intent %s has unreconciled orders "
                    "on Bybit %s — possible naked/duplicate position",
                    pending.snapshot_ts, bad,
                )
            else:
                clear_pending_intent()
                log.info(
                    "crash-recovery: pending intent %s clean (no orders "
                    "landed) — cleared", pending.snapshot_ts
                )

    if halt_reasons:
        halt(
            "startup crash-recovery gate: "
            + "; ".join(halt_reasons)
            + " — a prior cycle may hold an unreconciled position; review "
            "executions/ + Bybit order-history, reconcile carry_state, then "
            "clear the HALT marker before resume"
        )
        return True
    return False


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


def _execution_block(outcome: dict[str, Any]) -> dict[str, Any]:
    """The cycle's actual-outcome block — shared by the IPFS pin
    (intent + execution audit trail) and the human reflection."""
    return {
        "result": outcome.get("result"),
        "actions_executed": outcome.get("actions_executed"),
        "actions_failed": outcome.get("actions_failed"),
        "actions": outcome.get("actions") or [],
    }


async def _attach_reflection(
    outcome: dict[str, Any],
    client: anthropic.AsyncAnthropic,
) -> None:
    """Best-effort: generate a first-person 'diary' note for the finished
    cycle and persist it as the decision's top-level `reflection`.

    Called twice per cycle: once inside `run_one_cycle` just before the
    on-chain anchor (so the executed-path IPFS pin embeds the note), and
    once as a backstop in `run_loop` for cycles that returned before the
    pin (held / skipped:invalid / errored). Idempotent across both calls —
    it skips when the note is already present on the outcome OR the file —
    so exactly one Haiku call happens per cycle. The note is written into
    the decision file, so `_record_cycle_from_outcome` (which re-reads the
    file) carries it into Postgres → API → web. Failure never affects the
    cycle.
    """
    # Idempotency, source of truth = the outcome. Checking the outcome (not
    # just the file) closes a double-call gap: if the file write below fails,
    # the note still lives on the outcome, so the backstop call won't re-run
    # the (paid) Haiku generation.
    if outcome.get("reflection"):
        return
    decision_name = outcome.get("decision_filename")
    if not decision_name:
        return
    decision_path = DECISION_DIR / decision_name
    if not decision_path.exists():
        return
    try:
        decision_dict = json.loads(decision_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if decision_dict.get("reflection"):
        return  # a prior run already persisted one (resume / re-run)

    raw_snapshot: dict[str, Any] | None = None
    snap_name = outcome.get("snapshot_filename")
    if snap_name:
        snap_path = SNAPSHOT_DIR / snap_name
        if snap_path.exists():
            try:
                raw_snapshot = json.loads(snap_path.read_text())
            except (OSError, json.JSONDecodeError):
                raw_snapshot = None

    text = await reflect_on_cycle(
        decision_dict,
        _execution_block(outcome),
        raw_snapshot,
        client=client,
    )
    if not text:
        return

    # Record on the outcome FIRST so a file-write failure can't make the
    # backstop re-generate. Then persist to the file (best-effort) so the
    # pin + store pick it up.
    outcome["reflection"] = text
    outcome.setdefault("stages", []).append("reflect")
    decision_dict["reflection"] = text
    try:
        decision_path.write_text(json.dumps(decision_dict, indent=2))
    except OSError as e:
        log.warning("could not persist reflection into %s: %s", decision_path, e)


def _drain_anchor_queue(
    writer: OnchainWriter, queue: AnchorQueue
) -> tuple[AnchorQueue, int]:
    """Retry each queued anchor (state-6). Drops entries that land now (or
    landed late, per `decision_exists`) and entries past
    `MAX_ANCHOR_ATTEMPTS` (a standing problem the operator must anchor
    manually). Returns `(new_queue, drained_count)`. Sync / RPC-bound — call
    via `asyncio.to_thread`."""
    remaining: list[PendingAnchor] = []
    drained = 0
    for p in queue.entries:
        try:
            decision_id = bytes.fromhex(p.decision_id)
            action_hash = bytes.fromhex(p.action_hash)
        except ValueError:
            log.warning(
                "dropping malformed anchor-queue entry %s", p.snapshot_filename
            )
            continue
        tx = writer.anchor_prepared(decision_id, p.ipfs_cid, action_hash)
        if tx:
            drained += 1
            log.info(
                "re-anchored queued decision %s: tx=%s", p.snapshot_filename, tx
            )
            continue
        if writer.decision_exists(decision_id):
            drained += 1  # landed late on a prior attempt → drop
            continue
        attempts = p.attempts + 1
        if attempts >= MAX_ANCHOR_ATTEMPTS:
            log.error(
                "dropping decision %s from anchor queue after %d failed "
                "attempts — operator must anchor manually",
                p.snapshot_filename, attempts,
            )
            continue
        remaining.append(p.model_copy(update={"attempts": attempts}))
    return AnchorQueue(entries=remaining), drained


async def _anchor_onchain(
    decision: Decision,
    outcome: dict[str, Any],
) -> None:
    """Best-effort: write `recordDecision` to DecisionLog and (if the
    cooldown is up) `updateReputation` on the canonical 8004 oracle.

    Reads config from env on every cycle so a hot-reloaded `.env.local`
    is picked up after restart. Anything missing → silently no-op.
    Results land in `outcome["onchain"]` for downstream telemetry.
    """
    writer = await asyncio.to_thread(OnchainWriter.from_env)
    if writer is None:
        return

    snap_name = outcome.get("snapshot_filename") or ""
    # Hydrate the decision dict from disk so we get the canonical
    # `_meta.written_at` + any IPFS cid sidecars that `write_decision`
    # baked in. Falls back to the in-memory Decision dump on read error.
    decision_dict: dict[str, Any]
    decision_path_name = outcome.get("decision_filename") or ""
    decision_path = DECISION_DIR / decision_path_name if decision_path_name else None
    if decision_path and decision_path.exists():
        try:
            decision_dict = json.loads(decision_path.read_text())
        except (OSError, json.JSONDecodeError):
            decision_dict = decision.model_dump()
    else:
        decision_dict = decision.model_dump()

    anchor: dict[str, Any] = {}

    # Drain any decisions a prior cycle couldn't anchor (state-6) — done
    # first so a recovered RPC clears the backlog before the current anchor.
    queue = read_anchor_queue()
    if queue.entries:
        queue, drained = await asyncio.to_thread(
            _drain_anchor_queue, writer, queue
        )
        if drained:
            anchor["anchor_drained"] = drained

    # Low-gas alert: a near-empty EOA is the dominant anchor-failure cause.
    gas_mnt = await asyncio.to_thread(writer.gas_balance_mnt)
    if gas_mnt is not None and gas_mnt < MIN_GAS_MNT:
        log.warning(
            "agent EOA low on gas: %.4f MNT < %s threshold — on-chain "
            "anchoring will start failing until topped up",
            float(gas_mnt), MIN_GAS_MNT,
        )
        anchor["low_gas_mnt"] = str(gas_mnt)

    # Anchor ACTUAL execution, not just intent. `outcome` already carries
    # the per-action results (status) by the time we're called (post-
    # execute). The IPFS payload embeds an `_execution` block (the public
    # audit trail), and `actionHash` commits to the executed ledger for
    # live cycles — so a partial failure can't masquerade on-chain as a
    # fully-executed allocation. Dry-run / hold (no_actions) fall back to
    # the intent hash (intent == execution there).
    result = outcome.get("result")
    execution_block = _execution_block(outcome)
    executed_actions = (
        outcome.get("actions")
        if result in ("executed", "executed_partial")
        else None
    )

    # Pin rationale to IPFS first so the on-chain event carries a public
    # CID. Failure → empty CID (still anchored, just no public link).
    # The pinned payload = decision intent + `_execution` outcome.
    pin_payload = dict(decision_dict)
    pin_payload["_execution"] = execution_block
    ipfs_cid = await asyncio.to_thread(
        pin_decision_rationale, pin_payload, snap_name
    )
    if ipfs_cid:
        anchor["ipfs_cid"] = ipfs_cid
        # Persist the CID back into the decision file so future cycles'
        # `_summarize_prior_decision` can reference it + the data-store
        # picks it up if backfill re-reads decisions.
        if decision_path and decision_path.exists():
            try:
                meta = decision_dict.setdefault("_meta", {})
                meta["ipfs_cid"] = ipfs_cid
                decision_path.write_text(json.dumps(decision_dict, indent=2))
            except OSError as e:
                log.warning("could not persist ipfs_cid into %s: %s", decision_path, e)

    # Derive the anchor inputs here (not inside record_decision) so a failed
    # send can be queued with the exact ids/hash for a later replay (state-6).
    decision_id, intent_hash = derive_ids(decision_dict, snap_name)
    action_hash = (
        derive_execution_hash(executed_actions)
        if executed_actions is not None
        else intent_hash
    )
    tx_hash = await asyncio.to_thread(
        writer.anchor_prepared, decision_id, ipfs_cid or "", action_hash
    )
    if tx_hash:
        anchor["decision_tx"] = tx_hash
        log.info("decision anchored on-chain: tx=%s cid=%s", tx_hash, ipfs_cid or "(none)")
    else:
        # No tx hash AND not already on-chain → genuine send failure; queue it
        # for retry so the audit trail self-heals next cycle (state-6).
        already = await asyncio.to_thread(writer.decision_exists, decision_id)
        if not already:
            queue = queue.upsert(PendingAnchor(
                decision_id=decision_id.hex(),
                snapshot_filename=snap_name,
                ipfs_cid=ipfs_cid or "",
                action_hash=action_hash.hex(),
                enqueued_at=datetime.now(UTC),
            ))
            anchor["anchor_queued"] = True
            log.warning(
                "decision %s failed to anchor on-chain — queued for retry",
                snap_name,
            )

    write_anchor_queue(queue)

    if anchor:
        outcome["onchain"] = anchor
        outcome.setdefault("stages", []).append("anchor_onchain")


async def _push_reputation_heartbeat() -> dict[str, Any] | None:
    """Best-effort: attest the agent's realized APR to the canonical
    ERC-8004 registry from the live equity history.

    Runs every cycle (post-snapshot, BEFORE the early returns) so
    reputation is a true heartbeat — independent of whether the cycle
    actually traded — unlike decision anchoring, which only fires on the
    execute path. Throttled to once an hour off-chain (`reputation`'s own
    state file); the push is recorded only after a confirmed tx so a
    failed send retries next cycle. Anything missing / not-yet-due → `None`.
    """
    if not should_push():
        return None
    score = compute_realized_apr_bps(read_equity_history())
    if score is None:
        return None
    writer = await asyncio.to_thread(OnchainWriter.from_env)
    if writer is None:
        return None
    tx = await asyncio.to_thread(writer.push_apr_reputation, score.apr_bps)
    if not tx:
        return None
    record_push(score, tx)
    log.info(
        "reputation attested on-chain: apr=%d bps (%.1f%%) tx=%s "
        "window=%.1fd baseline=$%.2f current=$%.2f",
        score.apr_bps, score.apr_bps / 100, tx,
        float(score.elapsed_seconds) / 86400,
        float(score.baseline_equity), float(score.current_equity),
    )
    return {"reputation_tx": tx, "apr_bps": score.apr_bps}


def _drop_picks_into_cash(
    decision_dict: dict[str, Any],
    blocked_keys: set[tuple[str, str]],
) -> tuple[dict[str, Any], list[str]]:
    """Remove every pick whose `(family, product_id)` is in `blocked_keys`
    and roll its weight into `cash_usdc`. Returns `(new_decision_dict,
    dropped)` where `dropped` is the list of pids actually removed (so the
    caller can log + add notes).

    Family from `_pick_family(venue_id)` — keying by `(family, pid)` not bare
    pid stops an earn-namespace id from dropping a same-numeric LM pick, or
    vice-versa (ah.23). A `("", pid)` wildcard key (legacy bare cooldown
    entry) matches a pid in ANY family, preserving in-flight cooldowns across
    the note-format change.

    Same rescale logic as `_build_auto_close_decision` (kept-pick weights
    rescale within their venue; venue weight shrinks proportionally;
    fully-emptied venues collapse to cash). Validator still runs on the
    output as a safety net.
    """
    new_venues: list[dict[str, Any]] = []
    cash_addition = 0.0
    dropped: list[str] = []
    for v in decision_dict.get("venues", []) or []:
        picks = v.get("picks", []) or []
        venue_weight = float(v.get("weight", 0))
        if not picks:
            new_venues.append(dict(v))
            continue
        fam = _pick_family(v.get("venue_id") or "")
        kept = []
        for p in picks:
            pid = str(p.get("product_id", ""))
            if (fam, pid) in blocked_keys or ("", pid) in blocked_keys:
                dropped.append(pid)
            else:
                kept.append(p)
        if len(kept) == len(picks):
            new_venues.append(dict(v))
            continue
        if not kept:
            cash_addition += venue_weight
            continue
        kept_sum = sum(float(p.get("weight", 0)) for p in kept)
        if kept_sum <= 0:
            cash_addition += venue_weight
            continue
        new_venue_weight = venue_weight * kept_sum
        cash_addition += venue_weight - new_venue_weight
        rescaled = [
            {**p, "weight": float(p.get("weight", 0)) / kept_sum} for p in kept
        ]
        new_venues.append(
            {**v, "weight": new_venue_weight, "picks": rescaled}
        )

    if cash_addition > 0:
        cash_seen = False
        for v in new_venues:
            if v.get("venue_id") == "cash_usdc":
                v["weight"] = float(v.get("weight", 0)) + cash_addition
                cash_seen = True
                break
        if not cash_seen:
            new_venues.append(
                {"venue_id": "cash_usdc", "weight": cash_addition, "picks": []}
            )
    new_decision = {**decision_dict, "venues": new_venues}
    return new_decision, dropped


def _strip_carry_coins_from_decision(
    decision: Decision,
    snapshot: Snapshot,
    coins: set[str],
) -> tuple[Decision, list[str]]:
    """Drop `bybit_funding_carry` picks for `coins` from `decision`, rolling
    freed weight into cash_usdc. Used when a `carry_liq_close` stop-loss fires:
    making the carry target-absent means the diff CLOSEs it (state-present,
    target-absent) on an executing cycle — including the auto-close fast-path —
    and crucially cannot RE-OPEN it the same cycle (`_funding_carry_diff` opens
    only on target-present + state-absent). loop-1/wt-2.

    Resolves each carry pick's coin through the snapshot's FundingCarry
    products (pick.product_id is the perp symbol). Returns the new decision +
    the dropped symbols; no-op (same decision, []) when nothing matches.
    """
    if not coins:
        return decision, []
    venue = decision.venue(CARRY_VENUE_ID)  # type: ignore[arg-type]
    if venue is None or not venue.picks:
        return decision, []
    carry_products = {
        p.product_id: p for p in snapshot.products.get(CARRY_CATEGORY, [])
    }
    drop_keys = {
        ("carry", str(pick.product_id))
        for pick in venue.picks
        if (summ := carry_products.get(pick.product_id)) is not None
        and summ.coin.upper() in coins
    }
    if not drop_keys:
        return decision, []
    new_dict, dropped = _drop_picks_into_cash(decision.model_dump(), drop_keys)
    return Decision.model_validate(new_dict), dropped


# Venues whose NEW picks draw the liquid stable pool (stable subscribes +
# hedged non-stable spot/margin). LM / advance-Earn are funded / gated
# separately, so the sub-floor clamp leaves them to the validator.
_LIQUID_CLAMP_VENUES = ("bybit_flex", "bybit_onchain")
# Same set PLUS funding-carry for the liquid-budget clamp only. A carry pick
# opens a spot Buy + perp short (the same ~2.05× USDT draw as a hedged
# non-stable), so an unfundable carry must drop to cash too — else it survives
# to `check_stable_spend_cap` and strands the cycle skipped:invalid (prod
# 2026-06-08: HYPE carry $18.23 vs $10.41 liquid, every cycle). NOT shared
# with the sub-floor clamp, which has carry-specific funding-floor semantics
# (carry is gated by `check_funding_carry_floor`, not the hedge floor).
_LIQUID_CLAMP_VENUES_CARRY = (*_LIQUID_CLAMP_VENUES, "bybit_funding_carry")
# Mirror of the executor's MIN_ACTION_USDC / validator _MIN_ACTION_USDC —
# a delta below this is a no-op the diff never acts on.
_MIN_NEW_ACTION_USD = 0.50

# Confidence recompute (`agent-yield-quality.4`). 12/14 prod cycles emitted
# EXACTLY 0.65 — one notch above the 0.60 execute gate — i.e. the LLM anchors
# on a fixed "just above the floor" number rather than scoring conviction. We
# recompute confidence DETERMINISTICALLY from the data quality of THIS cycle
# (unconfirmed APRs, snapshot data gaps, last-cycle execution failures, budget
# starvation) so a thin/risky cycle can't auto-execute on a hand-picked 0.65.
# Penalties only LOWER; the single bonus can RAISE confidence, but only when
# every pick is confirmed AND only by `CONF_BONUS_ALL_CONFIRMED` (so the
# recompute can never inflate a low LLM confidence into a live trade).
CONF_PENALTY_UNCONFIRMED_APR = 0.10  # MAX penalty for NEW non-stable estimate_apr exposure
# The unconfirmed-APR penalty scales with how much of the BOOK sits in NEW
# estimate_apr exposure, reaching the full `CONF_PENALTY_UNCONFIRMED_APR` at
# this fraction. A flat penalty dropped even a single probe-cap-sized (≤7%)
# pick below the 0.60 execute gate, so the agent could never START the
# probe→confirm flow on a high-yield unconfirmed pick and stayed parked in
# low-yield confirmed stables (live 2026-06-09). Scaling lets a small hedged
# probe execute (begin confirming the rate) while a large unconfirmed tilt
# still gates.
CONF_UNCONFIRMED_FULL_FRAC = 0.30
CONF_PENALTY_DATA_GAP = 0.10  # snapshot.errors OR a picked apr_source=missing
CONF_PENALTY_FAILED_LEGS = 0.10  # last cycle executed_partial / error / failed legs
CONF_PENALTY_BUDGET_STARVED = 0.05  # liquid clamp dropped NEW picks THIS cycle
CONF_BONUS_ALL_CONFIRMED = 0.05  # every non-cash pick apr_history / measured_yield
# Pure telemetry: warn when the current + the prior N-1 confidences are all
# equal (the 0.65-anchor signature) so an operator/dashboard can flag it.
CONF_ANCHOR_STREAK_N = 5


def _clamp_to_liquid_budget(
    decision_dict: dict[str, Any],
    snapshot: Snapshot,
    *,
    decide_captured_at: str | None = None,
    carry_state: CarryState | None = None,
) -> tuple[dict[str, Any], list[str], str | None]:
    """Deterministic backstop (`bybit-sandbox.67`): the LLM repeatedly
    over-commits NEW Earn deployment past the liquid budget even after the
    snapshot pre-computes it (`wallet.max_new_nonstable_usd` /
    `liquid_stables_usd`) AND the prompt spells it out — verified live: it
    quoted "$7.40 max new non-stable" in its own thesis then took a $14
    non-stable pick. So a prose/advisory gate isn't enough; this drops the
    largest NEW picks (whole) into cash until the cycle's new spend fits
    the budget.

    Safe-direction: only REDUCES deployment toward cash (never opens or
    enlarges), so it can't create naked exposure or a bad trade; the
    validator still runs after. Held positions kept at size (net_new <
    MIN) are never touched — only fresh/grown picks; passing `carry_state`
    credits held funding-carry the same way (executor-2), so re-stating an
    open carry isn't mistaken for fresh spend and dropped to cash. Per-category, with
    the shared-pool looseness documented on `check_stable_earn_funding`;
    this is an upstream nudge, the validator is the hard gate.

    Freshness contract (`bybit-sandbox.69`): the budget read below comes
    from `snapshot.wallet`, so `snapshot` MUST be the cycle's fresh
    snapshot — the SAME object whose serialization was fed to `decide()`,
    so the budget clamped here equals the advisory budget the LLM saw.
    Today that holds (one `collect_snapshot` per cycle). If a future
    snapshot-lifecycle refactor ever caches/reuses snapshots, pass
    `decide_captured_at` (the `captured_at` of the snapshot decide saw):
    on a mismatch the clamp degrades to a safe no-op (skip + warn) rather
    than clamping against a stale budget. Never raises — a hard failure in
    the cycle hot path would take down the heartbeat over a refactor slip.
    """
    if decide_captured_at is not None:
        fresh = False
        try:
            decided_at = datetime.fromisoformat(
                decide_captured_at.replace("Z", "+00:00")
            )
            fresh = decided_at == snapshot.captured_at
        except (ValueError, TypeError, AttributeError):
            fresh = False
        if not fresh:
            log.warning(
                "liquid clamp: snapshot captured_at (%s) != the snapshot "
                "decide saw (%s) — skipping clamp to avoid a stale budget "
                "(.69 freshness guard)",
                snapshot.captured_at, decide_captured_at,
            )
            return decision_dict, [], None

    wallet = snapshot.wallet
    total_book = float(wallet.total_equity_usd)
    liquid_stables = float(wallet.liquid_stables_usd)
    # No liquidity signal (pre-pivot fixtures / legacy collector) → no-op,
    # mirroring the validator's supply<=0 fall-through. Prod always
    # populates the liquid fields.
    if total_book <= 0 or liquid_stables <= 0:
        return decision_dict, [], None
    max_nonstable = float(wallet.max_new_nonstable_usd)

    held = _held_usd_by_product(snapshot, carry_state=carry_state)
    prod_coin: dict[tuple[str, str], str] = {}
    for venue_id in _LIQUID_CLAMP_VENUES_CARRY:
        meta = VENUE_REGISTRY.get(venue_id)
        cat = getattr(meta, "snapshot_category", None) if meta else None
        if not cat:
            continue
        for p in snapshot.products.get(cat, []):
            prod_coin[(cat, p.product_id)] = p.coin.upper()

    # (family, pid) keys (ah.23) so a dropped earn pid can't also drop a
    # same-numeric LM pick in `_drop_picks_into_cash`.
    new_stable: list[tuple[tuple[str, str], float]] = []
    new_nonstable: list[tuple[tuple[str, str], float]] = []
    for v in decision_dict.get("venues", []) or []:
        vid = v.get("venue_id")
        if vid not in _LIQUID_CLAMP_VENUES_CARRY:
            continue
        meta = VENUE_REGISTRY.get(vid)
        cat = getattr(meta, "snapshot_category", None) if meta else None
        if not cat:
            continue
        fam = _pick_family(vid)
        vw = float(v.get("weight", 0))
        for p in v.get("picks", []) or []:
            pid = str(p.get("product_id", ""))
            net_new = (
                total_book * vw * float(p.get("weight", 0))
                - held.get((cat, pid), 0.0)
            )
            if net_new < _MIN_NEW_ACTION_USD:
                continue  # hold / reduce — funds nothing
            if prod_coin.get((cat, pid), "") in STABLES:
                new_stable.append(((fam, pid), net_new))
            else:
                new_nonstable.append(((fam, pid), net_new))

    to_drop: set[tuple[str, str]] = set()

    def _select(
        picks: list[tuple[tuple[str, str], float]], budget: float
    ) -> None:
        total = sum(n for _, n in picks)
        if total <= budget + 1e-9:
            return
        # Drop largest-first so the fewest picks are sacrificed.
        for key, n in sorted(picks, key=lambda x: x[1], reverse=True):
            if total <= budget + 1e-9:
                break
            to_drop.add(key)
            total -= n

    _select(new_nonstable, max_nonstable)
    _select(new_stable, liquid_stables)

    if not to_drop:
        return decision_dict, [], None
    new_dict, dropped = _drop_picks_into_cash(decision_dict, to_drop)
    note = (
        f"liquid_clamp dropped over-budget NEW picks "
        f"{sorted(set(dropped))} → cash (liquid_stables ${liquid_stables:.2f}, "
        f"max_new_nonstable ${max_nonstable:.2f})"
    )
    return new_dict, dropped, note


def _pick_is_subfloor_nonstable(summary: Any, perp_info: Any) -> bool:
    """True when growing/opening a hedged Earn pick on this product would be
    rejected by `check_funding_rate_floor` — i.e. its realizable NET-of-hedge
    yield is not profitable. Uses the SAME `net_hedge_yield` helper as the
    validator so the clamp can't over-reject net-positive high-APR picks (the
    2026-06-08 ME bug: a +28%-net pick dumped to cash on raw funding). Stables,
    no summary, no funding signal, or a broken interval → False (the clamp
    leaves those to the validator)."""
    if summary is None:
        return False
    if (summary.coin or "").upper() in STABLES:
        return False
    net, interval_broken = net_hedge_yield(summary, perp_info)
    if net is None or interval_broken:
        return False
    return net <= NET_HEDGE_YIELD_FLOOR


def _clamp_subfloor_nonstable_growth(
    decision_dict: dict[str, Any],
    snapshot: Snapshot,
) -> tuple[dict[str, Any], list[str], str | None]:
    """Deterministic backstop (`bybit-sandbox.66` follow-up to `.67`): the LLM
    keeps GROWING (or OPENING) a hedged non-stable Earn pick whose realizable
    NET-of-hedge yield isn't profitable. `check_funding_rate_floor` rejects
    that net-new sub-floor exposure, stranding the cycle as skipped:invalid —
    even though KEEPING the position at current size is validator-exempt. A
    prose/funding-pre-filter nudge isn't enough (same lesson as `.65`/`.67`),
    so clamp each offending pick's effective weight DOWN to its current held
    size (0 if not held) and move the freed weight to cash.

    Safe-direction: only REDUCES sub-floor deployment toward cash, never opens
    or enlarges, so it can't create exposure — and keeping a held sub-floor
    position at current size is exactly what the validator allows. Uses the
    SAME `net_hedge_yield` helper as `check_funding_rate_floor` (2026-06-08:
    this clamp previously gated RAW funding ≥ -10.95%/yr and dumped a +28%-net
    ME pick to cash while the net-aware validator would have passed it — the
    drift this shared helper prevents). The validator still runs after as the
    hard gate."""
    total_book = float(snapshot.wallet.total_equity_usd)
    if total_book <= 0:
        return decision_dict, [], None
    held_map = _held_usd_by_product(snapshot)
    perp_market = getattr(snapshot, "perp_market", None) or {}
    prod_coin: dict[tuple[str, str], str] = {}
    prod_summary: dict[tuple[str, str], Any] = {}
    for venue_id in _LIQUID_CLAMP_VENUES:
        meta = VENUE_REGISTRY.get(venue_id)
        cat = getattr(meta, "snapshot_category", None) if meta else None
        if not cat:
            continue
        for p in snapshot.products.get(cat, []):
            prod_coin[(cat, p.product_id)] = p.coin.upper()
            prod_summary[(cat, p.product_id)] = p

    clamped: list[str] = []
    freed = 0.0
    new_venues: list[dict[str, Any]] = []
    for v in decision_dict.get("venues", []) or []:
        vid = v.get("venue_id")
        vw = float(v.get("weight", 0))
        meta = VENUE_REGISTRY.get(vid)
        cat = getattr(meta, "snapshot_category", None) if meta else None
        if vid not in _LIQUID_CLAMP_VENUES or not cat or vw <= 0:
            new_venues.append(v)
            continue
        # Absolute effective weight per pick, clamping sub-floor growth.
        abs_eff: list[tuple[dict[str, Any], float]] = []
        changed = False
        for p in v.get("picks", []) or []:
            pid = str(p.get("product_id", ""))
            coin = prod_coin.get((cat, pid), "")
            summary = prod_summary.get((cat, pid))
            perp_info = perp_market.get(coin) or perp_market.get(coin.lower())
            eff = vw * float(p.get("weight", 0))
            held = held_map.get((cat, pid), 0.0)
            net_new = eff * total_book - held
            if net_new > _MIN_NEW_ACTION_USD and _pick_is_subfloor_nonstable(
                summary, perp_info
            ):
                new_eff = held / total_book  # clamp to current held (0 if new)
                freed += eff - new_eff
                clamped.append(pid)
                changed = True
                abs_eff.append((p, new_eff))
            else:
                abs_eff.append((p, eff))
        if not changed:
            new_venues.append(v)
            continue
        new_vw = sum(e for _, e in abs_eff)
        if new_vw <= 1e-9:
            continue  # whole venue clamped away → freed weight goes to cash
        new_picks = [
            {**p, "weight": e / new_vw} for p, e in abs_eff if e > 1e-9
        ]
        new_venues.append({**v, "weight": new_vw, "picks": new_picks})

    if not clamped:
        return decision_dict, [], None

    # Park the freed weight in cash_usdc (merge into an existing cash venue or
    # append one) — same shape as `_drop_picks_into_cash`'s cash handling.
    cash_seen = False
    for v in new_venues:
        if v.get("venue_id") == "cash_usdc":
            v["weight"] = float(v.get("weight", 0)) + freed
            cash_seen = True
            break
    if not cash_seen:
        new_venues.append({"venue_id": "cash_usdc", "weight": freed, "picks": []})

    new_dict = {**decision_dict, "venues": new_venues}
    note = (
        f"subfloor_clamp held sub-floor non-stable growth → cash "
        f"{sorted(set(clamped))} (net-of-hedge yield <= "
        f"{NET_HEDGE_YIELD_FLOOR * 100:.0f}% — hedge not profitable; kept at "
        f"current size)"
    )
    return new_dict, clamped, note


def _iter_picked_summaries(
    decision_dict: dict[str, Any],
    snapshot: Snapshot,
) -> list[tuple[str, Any, float]]:
    """Resolve every ranker-backed pick to `(category, summary, net_new_usd)`.

    `net_new_usd` is `target − held` (the delta the live diff acts on), held
    USD taken from `_held_earn_detail` so the classification matches
    `check_estimate_apr_probe_cap` exactly. Picks whose product_id isn't in the
    snapshot (hallucination — owned by `check_product_ids_in_snapshot`) are
    skipped. `cash_usdc` / picks-less venues contribute nothing."""
    total_book = float(snapshot.wallet.total_equity_usd)
    detail = _held_earn_detail(snapshot)
    idx = _snapshot_index(snapshot)
    out: list[tuple[str, Any, float]] = []
    for v in decision_dict.get("venues", []) or []:
        vid = v.get("venue_id")
        meta = VENUE_REGISTRY.get(vid)
        cat = getattr(meta, "snapshot_category", None) if meta else None
        if not getattr(meta, "requires_picks", False) or not cat:
            continue
        vw = float(v.get("weight", 0))
        cat_idx = idx.get(cat, {})
        for p in v.get("picks", []) or []:
            pid = str(p.get("product_id", ""))
            summary = cat_idx.get(pid)
            if summary is None:
                continue
            target = total_book * vw * float(p.get("weight", 0))
            held = float(detail.get((cat, pid), {}).get("usd", 0.0))
            out.append((cat, summary, target - held))
    return out


def _recompute_confidence(
    decision_dict: dict[str, Any],
    snapshot: Snapshot,
    priors: list[dict[str, Any]] | None,
) -> tuple[float, list[str]]:
    """Deterministic confidence from THIS cycle's data quality
    (`agent-yield-quality.4`). The LLM anchors confidence on a fixed 0.65 (one
    notch above the 0.60 execute gate) instead of scoring conviction, so a thin
    or risky cycle auto-executes on a hand-picked number. Recompute it from
    signals the LLM can't fudge:

      − unconfirmed APR: any NEW (net_new > `_MIN_NEW_ACTION_USD`) non-stable
        pick still on `estimate_apr` (a quoted rate that may be a transient
        promo, classified exactly as `check_estimate_apr_probe_cap`);
      − data gap: `snapshot.errors` non-empty OR any picked product priced off
        `apr_source == "missing"` (yield can't be trusted);
      − failed legs: the latest prior cycle came back `executed_partial` /
        `error` (or carried failed legs) — execution risk that should temper
        the next bet;
      − budget starved: this cycle's liquid clamp dropped NEW picks (the plan
        didn't fit the funds), keyed off `outcome[liquid_clamp_dropped]`.

    One bonus RAISES: every non-cash pick confirmed (`apr_history` /
    `measured_yield`). Penalties stack down to the `MIN_CONFIDENCE` floor; the
    result is additionally clamped to `base + CONF_BONUS_ALL_CONFIRMED` so the
    recompute can lift confidence ONLY via the explicit confirmed bonus — never
    inflate a low LLM confidence into a live trade. Pure: no IO, no mutation."""
    base = float(decision_dict.get("confidence", 0.0))
    picks = _iter_picked_summaries(decision_dict, snapshot)
    reasons: list[str] = []
    new = base

    total_book = float(snapshot.wallet.total_equity_usd)
    unconfirmed_new_usd = sum(
        net_new
        for _cat, s, net_new in picks
        if net_new > _MIN_NEW_ACTION_USD
        and (s.coin or "").upper() not in STABLES
        and s.apr_source == "estimate_apr"
    )
    if unconfirmed_new_usd > 0 and total_book > 0:
        # Penalty SCALES with the NEW unconfirmed book fraction (full at
        # CONF_UNCONFIRMED_FULL_FRAC) so a probe-cap-sized pick barely dents
        # confidence and the probe→confirm flow can start, while a large
        # unconfirmed tilt still gates. See CONF_UNCONFIRMED_FULL_FRAC.
        frac = unconfirmed_new_usd / total_book
        penalty = CONF_PENALTY_UNCONFIRMED_APR * min(1.0, frac / CONF_UNCONFIRMED_FULL_FRAC)
        new -= penalty
        reasons.append(
            f"unconfirmed_apr (NEW non-stable estimate_apr {frac:.1%} of book): "
            f"-{penalty:.3f}"
        )

    picked_missing = any(s.apr_source == "missing" for _cat, s, _nn in picks)
    # Scope the data-gap penalty to errors that touch THIS decision's picks.
    # `snapshot.errors` is a catch-all dominated by benign peripheral failures
    # — advance_position rate-limits, a perp ticker for an UNPICKED coin like
    # METH — present on essentially every cycle. A blanket trigger would dock
    # −0.10 every cycle, pushing the 0.65 anchor below the 0.60 execute gate
    # and silently halting trading. Match the coin only as a bracketed token
    # (`[BERA]`), so an unpicked `perp_market[METH]` can't false-trigger an ETH
    # pick. The missing-APR case is always pick-relevant.
    picked_coins = {(s.coin or "").upper() for _cat, s, _nn in picks if s.coin}
    pick_relevant_error = any(
        f"[{coin}]" in err.upper()
        for err in snapshot.errors
        for coin in picked_coins
    )
    if picked_missing or pick_relevant_error:
        new -= CONF_PENALTY_DATA_GAP
        reasons.append(
            f"data_gap (pick-relevant snapshot.error={pick_relevant_error} "
            f"picked_missing_apr={picked_missing}): -{CONF_PENALTY_DATA_GAP}"
        )

    latest = (priors or [])[-1] if priors else None
    prior_outcome = (latest or {}).get("_cycle_outcome") or {}
    failed_legs = int(prior_outcome.get("actions_failed") or 0)
    if prior_outcome.get("result") in ("executed_partial", "error") or failed_legs > 0:
        new -= CONF_PENALTY_FAILED_LEGS
        reasons.append(
            f"failed_legs (last cycle result={prior_outcome.get('result')!r} "
            f"failed={failed_legs}): -{CONF_PENALTY_FAILED_LEGS}"
        )

    if decision_dict.get("_outcome_liquid_clamp_dropped"):
        new -= CONF_PENALTY_BUDGET_STARVED
        reasons.append(
            f"budget_starved (liquid clamp dropped NEW picks): "
            f"-{CONF_PENALTY_BUDGET_STARVED}"
        )

    if picks and all(
        s.apr_source in ("apr_history", "measured_yield") for _cat, s, _nn in picks
    ):
        new += CONF_BONUS_ALL_CONFIRMED
        reasons.append(
            f"all_confirmed (every pick apr_history/measured_yield): "
            f"+{CONF_BONUS_ALL_CONFIRMED}"
        )

    # Floor at MIN_CONFIDENCE so a deterministic penalty never flips a VALID
    # cycle (base >= floor) to skipped:invalid — but never RESCUE an
    # already-sub-floor LLM confidence up to the floor either (that would let a
    # 0.30-conviction cycle pass the validator's confidence gate). So the lower
    # bound is `min(MIN_CONFIDENCE, base)`.
    new = max(min(MIN_CONFIDENCE, base), min(1.0, new))
    # The recompute can RAISE only via the explicit confirmed bonus — never
    # inflate a low LLM confidence into a live trade by stacking nothing.
    new = min(new, base + CONF_BONUS_ALL_CONFIRMED)
    return new, reasons


def _confidence_anchor_warning(
    current_conf: float,
    priors: list[dict[str, Any]] | None,
    n: int = CONF_ANCHOR_STREAK_N,
) -> str | None:
    """Pure telemetry (`agent-yield-quality.4`): return a warning string when
    `current_conf` plus the most recent `n-1` prior confidences are ALL equal
    (within 1e-6) — the fixed-anchor signature (12/14 prod cycles emitted
    exactly 0.65). Never blocks or mutates; the caller logs + records it.

    Returns None when there aren't enough priors, or the streak is broken."""
    if n <= 1 or not priors or len(priors) < n - 1:
        return None
    recent = [float(d.get("confidence", -1.0)) for d in priors[-(n - 1):]]
    streak = [current_conf, *recent]
    if all(abs(c - current_conf) <= 1e-6 for c in streak):
        return (
            f"confidence anchored: last {n} cycles all emitted "
            f"{current_conf:.2f} (≥ a fixed anchor, not a conviction score)"
        )
    return None


def _recompute_expected_apr(
    decision_dict: dict[str, Any],
    snapshot: Snapshot,
) -> tuple[float, list[dict[str, Any]]]:
    """Deterministic `expected_blended_apr_pct` from the snapshot's per-pick
    APR (`agent-yield-quality.5`). The headline feeds the on-chain DecisionLog
    + IPFS rationale, but it's currently the LLM's hand-computed number. Blend
    it from data instead:

        weight_in_book = venue.weight * pick.weight
        pick_apr       = net-of-hedge APR for non-stables
                         (`effective_apr_net_hedge` if present, else
                         `effective_apr_net_holding`, else `effective_apr`),
                         the plain effective APR for stables;
                         `cash_usdc` (no picks) contributes 0.
        blended (pct)  = sum(weight_in_book * pick_apr) * 100

    Net-of-hedge matters: a hedged 101% Earn at -37% funding is ~+64% net, not
    101%. Snapshot APRs are fractional [0,1]; the headline is percent
    (4.07 = 4.07%), so the blend is ×100. Returns `(apr_pct, breakdown)` where
    breakdown is one row per contributing pick (for logging / audit). The
    headline is floored at 0 to satisfy the schema (`expected_blended_apr_pct
    >= 0`) — a negative net blend means a bleeding hedge, which
    `check_funding_rate_floor` independently rejects post-recompute. Pure."""
    idx = _snapshot_index(snapshot)
    blended_frac = 0.0
    breakdown: list[dict[str, Any]] = []
    for v in decision_dict.get("venues", []) or []:
        vid = v.get("venue_id")
        vw = float(v.get("weight", 0))
        meta = VENUE_REGISTRY.get(vid)
        cat = getattr(meta, "snapshot_category", None) if meta else None
        if not getattr(meta, "requires_picks", False) or not cat:
            continue  # cash_usdc + picks-less venues contribute 0
        cat_idx = idx.get(cat, {})
        for p in v.get("picks", []) or []:
            summary = cat_idx.get(str(p.get("product_id", "")))
            if summary is None:
                continue
            weight_in_book = vw * float(p.get("weight", 0))
            if (summary.coin or "").upper() in STABLES:
                pick_apr = summary.effective_apr
            else:
                pick_apr = (
                    summary.effective_apr_net_hedge
                    if summary.effective_apr_net_hedge is not None
                    else summary.effective_apr_net_holding
                    if summary.effective_apr_net_holding is not None
                    else summary.effective_apr
                )
            contrib = weight_in_book * float(pick_apr)
            blended_frac += contrib
            breakdown.append(
                {
                    "venue_id": vid,
                    "product_id": summary.product_id,
                    "coin": summary.coin,
                    "weight_in_book": weight_in_book,
                    "pick_apr_pct": float(pick_apr) * 100,
                }
            )
    return max(0.0, blended_frac * 100), breakdown


# `pick_invalidated` events encode the closed position as "earn:<pid>"
# (FlexibleSaving/OnChain) or "lm:<pid>" (LiquidityMining). Those productId
# spaces OVERLAP (confirmed across 108 snapshots: {6,7,13,14,15,18,19,22,24,
# 26}), so the auto-close walk must qualify a drop by VENUE FAMILY — an
# `lm:14` event may not drop a same-id earn pick, and vice-versa (loop-2).
_AUTO_CLOSE_FAMILY_BY_VENUE: dict[str, str] = {
    "bybit_flex": "earn",
    "bybit_onchain": "earn",
    "bybit_lm": "lm",
}


def _pick_family(venue_id: str) -> str:
    """Collision namespace for a pick's `product_id` (ah.23). Earn productIds
    (flex + onchain share ONE Bybit Earn namespace) and LM productIds are
    SEPARATE id spaces that numerically overlap (empirically {6,7,13,…}), so a
    bare-pid match across them drops / closes the WRONG pick. Keying drops and
    cooldowns by `(family, product_id)` disambiguates. `carry` for the carry
    venue; the venue_id itself for anything outside the known families (a safe
    unique fallback)."""
    return _AUTO_CLOSE_FAMILY_BY_VENUE.get(venue_id) or (
        "carry" if venue_id == CARRY_VENUE_ID else venue_id
    )


# Part 4 — time a funding-driven auto-close to ~20s before settlement. OFF by
# default (immediate close, the pre-existing behavior); flip the env flag to
# enable. Money path, so kept reversible without a redeploy.
WATCHER_SETTLEMENT_TIMED_CLOSE = (
    os.environ.get("VAULT8004_WATCHER_SETTLEMENT_TIMED_CLOSE", "0") == "1"
)
# Hard cap on the pre-settlement wait — must stay under the watcher poll interval
# (120s) so the close never blocks longer than one poll. A settlement further out
# than this means there's nothing to time yet → close immediately.
MAX_PRE_SETTLE_SLEEP_S = 110.0


def _settlement_close_delay_s(
    wake_events: list[dict[str, Any]] | None,
    now_ms: int,
    enabled: bool = WATCHER_SETTLEMENT_TIMED_CLOSE,
) -> float:
    """Seconds to wait before executing an auto-close so a funding-driven exit
    lands ~20s before settlement (Part 4, bounded best-effort).

    Returns 0.0 (execute immediately) when: the flag is off; no firing
    `pick_invalidated` event carries `next_funding_ms` (price/peg/liq closes stay
    immediate); or the soonest settlement is further out than one poll interval
    (`MAX_PRE_SETTLE_SLEEP_S` — nothing to time yet, close now)."""
    if not enabled or not wake_events:
        return 0.0
    next_fundings: list[int] = []
    for e in wake_events:
        if e.get("kind") != "pick_invalidated":
            continue
        raw = (e.get("current") or {}).get("next_funding_ms")
        if raw is None:
            continue
        try:
            next_fundings.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not next_fundings:
        return 0.0
    delay = seconds_until_close_target(min(next_fundings), now_ms)
    return delay if delay <= MAX_PRE_SETTLE_SLEEP_S else 0.0


def _build_auto_close_decision(
    prior: dict[str, Any] | None,
    wake_events: list[dict[str, Any]] | None,
    recently_closed_keys: frozenset[tuple[str, str]] = frozenset(),
) -> dict[str, Any] | None:
    """Build a deterministic close-only decision from `pick_invalidated`
    wake events — bypasses the LLM entirely so a tripped stop-loss
    closes within seconds, not minutes (LLM round-trip + validator pass).

    `recently_closed_keys` are `(family, product_id)` pairs already auto-closed
    within the cooldown window (loop-4 / ah.23): an event whose `(family, pid)`
    is in this set is skipped, so a persistently-firing invalidation (e.g. a
    redeem stuck in Processing, still echoed in the baseline) doesn't re-trigger
    an all-cash no-op every wake. Keying by `(family, pid)` — not bare pid —
    means a recently-closed LM pick can't suppress a same-numeric Earn close.
    A `("", pid)` wildcard entry (legacy bare cooldown) still suppresses any
    family, preserving in-flight cooldowns across the note-format change.

    Returns:
      - decision dict (mutated copy of `prior` with affected picks
        removed and freed weight rolled into cash_usdc), OR
      - None when no auto-close-eligible event is present (caller falls
        through to the normal LLM decide path).

    The mutation logic:
      • For each `pick_invalidated` event, collect the closed
        `(family, product_id)` key (family from `position_id="earn:<pid>"`
        OR `"lm:<pid>"`) + coin.
      • Walk the prior decision's venues. For each venue, a pick is dropped
        only when its venue family matches the event family AND its
        product_id matches — productId spaces overlap across families.
        Remaining picks rescale within the venue to sum to 1.0, venue weight
        scales down proportionally. If all picks are closed, drop the venue.
      • All freed venue weight goes to cash_usdc (added to its current
        weight, or appended as a new entry if absent).
      • `hedges` array zeroed — auto-hedge derives from picks, so
        removing an Earn pick auto-closes its paired perp via
        `diff_to_actions`. For a dropped LM pick the diff issues a
        full REDEEM_LM; the paired short is INTENTIONALLY left open this
        cycle (the LP redeem is async, so the short still backs real base
        exposure until the LP settles) — it goes orphan and
        `_orphan_perp_close_actions` closes it on a later cycle. Do NOT
        "fix" this by zeroing the LM short here: that would strip the
        hedge while the LP principal is still live.

    Funding-carry near-liq is NOT handled here (carry has no pick/productId
    to drop) — the watcher emits a separate `carry_liq_close` event the
    loop's de-risk sweep consumes; an `carry_liq_close`-only cycle yields no
    close_pids → this returns None → normal cycle handles it.

    The output is structurally a valid Decision dict (sums to 1.0,
    only known venue_ids, picks well-formed). Validator still runs on
    it as a sanity net; with deterministic mutation the only realistic
    failure is the cash_usdc venue exceeding its max_weight (1.0), so
    in practice it always passes.
    """
    if not prior or not wake_events:
        return None
    close_keys: set[tuple[str, str]] = set()
    close_pids: set[str] = set()
    close_coins: set[str] = set()
    for e in wake_events:
        if (e.get("kind") or "") != "pick_invalidated":
            continue
        position_id = e.get("position_id") or ""
        family, sep, pid = position_id.partition(":")
        if not sep or family not in ("earn", "lm") or not pid:
            continue
        if (family, pid) in recently_closed_keys or (
            "", pid
        ) in recently_closed_keys:
            # Already auto-closed within the cooldown window — suppress so a
            # persistently-firing event doesn't churn all-cash cycles (loop-4).
            # `(family, pid)` keyed (ah.23); `("", pid)` = legacy bare entry.
            continue
        close_keys.add((family, pid))
        close_pids.add(pid)
        coin = e.get("coin") or ""
        if coin:
            close_coins.add(coin.upper())
    if not close_keys:
        return None

    new_venues: list[dict[str, Any]] = []
    cash_addition = 0.0
    for v in prior.get("venues", []) or []:
        picks = v.get("picks", []) or []
        venue_weight = float(v.get("weight", 0))
        if not picks:
            new_venues.append(dict(v))
            continue
        vfam = _AUTO_CLOSE_FAMILY_BY_VENUE.get(v.get("venue_id") or "")
        kept = [
            p for p in picks
            if vfam is None
            or (vfam, str(p.get("product_id", ""))) not in close_keys
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
        "notes": [
            f"auto_close:{fam}:{pid}" for fam, pid in sorted(close_keys)
        ],
        "expected_blended_apr_pct": 0.0,
    }
    return out


def _reconcile_carry_state(
    carry_state: CarryState,
    results: list[Any],
    outcome: dict[str, Any],
    *,
    context: str,
) -> None:
    """Roll carry state forward from dispatch `results` and persist iff it
    changed. Used by BOTH the executed-diff path and the de-risk sweep — the
    sweep closes carry positions on Bybit (`_carry_liq_close_actions`) and
    MUST write the closure back, else the state file stays stale → dup closes
    + unbounded retry next cycle (executor-1/wt-1/state-1).

    On write failure trip the HALT marker: the real Bybit position may now be
    open/closed while the file is stale, so the operator MUST manually
    reconcile before the agent resumes. `context` labels the call path in the
    HALT message + logs. No-op on empty `results`.
    """
    if not results:
        return
    try:
        new_state = apply_carry_results_to_state(carry_state, results)
        if new_state.positions != carry_state.positions:
            write_carry_state(new_state)
            outcome.setdefault("stages", []).append("carry_state_updated")
    except Exception as e:  # noqa: BLE001
        outcome["carry_state_error"] = f"{type(e).__name__}: {e}"
        halt(
            f"carry_state write failed after {context} "
            f"({type(e).__name__}: {e}) — manual reconciliation required "
            f"before resume"
        )
        log.exception(
            "carry_state update failed after %s — HALT created", context
        )


async def _execute_with_recovery(
    client: Any,
    actions: list[Any],
    snapshot_ts: str,
    carry_state: CarryState,
    outcome: dict[str, Any],
    *,
    context: str,
    dry_run: bool,
) -> list[Any]:
    """Unified live-execute envelope (ah.24) — shared by the main diff and the
    de-risk sweep so the sweep stops being a parallel dispatch
    (executor-1/executor-3). Steps, all skipped on dry-run:

      1. Persist the confirmable order links as a pending-intent marker BEFORE
         dispatch — a SIGKILL mid-`execute_actions` (esp. a carry close's
         spot+perp legs the executions scan treats as unconfirmable) is then
         recoverable by the startup crash gate (ah.7).
      2. `execute_actions`, then reconcile `carry_state` in a `finally` so a
         real Bybit close is rolled into state even if later code raises
         (executor-1/wt-1/state-1).
      3. Clear the marker once execute + reconcile are durable — but NOT on a
         reconcile HALT (`carry_state_error`), so the operator keeps the leg
         links; an `execute_actions` exception propagates past the clear so
         the marker survives for the next startup to re-verify.

    Returns the per-action results; the caller does its own outcome
    accounting on them."""
    results: list[Any] = []
    if not dry_run:
        links = _confirmable_order_links(actions)
        if links:
            write_pending_intent(
                PendingIntent(snapshot_ts=snapshot_ts, links=links)
            )
    try:
        results = await execute_actions(
            client, actions, snapshot_ts=snapshot_ts, dry_run=dry_run
        )
    finally:
        if not dry_run:
            _reconcile_carry_state(
                carry_state, results, outcome, context=context
            )
    if not dry_run and "carry_state_error" not in outcome:
        clear_pending_intent()
    return results


async def _execute_redeem_exits(
    client: BybitClient,
    snap: Any,
    snapshot_ts: str,
) -> tuple[list[dict[str, Any]], int]:
    """Deterministic (no-LLM) exit for settled hedged-Earn redeems.

    For each durable `RedeemExitIntent` whose coin has arrived in `snap`, close
    the paired perp short + swap the freed coin to a stable, sized from the
    RECORDED redeem (capped at the live wallet). Returns `(records, executed)`
    where `executed` counts confirmed `ok` orders. An intent is removed only
    when ALL its legs confirm — a partial failure keeps it for next-cycle retry
    (durable-intent-retry, strictly safer than rolling back a leg). The orphan
    re-derivation sweep remains the backstop, so this never strands a coin."""
    state = read_redeem_intents()
    if not state.intents:
        return [], 0

    # Live Earn amount per productId from this cycle's snapshot (a vanished /
    # zeroed row means the redeem fully settled).
    earn_amt: dict[str, Decimal] = {}
    for p in snap.earn_positions or []:
        data = p.model_dump(mode="python") if hasattr(p, "model_dump") else p
        pid = str(data.get("productId") or data.get("id") or "")
        if not pid:
            continue
        try:
            earn_amt[pid] = Decimal(str(data.get("amount", "0") or "0"))
        except (ArithmeticError, TypeError, ValueError):
            continue

    records: list[dict[str, Any]] = []
    executed = 0
    changed = False
    for i, intent in enumerate(list(state.intents)):
        # GATE: only act once the redeem has SETTLED (coin arrived / Earn row
        # gone). Acting earlier would close the paired short while the coin is
        # still in Earn — the naked-long bug this whole feature avoids.
        wallet_native = _coin_wallet_native(snap, intent.coin)
        if not check_earn_redeem_settled(
            intent, earn_amt.get(intent.product_id), wallet_native
        ):
            continue
        actions = exit_actions_from_intent(
            snap, intent, snapshot_ts=snapshot_ts, idx_offset=900 + i * 4
        )
        if not actions:
            # Settled but nothing actionable (already flat) → drop the intent.
            state = state.remove(intent.product_id)
            changed = True
            continue
        results = await execute_actions(
            client, actions, snapshot_ts=snapshot_ts, dry_run=False
        )
        executed += sum(1 for r in results if r.status == "ok")
        records.extend(
            {
                "kind": r.action.kind.value,
                "product_id": r.action.product_id,
                "coin": r.action.coin,
                "amount": str(r.action.amount),
                "status": r.status,
                "error": r.error,
            }
            for r in results
        )
        if all(r.status == "ok" for r in results):
            state = state.remove(intent.product_id)
            changed = True
        else:
            log.warning(
                "redeem-exit %s partial (%s) — intent kept for retry",
                intent.coin, intent.product_id,
            )
    if changed:
        write_redeem_intents(state)
    return records, executed


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

    # 0. Halt check — operator-controlled kill switch + auto-tripped on
    # state-coherence failures (carry_state write errors trip this so the
    # next cycle can't double-position on a stale state file). Checked
    # BEFORE snapshot to avoid an unnecessary API round-trip when the
    # agent is already meant to be off.
    halted, halt_reason = is_halted()
    if halted and halt_trigger() != "daily_drawdown":
        # Operator halt (manual touch, carry-coherence trip, startup gate) —
        # full stop, no API round-trip and no auto-recovery. A drawdown halt
        # falls through to snapshot below so equity keeps being sampled and
        # the marker can self-clear once the book recovers (state-8).
        outcome["result"] = "halted"
        outcome["halt_reason"] = halt_reason
        outcome["finished_at"] = datetime.now(UTC).isoformat()
        log.warning("cycle skipped: %s", halt_reason)
        return outcome

    try:
        # 1. Snapshot
        snap = await collect_snapshot(
            bybit_client,
            mantle_rpc_url=mantle_rpc_url,
            mantle_vault_address=mantle_vault_address,
        )
        # Min-hold ledger: stamp/age every non-stable exposure so the
        # `check_min_hold` validator gate can block a voluntary churn out of a
        # position too young to have earned back its round-trip friction. Done
        # BEFORE write_snapshot so the on-disk snapshot, the LLM input, and the
        # validator all see the same ages. Best-effort — a ledger IO failure
        # leaves ages empty (gate fails open) and never aborts the cycle.
        try:
            snap.held_coin_ages = update_ledger_and_ages(
                snap, now=datetime.now(UTC)
            )
        except Exception as e:  # noqa: BLE001 — best effort, never abort a cycle
            log.warning("position ledger update failed: %s", e)
        snap_path = write_snapshot(snap)
        outcome["snapshot_filename"] = snap_path.name
        outcome["stages"].append("snapshot")

        # 1b. Daily-drawdown circuit breaker — trips the HALT marker
        # if the wallet has lost more than the configured pct over the
        # 24h window. Recording AND checking happens here so even
        # halted runs still extend the history (drawdown can recover
        # only if data is being collected). On trip we fall through to
        # the outer halt-after-return path, but explicitly mark the
        # outcome so the cycle log distinguishes "halted by drawdown"
        # from "halted by operator".
        current_equity = snap.wallet.total_equity_usd
        record_equity(current_equity)

        # Reputation heartbeat — attest realized APR to ERC-8004 from the
        # equity history we just extended. Done here (before the drawdown
        # early-returns) so reputation reflects reality every cycle, not
        # only on the execute path. Best-effort + throttled internally.
        rep = await _push_reputation_heartbeat()
        if rep:
            outcome["reputation"] = rep
            outcome["stages"].append("reputation_heartbeat")

        drawdown_hit, drawdown_reason = check_daily_drawdown(current_equity)

        if halted:
            # Reached here ONLY on a drawdown halt (operator halts returned
            # above). We've now extended the equity history with this cycle's
            # sample — if the 24h drawdown is back within threshold, clear the
            # marker and resume; otherwise stay halted (state-8 auto-recovery).
            if drawdown_hit:
                outcome["result"] = "halted"
                outcome["halt_reason"] = halt_reason
                outcome["halt_trigger"] = "daily_drawdown"
                outcome["finished_at"] = datetime.now(UTC).isoformat()
                log.warning("drawdown HALT persists (not yet recovered): %s",
                            halt_reason)
                return outcome
            clear_halt()
            outcome["drawdown_recovered"] = True
            log.warning(
                "daily-drawdown HALT auto-cleared: 24h drawdown back within "
                "threshold (equity=$%s) — resuming cycle", current_equity,
            )
        elif drawdown_hit and drawdown_reason is not None:
            halt(drawdown_reason, trigger="daily_drawdown")
            outcome["result"] = "halted"
            outcome["halt_reason"] = drawdown_reason
            outcome["halt_trigger"] = "daily_drawdown"
            outcome["finished_at"] = datetime.now(UTC).isoformat()
            return outcome

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

        # 1c. Stable consolidation (2026-06-08). Idle NON-CORE stables
        # (e.g. USD1 principal left by a Flex redeem) are invisible to the
        # USDC+USDT liquid budget and are never swept by the orphan-seller
        # (it skips stables), so they sit at 0% forever — observed live:
        # ~$42 USD1 stranded while the agent reported "budget too thin".
        # Rebase them to a core stable (USDT) here (live only, every cycle,
        # independent of the decision — a pure stable→stable move with no
        # directional risk). The freed USDT re-enters `liquid_stables_usd`
        # on the NEXT snapshot, so the agent deploys it instead of idling.
        if live:
            # Best-effort + isolated: a transient swap failure here must NOT
            # abort the cycle's decide/execute. (Unlike the post-decide
            # safety sweep, this runs pre-decide on EVERY live cycle, so an
            # unguarded raise would block the rebalance.)
            try:
                consolidate = _stable_consolidate_actions(
                    snap, snap_path.stem, idx_offset=700
                )
                if consolidate:
                    consolidate_results = await execute_actions(
                        bybit_client,
                        consolidate,
                        snapshot_ts=snap_path.stem,
                        dry_run=False,
                    )
                    outcome["stable_consolidate"] = [
                        {
                            "product_id": r.action.product_id,
                            "coin": r.action.coin,
                            "amount": str(r.action.amount),
                            "status": r.status,
                            "error": r.error,
                        }
                        for r in consolidate_results
                    ]
                    done = sum(1 for r in consolidate_results if r.status == "ok")
                    log.info(
                        "stable consolidation: rebased %d/%d idle non-core "
                        "stable balance(s) → USDT (freed liquid deploys next cycle)",
                        done, len(consolidate),
                    )
            except Exception as e:  # noqa: BLE001 — pre-decide side task
                outcome["stable_consolidate_error"] = f"{type(e).__name__}: {e}"
                log.warning("stable consolidation failed (non-fatal): %s", e)

        # 1d. Deterministic settled-redeem exit (no LLM). The watcher fires
        # `earn_redeem_settled` the moment a hedged-Earn redeem's freed coin
        # lands; this swaps EXACTLY the redeemed amount to a stable + closes the
        # paired short in one cycle, sized from the durable intent. Pure risk
        # reduction, so it runs on EVERY live cycle (cheap when no intents) and
        # never touches the LLM. If it executes anything we return early — the
        # book is now delta-flat on that coin and the next cycle rebalances
        # cleanly, sidestepping any double-act with the post-decide sweep.
        if live:
            try:
                redeem_records, redeem_done = await _execute_redeem_exits(
                    bybit_client, snap, snap_path.stem
                )
                if redeem_records:
                    outcome["redeem_exit"] = redeem_records
                if redeem_done:
                    outcome["result"] = "executed_redeem_exit"
                    outcome["stages"].append("redeem_exit")
                    outcome["finished_at"] = datetime.now(UTC).isoformat()
                    return outcome
            except Exception as e:  # noqa: BLE001 — pre-decide side task
                outcome["redeem_exit_error"] = f"{type(e).__name__}: {e}"
                log.warning("redeem-exit step failed (non-fatal): %s", e)

        # 2. Decide — auto-close fast-path when ANY pick_invalidated
        # event is in the wake set. Deterministic close from the prior
        # decision; skips LLM entirely so the stop-loss closes in
        # seconds rather than waiting for a Claude round-trip + token
        # cost. Per-coin events fan out to the matching pick(s) and
        # roll freed weight to cash. Falls through to the LLM path
        # when no eligible event is present.
        #
        # `mainnet-operations.4` memory layer: load up to MEMORY_DEPTH
        # priors (oldest → newest) for the LLM path; auto-close only
        # cares about the latest, taken as `priors[-1]`.
        priors = _load_recent_prior_decisions()
        latest_prior = priors[-1] if priors else None
        # Pids auto-closed within the cooldown window — gate BOTH the
        # auto-close fast-path (loop-4: suppress re-firing) and the LLM-path
        # re-pick filter below off the same set, computed once.
        cooldown = _collect_recently_invalidated(priors or [])
        auto_close = _build_auto_close_decision(
            latest_prior, wake_events, frozenset(cooldown.keys())
        )
        usage = None  # set only on the LLM path; auto-close skips Anthropic
        if auto_close is not None:
            log.info(
                "auto-close path: pick_invalidated event(s) — "
                "skipping LLM, deterministic close"
            )
            # Part 4: when enabled, hold a funding-driven close until ~20s before
            # settlement so the exit dodges one more funding tick. Bounded by
            # MAX_PRE_SETTLE_SLEEP_S; 0 for liq/peg/price closes (always immediate).
            delay_s = _settlement_close_delay_s(
                wake_events, int(datetime.now(UTC).timestamp() * 1000)
            )
            if delay_s > 0:
                log.info(
                    "settlement-timed close: holding %.0fs to land ~%ds before "
                    "funding settlement",
                    delay_s,
                    PRE_SETTLE_TARGET_S,
                )
                await asyncio.sleep(delay_s)
            decision = Decision.model_validate(auto_close)
            outcome["auto_close"] = True
        else:
            decision, usage = await decide(
                raw_snapshot,
                client=anthropic_client,
                prior_decisions=priors,
                wake_events=wake_events,
            )
            outcome["usage"] = usage.to_dict()
            outcome["estimated_cost_usd"] = float(usage.estimated_cost_usd)
            # Cooldown filter — strip any pick whose product_id was
            # auto-closed within PICK_INVALIDATE_COOLDOWN_MIN. Hard gate
            # so even if the LLM ignores the COOLDOWN ACTIVE banner in
            # the prompt, ping-pong re-entry doesn't reach the executor.
            # Reuses the `cooldown` computed above for the auto-close gate.
            if cooldown:
                blocked_keys = set(cooldown.keys())
                filtered_dict, dropped = _drop_picks_into_cash(
                    decision.model_dump(), blocked_keys
                )
                if dropped:
                    notes_list = filtered_dict.setdefault("notes", [])
                    notes_list.append(
                        f"cooldown_filter dropped re-picked pids: "
                        f"{','.join(sorted(set(dropped)))}"
                    )
                    log.warning(
                        "cooldown filter: LLM re-picked recently-invalidated "
                        "pids %s — rolled into cash_usdc",
                        sorted(set(dropped)),
                    )
                    decision = Decision.model_validate(filtered_dict)
                    outcome["cooldown_dropped"] = sorted(set(dropped))
            # Liquid-budget clamp (`.67`) — deterministic backstop for the
            # LLM over-committing NEW deployment past the pre-computed
            # liquid budget. Drops the largest over-budget NEW picks into
            # cash so the cycle produces a fundable decision instead of a
            # validator reject. Runs after the cooldown drop (both are
            # post-decide, pre-validate); validator still gates the result.
            clamped_dict, clamp_dropped, clamp_note = _clamp_to_liquid_budget(
                decision.model_dump(), snap,
                # `.69` freshness guard: tie the clamp budget to the exact
                # snapshot decide() saw, so a future snapshot-reuse refactor
                # degrades to a safe no-op instead of a stale clamp.
                decide_captured_at=raw_snapshot.get("captured_at"),
                # executor-2: credit held carry so a re-stated open carry
                # isn't counted as fresh spend and dropped to cash.
                carry_state=read_carry_state(),
            )
            if clamp_dropped:
                notes_list = clamped_dict.setdefault("notes", [])
                if clamp_note:
                    notes_list.append(clamp_note)
                log.warning(
                    "liquid clamp: LLM over-committed NEW picks past liquid "
                    "budget — rolled %s into cash_usdc",
                    sorted(set(clamp_dropped)),
                )
                decision = Decision.model_validate(clamped_dict)
                outcome["liquid_clamp_dropped"] = sorted(set(clamp_dropped))
            # Sub-floor non-stable clamp (`.66`) — deterministic backstop for
            # the LLM growing/opening a hedged non-stable whose funding is
            # below the hedge floor (check_funding_rate_floor reject). Clamps
            # the grown pick to current held size (keeping is exempt) so the
            # cycle validates instead of stranding skipped:invalid. Runs on
            # the liquid-clamped dict; validator still gates the result.
            sf_dict, sf_clamped, sf_note = _clamp_subfloor_nonstable_growth(
                decision.model_dump(), snap
            )
            if sf_clamped:
                notes_list = sf_dict.setdefault("notes", [])
                if sf_note:
                    notes_list.append(sf_note)
                log.warning(
                    "subfloor clamp: LLM grew sub-floor non-stable picks %s "
                    "past the funding floor — clamped to current size + cash",
                    sorted(set(sf_clamped)),
                )
                decision = Decision.model_validate(sf_dict)
                outcome["subfloor_clamp_clamped"] = sorted(set(sf_clamped))

            # Confidence recompute (`agent-yield-quality.4`) — deterministic,
            # LLM-path only. The LLM anchors confidence on a fixed 0.65 (one
            # notch above the 0.60 execute gate); recompute it from this
            # cycle's data quality so a thin/risky cycle can't auto-execute on
            # a hand-picked number. Runs after every clamp so the budget-
            # starved signal (liquid clamp dropped NEW picks) is visible, and
            # BEFORE write_decision/validate/the conf gate so the recomputed
            # value is what gets persisted, validated and gated.
            conf_input = decision.model_dump()
            # Carry the budget-starved signal into the pure recompute without
            # widening its signature (Decision drops `extra` on re-validate).
            conf_input["_outcome_liquid_clamp_dropped"] = outcome.get(
                "liquid_clamp_dropped"
            )
            new_conf, conf_reasons = _recompute_confidence(conf_input, snap, priors)
            if abs(new_conf - decision.confidence) > 1e-9:
                from_conf = float(decision.confidence)
                rebuilt = decision.model_dump()
                rebuilt["confidence"] = new_conf
                rebuilt.setdefault("notes", []).append(
                    f"confidence_recompute {from_conf:.2f}→{new_conf:.2f}: "
                    f"{'; '.join(conf_reasons) or 'no signals'}"
                )
                decision = Decision.model_validate(rebuilt)
                outcome["confidence_recomputed"] = {
                    "from": from_conf,
                    "to": new_conf,
                    "reasons": conf_reasons,
                }
                log.info(
                    "confidence recompute: %.2f → %.2f (%s)",
                    from_conf, new_conf, "; ".join(conf_reasons) or "no signals",
                )

            # Anchor-streak telemetry (`agent-yield-quality.4`) — pure, never
            # blocks. Load a deeper slice JUST for this check (the global
            # prior-depth default stays at 3) so a 0.65-anchor run of 5 cycles
            # is flagged for the operator/dashboard.
            anchor_priors = _load_recent_prior_decisions(n=CONF_ANCHOR_STREAK_N)
            anchor_msg = _confidence_anchor_warning(
                float(decision.confidence), anchor_priors
            )
            if anchor_msg:
                outcome["confidence_anchor_warning"] = anchor_msg
                log.warning("%s", anchor_msg)

            # Deterministic expected_blended_apr_pct (`agent-yield-quality.5`).
            # The headline feeds DecisionLog + IPFS; recompute it from the
            # snapshot's per-pick (net-of-hedge for non-stables) APR instead of
            # trusting the LLM's hand-computed number. Same `_snapshot_index`
            # lookup; runs before write_decision so the persisted/anchored
            # headline is the deterministic blend.
            new_apr, apr_breakdown = _recompute_expected_apr(
                decision.model_dump(), snap
            )
            if abs(new_apr - decision.expected_blended_apr_pct) > 1e-6:
                from_apr = float(decision.expected_blended_apr_pct)
                rebuilt = decision.model_dump()
                rebuilt["expected_blended_apr_pct"] = new_apr
                rebuilt.setdefault("notes", []).append(
                    f"expected_apr_recompute {from_apr:.2f}%→{new_apr:.2f}% "
                    f"(net-of-hedge blend of snapshot APRs)"
                )
                decision = Decision.model_validate(rebuilt)
                outcome["expected_apr_recomputed"] = {
                    "from": from_apr,
                    "to": new_apr,
                }
                log.info(
                    "expected APR recompute: %.2f%% → %.2f%% (%d picks)",
                    from_apr, new_apr, len(apr_breakdown),
                )

        # carry_liq_close stop-loss (loop-1/wt-2): drop the near-liq carry from
        # the decision BEFORE persist/validate/diff. Common to BOTH the LLM and
        # auto-close paths — a carry has no pick to drop in the auto-close
        # rewrite, so without this a `carry_liq_close` co-occurring with a
        # `pick_invalidated` is swallowed (auto-close decision is valid +
        # confidence 1.0 → the de-risk sweep's `not (ok and conf_ok)` gate skips
        # the close, leaving the short bleeding to liquidation). Dropping the
        # target makes the diff CLOSE it on the executing path with no re-open;
        # the standalone sweep below still covers the dry-run / skipped path.
        near_liq_carry_coins = {
            (e.get("coin") or "").upper()
            for e in (wake_events or [])
            if e.get("kind") == "carry_liq_close" and e.get("coin")
        }
        if near_liq_carry_coins:
            decision, carry_dropped = _strip_carry_coins_from_decision(
                decision, snap, near_liq_carry_coins
            )
            if carry_dropped:
                outcome["carry_liq_close_dropped"] = sorted(set(carry_dropped))
                log.warning(
                    "carry liq-close: dropped near-liq carry %s from the "
                    "decision so the diff closes (not re-opens) it",
                    sorted(near_liq_carry_coins),
                )

        decision_path = write_decision(
            decision,
            snap_path,
            wake_events=wake_events,
            usage=usage,
        )
        outcome["decision_filename"] = decision_path.name
        outcome["confidence"] = float(decision.confidence)
        outcome["expected_apr_pct"] = float(decision.expected_blended_apr_pct)
        outcome["stages"].append("decide")

        # 3. Validate. The watcher's danger-exit (auto_close) path skips the
        # min-hold gate so a peg break / crash / funding flip can unwind a
        # young position — "don't lose principal" overrides "hold to recoup".
        ok, errors = validate(
            decision, snap, allow_exits=bool(outcome.get("auto_close"))
        )
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

        # 3b. Safety de-risk sweep (2026-06-08). Winding down NAKED non-stable
        # spot (e.g. LM/LP-redeem principal stranded in FUND — live: ~17 TIA)
        # is pure risk reduction and must NOT be gated behind a valid,
        # confidence>=floor allocation. The orphan-sell normally rides inside
        # `diff_to_actions`, which only executes live on an approved cycle — so
        # while the agent ran a string of sub-0.60 / skipped:invalid cycles the
        # naked TIA was never sold, contradicting the controlled-risk mandate.
        # When the full allocation cycle WON'T execute live (validator reject
        # OR confidence < floor), run the orphan sweep on its own so naked
        # exposure is still de-risked. It reads only wallet/perp state (not the
        # rejected decision), keeps the delta-neutral guard (never sells a
        # hedge leg), and appends to the same cycle execution log. On a
        # full-live cycle `diff_to_actions` handles these sells, so skip here
        # to avoid double execution.
        conf_ok = decision.confidence >= min_confidence
        if live and not (ok and conf_ok):
            # Close orphan perp shorts (underlying Earn/LM redeemed → naked
            # short bleeding funding) FIRST, then sell the spot they no
            # longer back. Both are pure risk reduction and must run even
            # when the full allocation won't (else the normal hedge-diff
            # close stays gated to dry-run and dust never clears).
            carry_state = read_carry_state()
            carry_coins = carry_state.active_coins()
            # Durable cooldown so a settling LP isn't re-redeemed every
            # non-executing cycle (wt-3); positions emitted within the window
            # are skipped, settled/expired entries pruned after execute.
            lm_redeem_cd = read_lm_redeem_cooldown()
            lm_redeem_blocked = lm_redeem_cd.blocked_position_ids(snap.captured_at)
            perp_closes = _orphan_perp_close_actions(
                snap, snap_path.stem, idx_offset=780, carry_coins=carry_coins
            )
            # Reconcile over-hedged Earn coins (short > earn with orphan spot)
            # even on non-executing cycles — that's exactly when a trapped orphan
            # (failed subscribe) would otherwise persist for hours.
            reconcile_actions, reconciled_coins = _reconcile_hedge_to_earn_actions(
                snap, [], [], perp_closes, [], snap_path.stem,
                idx_offset=760, carry_coins=carry_coins,
            )
            reconciled_frozen = frozenset(reconciled_coins)
            # Plus: force-redeem any held LM whose naked base residual sits
            # above the floor. It lives INSIDE the LP so the spot/perp sweep
            # above can't reach it — only the LP redeem can — and a redeem
            # cycle scores below the 0.60 gate (mostly-cash book), so without
            # this it would never execute (observed live 2026-06-09).
            # Plus: close any funding-carry whose short is nearing liquidation
            # (watcher `carry_liq_close` events). On an EXECUTING cycle the
            # decision-strip above already routes this through the diff's CLOSE;
            # here it's the only deterministic exit on a non-executing cycle
            # (diff is dry-run). `near_liq_carry_coins` was resolved before
            # write_decision. Intersected with active carry coins inside the
            # helper, so a manual naked short (no carry record) is ignored.
            lm_residual = _lm_residual_redeem_actions(
                snap, snap_path.stem, idx_offset=820,
                blocked_position_ids=lm_redeem_blocked,
            )
            sweep = reconcile_actions + perp_closes + _orphan_spot_sell_actions(
                snap, [], [], perp_closes, [], snap_path.stem, idx_offset=800,
                reconciled_coins=reconciled_frozen,
            ) + lm_residual + _carry_liq_close_actions(
                snap, carry_state, near_liq_carry_coins, snap_path.stem,
                idx_offset=840,
            )
            # Final reindex over the COMBINED sweep (executor-3): the segment
            # helpers above use hand-tuned 20-slot idx_offset windows that
            # collide if any segment overruns its slot. Reassigns sequential
            # ids + rewrites carry-leg `extra` spot/perp ids (executor-4),
            # same pass the main diff runs. Safe to renumber from 0: the sweep
            # runs only when the diff is dry-run, so no executed-id collision.
            _reindex_order_link_ids(sweep, snap_path.stem)
            if sweep:
                # ah.24: through the SAME durable envelope as the main diff —
                # pending-intent persist (crash recovery) + carry-state
                # reconcile + clear — instead of a raw parallel dispatch. The
                # sweep is always live here; its confirmable carry-close legs
                # are now SIGKILL-recoverable like the diff's.
                sweep_results = await _execute_with_recovery(
                    bybit_client, sweep, snap_path.stem, carry_state, outcome,
                    context="de-risk sweep", dry_run=False,
                )
                outcome["safety_sweep"] = [
                    {
                        "product_id": r.action.product_id,
                        "coin": r.action.coin,
                        "amount": str(r.action.amount),
                        "status": r.status,
                        "error": r.error,
                    }
                    for r in sweep_results
                ]
                swept_ok = sum(1 for r in sweep_results if r.status == "ok")
                log.warning(
                    "safety de-risk sweep: executed %d/%d orphan non-stable "
                    "sell(s) on a non-executing cycle (validator_ok=%s "
                    "confidence=%.2f < floor %.2f)",
                    swept_ok, len(sweep), ok,
                    float(decision.confidence), float(min_confidence),
                )
                # Stamp the cooldown for every residual-redeem we emitted (wt-3)
                # so the settling LP isn't re-redeemed next cycle; prune
                # settled/expired entries to keep the file bounded.
                emitted = {a.position_id for a in lm_residual if a.position_id}
                if emitted or lm_redeem_cd.entries:
                    live_lm = {
                        str(p.get("positionId") or "")
                        for p in (snap.lm_positions or [])
                    }
                    write_lm_redeem_cooldown(
                        lm_redeem_cd.record(emitted, snap.captured_at).prune(
                            live_lm, snap.captured_at
                        )
                    )

        if not ok:
            outcome["result"] = "skipped:invalid"
            return outcome

        # 4. Diff → actions. Pre-load funding-carry state so the diff
        # layer (a) sees existing carry positions and skips them in
        # the Earn-hedge reconciliation, and (b) emits CLOSE actions
        # for state-only coins (`bybit-strategy-expansion.5`).
        carry_state = read_carry_state()
        snapshot_ts = snap_path.stem
        actions = diff_to_actions(
            snap, decision, snapshot_ts, carry_state=carry_state
        )
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

        # 6. Execute — wrapped in try/finally so the carry-state write
        # runs even if the post-execute accounting (or any later code
        # in this cycle) raises. Without this guard, a crash between
        # `execute_actions` returning and the explicit carry-state
        # write below would leave a real Bybit carry position open
        # with no state record — next cycle's hedge layer would then
        # see an orphan spot+perp pair and likely mis-classify it.
        # Hard process crashes (OOM, SIGKILL) bypass this finally — that
        # window is covered by the pending-intent marker written just below,
        # which the next startup's crash-recovery gate verifies against Bybit.
        # ah.24: the durable execute envelope (pending-intent persist BEFORE
        # dispatch for SIGKILL recovery + carry-state reconcile in a finally +
        # marker clear once durable) is shared with the de-risk sweep via
        # `_execute_with_recovery`. An `execute_actions` exception propagates
        # out of the helper (reconcile still runs in its finally; the marker
        # survives for the next startup), skipping the accounting below.
        results = await _execute_with_recovery(
            bybit_client, actions, snapshot_ts, carry_state, outcome,
            context="execute", dry_run=effective_dry_run,
        )
        outcome["actions_executed"] = sum(
            1 for r in results if r.status == "ok"
        )
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
        if effective_dry_run:
            outcome["result"] = "ok"
        else:
            # `.42`: surface partial completion so the next-cycle startup scan
            # + post-mortem analyzer can distinguish a clean batch from one
            # where some actions errored mid-cycle (Bybit transient 5xx,
            # atomic-pair guard fired, retCode rejected, etc.). Pre-fix the
            # field said "executed" regardless and operator only knew via
            # actions_executed < actions_planned in the entry.
            failed = sum(
                1 for r in results
                if r.status in ("error", "orphan")
            )
            if failed > 0:
                outcome["result"] = "executed_partial"
                outcome["actions_failed"] = failed
            else:
                outcome["result"] = "executed"
        # ah.9 (state-3): an orphan OPEN_FUNDING_CARRY = the spot filled but
        # the perp leg never landed. The dispatch atomically unwinds the
        # spot; surface it P0 so the operator is alerted — especially the
        # rare case where the unwind ITSELF failed (genuinely naked spot,
        # swept next cycle by the orphan-seller). NOT a HALT: a HALT would
        # freeze that very sweep.
        carry_orphans = [
            r for r in results
            if r.action.kind.value == "open_funding_carry"
            and r.status == "orphan"
        ]
        if carry_orphans:
            details = []
            for r in carry_orphans:
                unwind = (r.response or {}).get("legs", {}).get("unwind") or {}
                details.append({
                    "coin": r.action.coin,
                    "error": r.error,
                    "unwound": unwind.get("unwound"),
                    "unwind_error": unwind.get("error"),
                })
                if unwind.get("unwound"):
                    log.error(
                        "P0 carry OPEN orphan (%s): perp leg failed, spot "
                        "UNWOUND (no naked exposure) — operator review: %s",
                        r.action.coin, r.error,
                    )
                else:
                    log.error(
                        "P0 carry OPEN orphan (%s): perp leg failed AND spot "
                        "unwind failed (%s) — NAKED spot long; orphan-seller "
                        "sweeps next cycle — operator review: %s",
                        r.action.coin, unwind.get("error"), r.error,
                    )
            outcome["carry_open_orphan"] = details

        # 6a. Post-execute baseline prune (watcher-2). The baseline was
        # refreshed at step 1a from the START-of-cycle snapshot, so the
        # positions this cycle just closed/redeemed still sit in it — and the
        # watcher (polling every ~120s) would keep evaluating them against live
        # funding and fire spurious `pick_invalidated` events until the next
        # cycle. Drop them now, by what we intended to close (a re-snapshot
        # wouldn't help: a slow OnChain redeem still reads as held). Best-effort
        # + log: a prune failure only leaves the staleness step 1a already had.
        if not effective_dry_run and results:
            try:
                _bl_path = watcher_baseline_path or WATCHER_BASELINE_PATH
                _bl = read_watcher_baseline(_bl_path)
                if _bl is not None:
                    _pruned = prune_closed_positions(_bl, results)
                    if _pruned is not _bl:  # no-op prune returns the same object
                        write_watcher_baseline(_pruned, _bl_path)
            except Exception as e:  # noqa: BLE001 — best effort
                log.warning("post-execute baseline prune failed: %s", e)

        # 6a2. Record hedged-Earn exit intents. Each non-stable REDEEM_EARN
        # this cycle frees a coin that lands only after settlement (OnChain
        # ~4d). Persist the intent so (a) the watcher can fire an
        # `earn_redeem_settled` event the moment it arrives — not the next 4h
        # heartbeat — and (b) the exit swaps EXACTLY the redeemed amount + closes
        # the paired short, deterministically. The intent is a precise/fast
        # path; the orphan re-derivation sweep stays the idempotent backstop, so
        # a write failure here only reverts to heartbeat-speed cleanup.
        if not effective_dry_run and results:
            try:
                new_intents = build_redeem_exit_intents(snap, results)
                if new_intents:
                    state = read_redeem_intents()
                    for it in new_intents:
                        state = state.upsert(it)
                    write_redeem_intents(state)
            except Exception as e:  # noqa: BLE001 — best effort
                log.warning("redeem-exit intent record failed: %s", e)

        # 6b. Reflection BEFORE anchoring (executed path). `_anchor_onchain`
        # builds the IPFS pin by re-reading the decision file, so writing the
        # human note here means the pinned (and on-chain-referenced) rationale
        # embeds it alongside the structured thesis. Held / skipped:invalid
        # cycles return earlier and never pin — the run_loop backstop attaches
        # their reflection for the store/web. Best-effort + idempotent: the
        # loop-level call then no-ops because the note is already present.
        try:
            await _attach_reflection(outcome, anthropic_client)
        except Exception as e:  # noqa: BLE001 — reflection is best-effort
            log.warning("reflection generation failed (non-fatal): %s", e)

        # 7. Anchor on-chain — best-effort. The decision file + Postgres
        # row remain the source of truth; the on-chain log is the public
        # audit trail (DecisionLog.recordDecision + ReputationOracle
        # heartbeat). Failure here MUST NOT abort the cycle: any RPC
        # blip, gas spike, or revert just warns and moves on.
        try:
            await _anchor_onchain(decision, outcome)
        except Exception as e:  # noqa: BLE001
            outcome.setdefault("onchain_error", f"{type(e).__name__}: {e}")
            log.warning("on-chain anchoring failed (non-fatal): %s", e)
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
    run_on_start: bool = True,
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

        # `.42` + ah.7 startup crash-recovery gate: a prior cycle may have
        # crashed (OOM / SIGKILL) between placing an order and recording it.
        # Cross-check unfinished cycles AND any surviving pending-intent marker
        # against live Bybit order-history; HALT (cycles then no-op) if a real
        # or unconfirmable order may be open that was never reconciled, so the
        # operator fixes it before the next diff opens new positions. Read-only
        # — never auto-replays (a blind replay inside Bybit's dedup window
        # could double-spend a response that landed just before the crash).
        await _startup_crash_recovery_gate(cycle_log_path, bybit_client, live=live)

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

        # `--no-startup-cycle`: on a restart (e.g. a redeploy) don't fire an
        # off-schedule heartbeat. Resume the existing cadence — wait until
        # `last_cycle + interval` is due before the first heartbeat. The
        # watcher still wakes us for P0 events during the wait; a first-ever
        # boot or an overdue schedule runs immediately (`_seconds_until_due`
        # returns 0). `--once` always runs now (manual smoke/invocation).
        if not once and not run_on_start and not stop_event.is_set():
            due_in = _seconds_until_due(cycle_log_path, interval_seconds)
            if due_in > 0 and not pending_events:
                log.info(
                    "no-startup-cycle: last cycle recent — waiting %.0fs "
                    "until the schedule is due (watcher events still wake)",
                    due_in,
                )
                await _sleep_until_next_cycle(
                    interval_seconds=due_in,
                    stop_event=stop_event,
                    wake_event=wake_event,
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
                # Reflection backstop. The executed path already attached its
                # note inside run_one_cycle (so the IPFS pin embeds it); this
                # covers the cycles that returned before the pin — held
                # (no_actions), skipped:invalid, errored — so every recorded
                # cycle carries a diary note into the store/web. Idempotent:
                # no-ops when the note is already present. Best-effort.
                try:
                    await _attach_reflection(outcome, anthropic_client)
                except Exception as e:  # noqa: BLE001 — reflection is best-effort
                    log.warning("reflection generation failed (non-fatal): %s", e)
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


def _last_cycle_time(cycle_log_path: Path) -> datetime | None:
    """Timestamp of the most recent recorded cycle — prefer `finished_at`,
    fall back to `started_at`. None when the log is missing / empty /
    unparseable. The cycle log lives on a persistent volume, so this
    survives container restarts and lets `--no-startup-cycle` resume the
    existing cadence across a redeploy."""
    if not cycle_log_path.is_file():
        return None
    last_ts: str | None = None
    for raw in cycle_log_path.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            outcome = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts = outcome.get("finished_at") or outcome.get("started_at")
        if ts:
            last_ts = ts
    if not last_ts:
        return None
    try:
        dt = datetime.fromisoformat(last_ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _seconds_until_due(
    cycle_log_path: Path,
    interval_seconds: float,
    *,
    now: datetime | None = None,
) -> float:
    """Seconds remaining until the heartbeat schedule is next due, based on
    the last recorded cycle (`last + interval`). 0.0 when there is no
    history (first-ever boot → run now) or the schedule is already overdue
    (catch up immediately)."""
    last = _last_cycle_time(cycle_log_path)
    if last is None:
        return 0.0
    now = now or datetime.now(UTC)
    elapsed = (now - last).total_seconds()
    return max(0.0, interval_seconds - elapsed)


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
        "--no-startup-cycle",
        dest="run_on_start",
        action="store_false",
        default=True,
        help=(
            "On startup, don't fire an immediate heartbeat cycle — resume "
            "the existing cadence instead (wait until last_cycle + "
            "--interval is due). Use in the long-running service so a "
            "redeploy/restart doesn't run an off-schedule cycle. The "
            "watcher still wakes on P0 events; an overdue or first-ever "
            "schedule runs immediately. Ignored with --once."
        ),
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
            run_on_start=args.run_on_start,
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
