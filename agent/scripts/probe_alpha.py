"""Live probe for Bybit Alpha Farm endpoints (`.54` follow-up).

Read-only — no purchase / redeem. Validates the four listing/asset
endpoints actually respond on the sandbox sub-account. Uses the typed
BybitClient methods (same code path as the loop), so this is the truest
"does the live loop see Alpha" test.

Run: `cd agent && uv run python -m scripts.probe_alpha`
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import BybitAPIError, BybitClient
from agent.bybit_oracle.config import OracleSettings


def _summarize(label: str, payload: object, max_rows: int = 3) -> None:
    print(f"\n=== {label} ===")
    if isinstance(payload, list):
        print(f"  rows: {len(payload)}")
        for row in payload[:max_rows]:
            print(f"  - {json.dumps(row, default=str)[:240]}")
        if len(payload) > max_rows:
            print(f"  … +{len(payload) - max_rows} more")
    elif isinstance(payload, dict):
        keys = sorted(payload.keys())
        print(f"  keys: {keys}")
        for k, v in list(payload.items())[:max_rows]:
            snippet = json.dumps(v, default=str)[:240]
            print(f"  - {k}: {snippet}")
    else:
        print(f"  {type(payload).__name__}: {payload}")


async def main() -> None:
    env_path = Path.home() / ".config" / "vault8004" / "bybit-sandbox.env"
    if env_path.is_file():
        load_dotenv(env_path, override=True)
    else:
        print(f"WARN: {env_path} not found — relying on shell env")

    cfg = OracleSettings()
    if not cfg.BYBIT_API_KEY.get_secret_value():
        print("FATAL: BYBIT_API_KEY not set")
        return

    print(f"probing base_url={cfg.BYBIT_BASE_URL}")
    # Sanity: same wallet-balance call the loop makes successfully every
    # cycle. If THIS fails, the auth path is broken and the Alpha results
    # are uninterpretable — bail.
    async with BybitClient.from_settings(cfg) as client:
        try:
            wallet = await client.get_asset_overview()
            print(f"baseline OK — wallet totalEquity={wallet.get('totalEquity')}")
        except Exception as e:  # noqa: BLE001
            print(f"baseline FAILED — {type(e).__name__}: {e}")
            print("aborting alpha probe — fix base auth first")
            return

        for label, coro in (
            ("biz-token-list", client.list_alpha_products()),
            ("pay-token-list", client.list_alpha_pay_tokens()),
            ("biz-token-price-list", client.list_alpha_price_info()),
            ("asset-list (POST trade/asset-list)", client.get_alpha_positions()),
        ):
            try:
                payload = await coro
                _summarize(label, payload)
            except BybitAPIError as e:
                print(f"\n=== {label} ===")
                print(f"  BybitAPIError retCode={e.ret_code} retMsg={e.ret_msg}")
            except Exception as e:  # noqa: BLE001
                print(f"\n=== {label} ===")
                print(f"  {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
