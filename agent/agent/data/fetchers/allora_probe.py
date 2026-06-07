"""One-shot probe to figure out which Allora endpoint accepts our API
key + what BTC price prediction looks like right now.

Run: `uv run --directory agent python -m agent.data.fetchers.allora_probe`

The Allora team historically split its API across two hosts (Upshot
consumer and Allora-native), and topic IDs aren't enumerated in public
docs — so this script just tries the candidate URLs in order and prints
whichever one returns a parseable inference. Use the output to pick the
canonical endpoint for the real fetcher.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx

# Each candidate is (label, URL). API key goes in `x-api-key` header.
# Tries: legacy Upshot consumer price endpoint, Allora-native equivalent,
# and on-chain raw inference endpoint with the BTC topic candidate IDs.
_CANDIDATES: list[tuple[str, str]] = [
    # Upshot consumer "/price" — symbol + horizon path style.
    ("upshot/BTC/5min", "https://api.upshot.xyz/v2/allora/consumer/price/ethereum-11155111/BTC/5min"),
    ("upshot/BTC/8h",   "https://api.upshot.xyz/v2/allora/consumer/price/ethereum-11155111/BTC/8h"),
    ("upshot/BTC/24h",  "https://api.upshot.xyz/v2/allora/consumer/price/ethereum-11155111/BTC/24h"),
    # Allora-native consumer.
    ("allora/BTC/5min", "https://api.allora.network/v2/allora/consumer/price/ethereum-11155111/BTC/5min"),
    ("allora/BTC/8h",   "https://api.allora.network/v2/allora/consumer/price/ethereum-11155111/BTC/8h"),
    # On-chain raw inference by topic id — `2`/`4` are the historical BTC topics.
    ("emissions/topic=2", "https://allora-api.testnet.allora.network/emissions/v7/latest_network_inferences/2"),
    ("emissions/topic=4", "https://allora-api.testnet.allora.network/emissions/v7/latest_network_inferences/4"),
]


async def _try(url: str, key: str) -> tuple[int, dict[str, Any] | str]:
    headers = {"x-api-key": key, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return 0, f"NETWORK_ERROR: {type(e).__name__}: {e}"
    if "application/json" in resp.headers.get("content-type", ""):
        try:
            return resp.status_code, resp.json()
        except json.JSONDecodeError:
            return resp.status_code, resp.text[:500]
    return resp.status_code, resp.text[:500]


async def main() -> int:
    key = os.environ.get("ALLORA_API_KEY", "")
    if not key:
        print("ALLORA_API_KEY missing in env", file=sys.stderr)
        return 1
    print(f"using ALLORA_API_KEY: {key[:6]}…{key[-4:]} ({len(key)} chars)")
    print()
    hits: list[str] = []
    for label, url in _CANDIDATES:
        status, body = await _try(url, key)
        ok = isinstance(status, int) and 200 <= status < 300
        marker = "OK " if ok else f"{status:>3}"
        snippet = (
            json.dumps(body, indent=2, default=str)[:600]
            if isinstance(body, dict)
            else str(body)[:600]
        )
        print(f"=== [{marker}] {label} ===")
        print(f"  url: {url}")
        print(snippet)
        print()
        if ok:
            hits.append(label)
    print(f"working endpoints: {hits or '(none)'}")
    return 0 if hits else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
