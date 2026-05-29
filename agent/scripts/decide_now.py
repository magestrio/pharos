"""Loads both root .env (Anthropic) and bybit-sandbox.env (Bybit) and
runs a one-off decide cycle against the latest snapshot.

Usage: cd agent && uv run python -m scripts.decide_now [<snapshot.json>]
Defaults to the most recent snapshot file in agent/sandbox/snapshots/.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load env BEFORE importing decide (which constructs Anthropic client at import time? actually at call time, but be safe)
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
load_dotenv(Path.home() / ".config" / "vault8004" / "bybit-sandbox.env", override=True)

from agent.sandbox.decide import decide  # noqa: E402
from agent.sandbox.snapshot import Snapshot  # noqa: E402


SNAPSHOTS_DIR = Path(__file__).resolve().parents[1] / "agent" / "sandbox" / "snapshots"


async def main():
    if len(sys.argv) > 1:
        snap_path = Path(sys.argv[1])
    else:
        snaps = sorted(SNAPSHOTS_DIR.glob("*.json"))
        if not snaps:
            print("no snapshots")
            return
        snap_path = snaps[-1]
    print(f"snapshot: {snap_path.name}")

    snap_json = json.loads(snap_path.read_text())
    decision = await decide(snap_json)

    print()
    print(f"thesis: {decision.thesis}")
    print(f"confidence: {decision.confidence}")
    print(f"expected_blended_apr_pct: {decision.expected_blended_apr_pct}")
    print(f"risk_flags: {decision.risk_flags}")
    print()
    print("=== venues ===")
    for v in decision.venues:
        print(f"  {v.venue_id:25s} weight={v.weight:.3f}")
        for pick in v.picks:
            print(f"    - {pick.product_id:15s} pick_weight={pick.weight:.3f}")
    if decision.hedges:
        print()
        print("=== hedges (informational) ===")
        for h in decision.hedges:
            print(f"  {h.coin:8s} notional_usd={h.notional_usd}")
    if decision.notes:
        print()
        print("=== notes ===")
        for n in decision.notes:
            print(f"  - {n}")


if __name__ == "__main__":
    asyncio.run(main())
