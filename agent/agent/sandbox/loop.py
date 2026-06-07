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
from agent.reason.venues import VENUE_REGISTRY
from agent.sandbox.decide import (
    DECISION_DIR,
    _collect_recently_invalidated,
    _load_recent_prior_decisions,
    decide,
    write_decision,
)
from agent.sandbox.ipfs_pin import pin_decision_rationale
from agent.sandbox.onchain_writer import OnchainWriter
from agent.sandbox.carry_state import (
    DEFAULT_CARRY_STATE_PATH,
    read_carry_state,
    write_carry_state,
)
from agent.sandbox.execute import (
    DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE,
    EXECUTIONS_DIR,
    apply_carry_results_to_state,
    diff_to_actions,
    execute_actions,
    reconcile_executions,
    request_approval,
)
from agent.sandbox.safety import (
    check_daily_drawdown,
    halt,
    is_halted,
    record_equity,
)
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
from agent.validate.rules import _held_usd_by_product, validate

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

    # Anchor ACTUAL execution, not just intent. `outcome` already carries
    # the per-action results (status) by the time we're called (post-
    # execute). The IPFS payload embeds an `_execution` block (the public
    # audit trail), and `actionHash` commits to the executed ledger for
    # live cycles — so a partial failure can't masquerade on-chain as a
    # fully-executed allocation. Dry-run / hold (no_actions) fall back to
    # the intent hash (intent == execution there).
    result = outcome.get("result")
    execution_block: dict[str, Any] = {
        "result": result,
        "actions_executed": outcome.get("actions_executed"),
        "actions_failed": outcome.get("actions_failed"),
        "actions": outcome.get("actions") or [],
    }
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

    tx_hash = await asyncio.to_thread(
        writer.record_decision,
        decision_dict,
        snap_name,
        ipfs_cid=ipfs_cid or "",
        executed_actions=executed_actions,
    )
    if tx_hash:
        anchor["decision_tx"] = tx_hash
        log.info("decision anchored on-chain: tx=%s cid=%s", tx_hash, ipfs_cid or "(none)")

    rep_tx = await asyncio.to_thread(writer.update_reputation)
    if rep_tx:
        anchor["reputation_tx"] = rep_tx
        log.info("reputation updated on-chain: tx=%s", rep_tx)

    if anchor:
        outcome["onchain"] = anchor
        outcome.setdefault("stages", []).append("anchor_onchain")


def _drop_picks_into_cash(
    decision_dict: dict[str, Any],
    blocked_pids: set[str],
) -> tuple[dict[str, Any], list[str]]:
    """Remove every pick whose `product_id` is in `blocked_pids` and roll
    its weight into `cash_usdc`. Returns `(new_decision_dict, dropped)`
    where `dropped` is the list of pids that were actually removed (so
    the caller can log + add notes).

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
        kept = []
        for p in picks:
            pid = str(p.get("product_id", ""))
            if pid in blocked_pids:
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


# Venues whose NEW picks draw the liquid stable pool (stable subscribes +
# hedged non-stable spot/margin). LM / advance-Earn / carry are funded /
# gated separately, so the liquid clamp leaves them to the validator.
_LIQUID_CLAMP_VENUES = ("bybit_flex", "bybit_onchain")
# Mirror of the executor's MIN_ACTION_USDC / validator _MIN_ACTION_USDC —
# a delta below this is a no-op the diff never acts on.
_MIN_NEW_ACTION_USD = 0.50


def _clamp_to_liquid_budget(
    decision_dict: dict[str, Any],
    snapshot: Snapshot,
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
    MIN) are never touched — only fresh/grown picks. Per-category, with
    the shared-pool looseness documented on `check_stable_earn_funding`;
    this is an upstream nudge, the validator is the hard gate.
    """
    wallet = snapshot.wallet
    total_book = float(wallet.total_equity_usd)
    liquid_stables = float(wallet.liquid_stables_usd)
    # No liquidity signal (pre-pivot fixtures / legacy collector) → no-op,
    # mirroring the validator's supply<=0 fall-through. Prod always
    # populates the liquid fields.
    if total_book <= 0 or liquid_stables <= 0:
        return decision_dict, [], None
    max_nonstable = float(wallet.max_new_nonstable_usd)

    held = _held_usd_by_product(snapshot)
    prod_coin: dict[tuple[str, str], str] = {}
    for venue_id in _LIQUID_CLAMP_VENUES:
        meta = VENUE_REGISTRY.get(venue_id)
        cat = getattr(meta, "snapshot_category", None) if meta else None
        if not cat:
            continue
        for p in snapshot.products.get(cat, []):
            prod_coin[(cat, p.product_id)] = p.coin.upper()

    new_stable: list[tuple[str, float]] = []
    new_nonstable: list[tuple[str, float]] = []
    for v in decision_dict.get("venues", []) or []:
        vid = v.get("venue_id")
        if vid not in _LIQUID_CLAMP_VENUES:
            continue
        meta = VENUE_REGISTRY.get(vid)
        cat = getattr(meta, "snapshot_category", None) if meta else None
        if not cat:
            continue
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
                new_stable.append((pid, net_new))
            else:
                new_nonstable.append((pid, net_new))

    to_drop: set[str] = set()

    def _select(picks: list[tuple[str, float]], budget: float) -> None:
        total = sum(n for _, n in picks)
        if total <= budget + 1e-9:
            return
        # Drop largest-first so the fewest picks are sacrificed.
        for pid, n in sorted(picks, key=lambda x: x[1], reverse=True):
            if total <= budget + 1e-9:
                break
            to_drop.add(pid)
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

    # 0. Halt check — operator-controlled kill switch + auto-tripped on
    # state-coherence failures (carry_state write errors trip this so the
    # next cycle can't double-position on a stale state file). Checked
    # BEFORE snapshot to avoid an unnecessary API round-trip when the
    # agent is already meant to be off.
    halted, halt_reason = is_halted()
    if halted:
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
        drawdown_hit, drawdown_reason = check_daily_drawdown(current_equity)
        if drawdown_hit and drawdown_reason is not None:
            halt(drawdown_reason)
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
        auto_close = _build_auto_close_decision(latest_prior, wake_events)
        usage = None  # set only on the LLM path; auto-close skips Anthropic
        if auto_close is not None:
            log.info(
                "auto-close path: pick_invalidated event(s) — "
                "skipping LLM, deterministic close"
            )
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
            cooldown = _collect_recently_invalidated(priors or [])
            if cooldown:
                blocked_pids = set(cooldown.keys())
                filtered_dict, dropped = _drop_picks_into_cash(
                    decision.model_dump(), blocked_pids
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
                decision.model_dump(), snap
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
        # Hard process crashes (OOM, SIGKILL) still bypass this; for
        # truly transactional state we'd need per-action writes inside
        # `execute_actions`, deferred for now.
        results: list = []
        try:
            results = await execute_actions(
                bybit_client,
                actions,
                snapshot_ts=snapshot_ts,
                dry_run=effective_dry_run,
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
                # `.42`: surface partial completion so the next-cycle
                # startup scan + post-mortem analyzer can distinguish a
                # clean batch from one where some actions errored mid-
                # cycle (Bybit transient 5xx, atomic-pair guard fired,
                # retCode rejected, etc.). Pre-fix the field said
                # "executed" regardless and operator only knew via
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
        finally:
            # Roll forward carry state from the dispatch results (`.5`).
            # Skipped on dry-run so a `--live` run is required to mutate
            # the persisted positions ledger. Empty `results` (execute
            # itself raised before producing anything) → nothing to roll
            # forward.
            if not effective_dry_run and results:
                try:
                    new_state = apply_carry_results_to_state(
                        carry_state, results
                    )
                    if new_state.positions != carry_state.positions:
                        write_carry_state(new_state)
                        outcome.setdefault("stages", []).append(
                            "carry_state_updated"
                        )
                except Exception as e:  # noqa: BLE001
                    # Real Bybit position may now be open while the
                    # state file is stale — next cycle could re-emit
                    # OPEN and double-position. Trip the HALT marker
                    # so the operator MUST manually reconcile before
                    # the agent runs again.
                    outcome["carry_state_error"] = f"{type(e).__name__}: {e}"
                    halt(
                        f"carry_state write failed after execute "
                        f"({type(e).__name__}: {e}) — manual "
                        f"reconciliation required before resume"
                    )
                    log.exception(
                        "carry_state update failed — HALT created"
                    )

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

        # `.42` startup scan: a prior cycle may have crashed between
        # writing per-action execution lines and writing the cycle
        # outcome (systemd OOM / SIGKILL). Surface those cycles in the
        # operator log so manual reconciliation can happen before the
        # new cycle starts opening positions. Read-only — does NOT
        # replay; auto-replay would risk duplicating already-executed
        # actions whose response landed before the crash.
        unfinished = detect_unfinished_cycles(cycle_log_path)
        for u in unfinished:
            log.warning(
                "unfinished prior cycle detected (no cycle_log entry): "
                "ts=%s total=%d counts=%s last_finished=%s — "
                "review %s before next cycle's diff opens new positions",
                u["snapshot_ts"],
                u["total"],
                u["counts"],
                u.get("last_finished_at"),
                u["path"],
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
