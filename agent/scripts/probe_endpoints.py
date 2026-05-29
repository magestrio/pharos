"""Probe a spread of Bybit endpoints — public + signed across categories —
to see if 10004 affects ALL signed calls or only some.

Public endpoints (no auth) tell us the network + base URL are fine.
Signed endpoints across different namespaces tell us if it's key-wide.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import BybitAPIError, BybitClient
from agent.bybit_oracle.config import OracleSettings


async def _check(label: str, coro) -> None:
    try:
        out = await coro
        if isinstance(out, list):
            print(f"OK   {label}: list[{len(out)}]")
        elif isinstance(out, dict):
            print(f"OK   {label}: dict keys={sorted(out.keys())[:6]}")
        else:
            print(f"OK   {label}: {type(out).__name__}")
    except BybitAPIError as e:
        print(f"FAIL {label}: retCode={e.ret_code} {e.ret_msg[:120]}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {label}: {type(e).__name__}: {str(e)[:120]}")


async def main() -> None:
    env_path = Path.home() / ".config" / "vault8004" / "bybit-sandbox.env"
    if env_path.is_file():
        load_dotenv(env_path, override=True)

    cfg = OracleSettings()
    api_key = cfg.BYBIT_API_KEY.get_secret_value()
    print(f"key_prefix={api_key[:6]}…{api_key[-4:]}  base_url={cfg.BYBIT_BASE_URL}")
    print()
    print("--- PUBLIC (no auth required) ---")

    async with BybitClient.from_settings(cfg) as client:
        # Public endpoints — these work without any auth at all
        await _check("get_tickers linear BTCUSDT", client.get_tickers(category="linear", symbol="BTCUSDT"))
        await _check("get_orderbook linear BTCUSDT", client.get_orderbook(category="linear", symbol="BTCUSDT"))
        await _check("get_funding_history BTCUSDT", client.get_funding_history("BTCUSDT", limit=3))
        await _check("get_kline linear BTCUSDT D", client.get_kline("BTCUSDT", interval="D", limit=3))
        await _check("get_instruments_info linear", client.get_instruments_info(category="linear", symbol="BTCUSDT"))

        print()
        print("--- SIGNED (require valid API key auth) ---")
        await _check("get_asset_overview", client.get_asset_overview())
        await _check("list_earn_products FlexibleSaving", client.list_earn_products(category="FlexibleSaving"))
        await _check("list_advance_earn_products DualAssets", client.list_advance_earn_products(category="DualAssets"))
        await _check("list_liquidity_mining_products", client.list_liquidity_mining_products())
        await _check("get_positions linear USDT", client.get_positions(category="linear", settle_coin="USDT"))
        await _check("get_earn_positions FlexibleSaving", client.get_earn_positions(category="FlexibleSaving"))

        print()
        print("--- ALPHA (.54) ---")
        # Fetch listing first — we need a real (chainCode, tokenAddress)
        # pair to exercise the per-token pay-token-list and the batch
        # price-list endpoints (both 180001 on empty body).
        alpha_list = await client.list_alpha_products()
        print(f"OK   list_alpha_products: list[{len(alpha_list)}]")
        if alpha_list:
            sample = alpha_list[0]
            chain = sample.get("chainCode", "")
            addr = sample.get("tokenAddress", "")
            symbol = sample.get("symbol", "?")
            print(f"     sample: {symbol} on {chain} @ {addr[:18]}…")
            await _check(
                f"list_alpha_pay_tokens(chain={chain})",
                client.list_alpha_pay_tokens(chain_code=chain, token_address=addr),
            )
            await _check(
                "list_alpha_price_info (batch top-3)",
                client.list_alpha_price_info(
                    token_address_info=[
                        {
                            "chainCode": p.get("chainCode", ""),
                            "tokenAddress": p.get("tokenAddress", ""),
                        }
                        for p in alpha_list[:3]
                    ]
                ),
            )
        await _check("get_alpha_positions", client.get_alpha_positions())


if __name__ == "__main__":
    asyncio.run(main())
