"""Isolate the 10004 signature error on /v5/alpha/* POST endpoints.

Compares behavior with:
- Working baseline: /v5/account/wallet-balance (we know this auth works)
- pay-token-list (empty body — no parameters to muddle)
- biz-token-list with int tokenTag, str tokenTag, omitted, with quoteMode
- asset-list with empty body
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import BybitAPIError, BybitClient
from agent.bybit_oracle.config import OracleSettings


async def _probe(
    client: BybitClient,
    label: str,
    method: str,
    path: str,
    body: dict | None = None,
    params: dict | None = None,
) -> None:
    try:
        data = await client._request(method, path, body=body, params=params)  # type: ignore[arg-type]
        result = data.get("result")
        if isinstance(result, list):
            print(f"OK  {label}: rows={len(result)}")
        elif isinstance(result, dict):
            keys = sorted(result.keys())[:5]
            print(f"OK  {label}: keys={keys}")
        else:
            print(f"OK  {label}: {type(result).__name__}")
    except BybitAPIError as e:
        print(f"ERR {label}: retCode={e.ret_code} msg={e.ret_msg}")
    except Exception as e:  # noqa: BLE001
        print(f"ERR {label}: {type(e).__name__}: {e}")


async def main() -> None:
    env_path = Path.home() / ".config" / "vault8004" / "bybit-sandbox.env"
    if env_path.is_file():
        load_dotenv(env_path, override=True)
    cfg = OracleSettings()
    api_key_val = cfg.BYBIT_API_KEY.get_secret_value()
    print(f"base_url={cfg.BYBIT_BASE_URL}")
    print(f"api_key_prefix={api_key_val[:6]}…{api_key_val[-4:] if api_key_val else ''}")
    print(f"private_key_path={cfg.BYBIT_PRIVATE_KEY_PATH}")
    print(f"recv_window={cfg.BYBIT_RECV_WINDOW}")

    async with BybitClient.from_settings(cfg) as client:
        # Baseline: known-good auth path. Pass query via `params` so the
        # client's sign-over-query logic kicks in correctly — embedding
        # the ? in the path string bypasses signing and Bybit returns 10004.
        await _probe(client, "baseline /v5/account/wallet-balance GET", "GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"})
        await _probe(client, "baseline /v5/asset/asset-overview GET (no params)", "GET", "/v5/asset/transfer/query-account-coins-balance", params={"accountType": "UNIFIED"})
        print("--- alpha variants ---")
        # pay-token-list: docs claim signed JSON body, no params
        await _probe(client, "pay-token-list empty {}", "POST", "/v5/alpha/trade/pay-token-list", {})
        # biz-token-list: docs say tokenTag int
        await _probe(client, "biz-token-list {tokenTag:0}", "POST", "/v5/alpha/trade/biz-token-list", {"tokenTag": 0})
        await _probe(client, "biz-token-list {tokenTag:'0'}", "POST", "/v5/alpha/trade/biz-token-list", {"tokenTag": "0"})
        await _probe(client, "biz-token-list {} (omit tokenTag)", "POST", "/v5/alpha/trade/biz-token-list", {})
        # price-list
        await _probe(client, "biz-token-price-list empty {}", "POST", "/v5/alpha/trade/biz-token-price-list", {})
        # asset-list (corrected path)
        await _probe(client, "asset-list (POST trade/asset-list)", "POST", "/v5/alpha/trade/asset-list", {})


if __name__ == "__main__":
    asyncio.run(main())
