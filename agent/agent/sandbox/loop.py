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
import json
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import BybitClient
from agent.sandbox.decide import (
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
from agent.sandbox.snapshot import collect_snapshot, write_snapshot
from agent.validate.rules import validate

# Default cycle cadence. Was 4h while LM was leverage=1 only; leveraged
# LM (`.47` follow-up 2026-05-29) can liquidate in minutes during a fast
# move, so the safe default tightens to 30 min. Operator can still
# override via `--interval` for a slower heartbeat when nothing leveraged
# is on the book.
DEFAULT_INTERVAL_SECONDS = 30 * 60  # 30 min
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


async def run_one_cycle(
    bybit_client: BybitClient,
    anthropic_client: anthropic.AsyncAnthropic,
    *,
    live: bool,
    yes: bool,
    min_confidence: float,
    mantle_rpc_url: str | None = None,
    mantle_vault_address: str | None = None,
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

        # 2. Decide
        raw_snapshot = json.loads(snap_path.read_text())
        prior = _load_latest_prior_decision()
        decision = await decide(
            raw_snapshot, client=anthropic_client, prior_decision=prior
        )
        decision_path = write_decision(decision, snap_path)
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
    oracle_cfg: "OracleSettings | None" = None,
) -> None:
    """Run cycles indefinitely (or once) at `interval_seconds` apart.

    The wait between cycles is cancellable: when the `stop_event` fires
    (set by SIGINT / SIGTERM in `_install_signal_handlers`), the current
    sleep wakes immediately and the loop exits at the top of the next
    iteration. In `--once` mode the interval is irrelevant — single
    cycle then return.
    """
    stop_event = stop_event or asyncio.Event()
    _install_signal_handlers(stop_event)
    cycle_log_path.parent.mkdir(parents=True, exist_ok=True)

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

        while not stop_event.is_set():
            log.info(
                "starting cycle (live=%s, yes=%s, min_confidence=%.2f)",
                live, yes, min_confidence,
            )
            outcome = await run_one_cycle(
                bybit_client,
                anthropic_client,
                live=live,
                yes=yes,
                min_confidence=min_confidence,
                mantle_rpc_url=mantle_rpc_url,
                mantle_vault_address=mantle_vault_address,
            )
            with cycle_log_path.open("a") as f:
                f.write(json.dumps(outcome) + "\n")
            log.info("cycle result: %s", outcome.get("result"))
            if once or stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=interval_seconds
                )
            except asyncio.TimeoutError:
                pass  # next cycle


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
            "= 4h heartbeat)."
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
        )
    )


if __name__ == "__main__":
    _main()
