"""Probe `/v5/earn/hold-to-earn/product` — user hint that Alpha LP
(USDC/USD1 ~30% APR) lives in this category. Also check OnChain listing
for the same pairs in case it's surfaced there too.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import BybitAPIError, BybitClient
from agent.bybit_oracle.config import OracleSettings


async def _raw(client: BybitClient, label: str, method: str, path: str, params=None, body=None):
    try:
        data = await client._request(method, path, params=params, body=body)  # type: ignore[arg-type]
        result = data.get("result")
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = result.get("products") or result.get("list") or result.get("rows") or []
        else:
            items = []
        print(f"OK   {label}: rows={len(items)}")
        for r in items[:5]:
            if isinstance(r, dict):
                # Pull a compact representation
                relevant = {
                    k: v for k, v in r.items()
                    if k in ("productId", "coin", "baseCoin", "quoteCoin",
                             "estimateApr", "apr", "apyE8", "apy", "duration",
                             "minStakeAmount", "category", "type", "name",
                             "symbol", "title")
                }
                print(f"     - {json.dumps(relevant, default=str)[:240]}")
                # Also dump all keys for the first item to see full shape
                if r is items[0]:
                    print(f"       (all keys: {sorted(r.keys())})")
    except BybitAPIError as e:
        print(f"FAIL {label}: retCode={e.ret_code} {e.ret_msg[:120]}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {label}: {type(e).__name__}: {str(e)[:120]}")


async def main():
    load_dotenv(Path.home() / ".config" / "vault8004" / "bybit-sandbox.env", override=True)
    cfg = OracleSettings()

    async with BybitClient.from_settings(cfg) as c:
        print("--- FULL hold-to-earn dump ---")
        data = await c._request("GET", "/v5/earn/hold-to-earn/product")
        print(json.dumps(data.get("result"), indent=2)[:3000])
        print()
        print("--- /v5/earn/hold-to-earn/product (no params) ---")
        await _raw(c, "hold-to-earn no params", "GET", "/v5/earn/hold-to-earn/product")

        print()
        print("--- /v5/earn/hold-to-earn/product?coin=USD1 ---")
        await _raw(c, "hold-to-earn coin=USD1", "GET", "/v5/earn/hold-to-earn/product", params={"coin": "USD1"})

        print()
        print("--- /v5/earn/hold-to-earn/product?coin=USDC ---")
        await _raw(c, "hold-to-earn coin=USDC", "GET", "/v5/earn/hold-to-earn/product", params={"coin": "USDC"})

        print()
        print("--- /v5/earn/product?category=OnChain (look for USD1 pairs) ---")
        try:
            onchain = await c.list_earn_products(category="OnChain")
            usd1_rows = [
                p for p in onchain
                if p.coin == "USD1"
                or "USD1" in (getattr(p, "swapCoin", "") or "")
                or "USD1" in str(getattr(p, "duration", ""))
            ]
            print(f"OK   OnChain has {len(onchain)} products total, {len(usd1_rows)} mention USD1")
            for p in usd1_rows[:3]:
                print(f"     - product={p.productId} coin={p.coin} apr={p.estimateApr}")
        except BybitAPIError as e:
            print(f"FAIL OnChain: retCode={e.ret_code} {e.ret_msg[:120]}")


if __name__ == "__main__":
    asyncio.run(main())
