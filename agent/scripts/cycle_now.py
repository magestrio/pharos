"""Loads root .env (Anthropic) + bybit-sandbox.env (Bybit) and runs ONE
full loop cycle (snapshot → decide → validate → diff → execute). Defaults
to dry-run unless --live is passed. For .14 pre-flight validation.

Usage:
    cd agent && uv run python -m scripts.cycle_now            # dry-run
    cd agent && uv run python -m scripts.cycle_now --live     # actually trade
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

# Load env BEFORE importing modules that construct clients at import time
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
load_dotenv(Path.home() / ".config" / "vault8004" / "bybit-sandbox.env", override=True)

import anthropic  # noqa: E402

from agent.bybit_oracle.bybit_client import BybitClient  # noqa: E402
from agent.bybit_oracle.config import OracleSettings  # noqa: E402
from agent.sandbox.loop import run_one_cycle  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Actually trade")
    parser.add_argument("--yes", action="store_true", help="Auto-approve")
    parser.add_argument("--min-confidence", type=float, default=0.6)
    args = parser.parse_args()

    cfg = OracleSettings()
    async with BybitClient.from_settings(cfg) as bybit:
        anthropic_client = anthropic.AsyncAnthropic()
        outcome = await run_one_cycle(
            bybit,
            anthropic_client,
            live=args.live,
            yes=args.yes,
            min_confidence=args.min_confidence,
        )
    print()
    print("=== cycle outcome ===")
    print(json.dumps(outcome, indent=2, default=str)[:3000])


if __name__ == "__main__":
    asyncio.run(main())
