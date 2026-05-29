"""Probe — does Bybit LM listing return USDC/USD1 if we ask explicitly?

Snapshot returned 22 LM products with USDC pairs only (BTC/USDC, ETH/USDC)
plus USDT-quote pairs. No USDC/USD1. Either:
  (a) USDC/USD1 is a real LM product hidden by default listing pagination
  (b) "Alpha LP" with USDC/USD1 is a separate API namespace
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import BybitAPIError, BybitClient
from agent.bybit_oracle.config import OracleSettings


async def _try(label: str, coro):
    try:
        out = await coro
        if isinstance(out, list):
            print(f"OK   {label}: list[{len(out)}]")
            for r in out[:10]:
                b = r.get("baseCoin", "?")
                q = r.get("quoteCoin", "?")
                pid = r.get("productId", "?")
                apy = r.get("apyE8")
                print(f"     - product={pid:>4s} pair={b}/{q} apyE8={apy}")
        else:
            print(f"OK   {label}: {type(out).__name__}")
    except BybitAPIError as e:
        print(f"FAIL {label}: retCode={e.ret_code} {e.ret_msg[:120]}")


async def main():
    load_dotenv(Path.home() / ".config" / "vault8004" / "bybit-sandbox.env", override=True)
    cfg = OracleSettings()

    async with BybitClient.from_settings(cfg) as c:
        print("--- default LM listing (no filter) ---")
        await _try("list_liquidity_mining_products()", c.list_liquidity_mining_products())

        print()
        print("--- explicit USDC base / USD1 quote ---")
        await _try("LM USDC/USD1", c.list_liquidity_mining_products(base_coin="USDC", quote_coin="USD1"))
        await _try("LM USD1/USDC", c.list_liquidity_mining_products(base_coin="USD1", quote_coin="USDC"))

        print()
        print("--- USD1 anywhere ---")
        await _try("LM USD1/*", c.list_liquidity_mining_products(quote_coin="USD1"))
        await _try("LM */USD1", c.list_liquidity_mining_products(base_coin="USD1"))


if __name__ == "__main__":
    asyncio.run(main())
