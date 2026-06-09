"""Auto-extracted submodule (ah.25 execute split). See package __init__."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import (
    BybitClient,
)
from agent.reason.schema import Decision
from agent.sandbox.execute.common import (
    DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE,
    EXECUTIONS_DIR,
)
from agent.sandbox.execute.dispatch import (
    execute_actions,
)
from agent.sandbox.execute.planner import (
    diff_to_actions,
)
from agent.sandbox.execute.types import (
    Action,
    ActionKind,
)
from agent.sandbox.snapshot import (
    SNAPSHOT_DIR,
    Snapshot,
)


def _load_paired_snapshot(decision_path: Path) -> tuple[Snapshot, dict[str, Any], str]:
    """Read the decision JSON, locate its paired snapshot via the
    `_meta.snapshot_filename` sidecar that `write_decision` writes, and
    parse the snapshot. Returns `(snapshot, raw_decision_dict, snapshot_ts)`.
    """
    raw_decision = json.loads(decision_path.read_text())
    meta = raw_decision.get("_meta") or {}
    snap_name = meta.get("snapshot_filename")
    if not snap_name:
        raise RuntimeError(
            f"decision {decision_path} has no _meta.snapshot_filename"
        )
    snap_path = Path(SNAPSHOT_DIR) / snap_name
    if not snap_path.is_file():
        raise RuntimeError(f"paired snapshot not found: {snap_path}")
    raw_snapshot = json.loads(snap_path.read_text())
    snap = Snapshot.model_validate(raw_snapshot)
    ts = snap_path.stem  # `<UTC ts>` without `.json`
    return snap, raw_decision, ts


def request_approval(
    decision: Decision,
    actions: list[Action],
    *,
    yes: bool,
    min_confidence: float,
    stdin: Any = None,
    input_fn: Any = None,
) -> bool:
    """Return True if the operator (or the auto-approve guard) signs off
    on live execution. `.12` approval gate.

    Three paths:
    1. `--yes` flag AND `decision.confidence >= min_confidence` → auto-approve
       (intended for the loop driver `.13` once a few cycles have run
       interactively and the operator trusts the model).
    2. Interactive terminal (`stdin.isatty()`) → prompt `y/N`; anything
       other than `y` / `yes` aborts.
    3. Non-interactive stdin + no `--yes` → refuse. This is the safety
       valve: a cron / CI invocation can't accidentally place orders
       without an explicit blanket approval.

    The `stdin` arg is the injection seam for tests; production passes
    `None` (defaults to `sys.stdin`).
    """
    stdin = stdin if stdin is not None else sys.stdin
    prompt = input_fn if input_fn is not None else input

    plan_summary = _render_plan_summary(actions)
    print()
    print("=== APPROVAL REQUIRED (live execution) ===")
    print(
        f"confidence={decision.confidence:.2f}  "
        f"expected_apr={decision.expected_blended_apr_pct:.2f}%  "
        f"risk_flags={decision.risk_flags}"
    )
    print(plan_summary)

    if yes:
        if decision.confidence >= min_confidence:
            print(
                f"--yes accepted (confidence {decision.confidence:.2f} "
                f">= min {min_confidence:.2f}). Proceeding."
            )
            return True
        print(
            f"--yes ignored: confidence {decision.confidence:.2f} "
            f"below auto-approve floor {min_confidence:.2f}. "
            "Falling back to interactive prompt."
        )

    if not stdin.isatty():
        print(
            "stdin is not a TTY and --yes is not active (or confidence "
            "below floor). Refusing to execute — abort.",
            file=sys.stderr,
        )
        return False

    try:
        resp = prompt("Execute live? [y/N] ").strip().lower()
    except EOFError:
        return False
    return resp in ("y", "yes", "д", "да")


def _render_plan_summary(actions: list[Action]) -> str:
    """Group actions by kind for a human-readable diff to approve."""
    lines: list[str] = []
    by_kind: dict[ActionKind, list[Action]] = {}
    for a in actions:
        by_kind.setdefault(a.kind, []).append(a)
    for kind in (
        ActionKind.REDEEM_EARN,
        ActionKind.REDEEM_LM,
        ActionKind.CLAIM_LM,
        ActionKind.ALPHA_REDEEM,
        ActionKind.CLOSE_PERP,
        ActionKind.SWAP_SPOT,
        ActionKind.OPEN_PERP_SHORT,
        ActionKind.SUBSCRIBE_EARN,
        ActionKind.SUBSCRIBE_ADVANCE_EARN,
        ActionKind.SUBSCRIBE_LM,
        ActionKind.ALPHA_PURCHASE,
        ActionKind.SKIP_OUT_OF_SCOPE,
    ):
        rows = by_kind.get(kind, [])
        if not rows:
            continue
        lines.append(f"  {kind.value} ({len(rows)}):")
        for a in rows:
            lines.append(
                f"    - {a.category}/{a.product_id} {a.coin} "
                f"${a.amount:.2f}"
            )
    return "\n".join(lines)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Execute a sandbox decision against Bybit Earn.")
    parser.add_argument(
        "--decision",
        type=Path,
        required=True,
        help="Path to a decision JSON written by agent.sandbox.decide",
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
            "Skip the interactive y/N approval prompt when running --live, "
            "provided decision.confidence >= --min-confidence. For "
            "scripted / cron use after a few interactive cycles."
        ),
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE,
        help=(
            f"Auto-approve floor for --yes (default "
            f"{DEFAULT_AUTO_APPROVE_MIN_CONFIDENCE}). Below this, --yes "
            "is ignored and the interactive prompt runs instead."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="dotenv to load (e.g. .env at repo root)",
    )
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(args.env_file, override=True)

    snap, raw_decision, snapshot_ts = _load_paired_snapshot(args.decision)
    # Reuse Decision from the raw — sandbox/decide wrote a pydantic-
    # validated decision plus _meta + optional _validator, so this is
    # round-trippable.
    decision_payload = {
        k: v for k, v in raw_decision.items() if not k.startswith("_")
    }
    decision = Decision.model_validate(decision_payload)

    actions = diff_to_actions(snap, decision, snapshot_ts)
    if not actions:
        print(f"no actions needed (book ${snap.wallet.total_equity_usd:.2f}, "
              f"decision matches current allocation within threshold)")
        return

    print(f"=== plan ({len(actions)} actions, dry_run={not args.live}) ===")
    for a in actions:
        print(f"  [{a.kind.value:22}] {a.category}/{a.product_id} {a.coin} "
              f"amount=${a.amount:.2f}  ({a.reason})")

    # `.12` approval gate. Dry-run skips; live requires interactive y/N
    # OR --yes-above-confidence. If approval is declined, downgrade to
    # a dry-run pass so the operator still gets a logged plan.
    effective_dry_run = not args.live
    if args.live:
        approved = request_approval(
            decision,
            actions,
            yes=args.yes,
            min_confidence=args.min_confidence,
        )
        if not approved:
            print("approval declined — downgrading to dry-run.")
            effective_dry_run = True

    async def run() -> None:
        async with BybitClient.from_settings() as client:
            results = await execute_actions(
                client, actions, snapshot_ts=snapshot_ts, dry_run=effective_dry_run
            )
        print("=== results ===")
        for r in results:
            print(f"  [{r.status:8}] {r.action.kind.value:22} "
                  f"{r.action.category}/{r.action.product_id} "
                  f"{r.action.coin} ${r.action.amount:.2f}"
                  + (f"  err={r.error}" if r.error else ""))
        print(f"  log: {EXECUTIONS_DIR / (snapshot_ts + '.jsonl')}")

    asyncio.run(run())
