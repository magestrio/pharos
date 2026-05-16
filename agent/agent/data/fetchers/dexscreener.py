from datetime import datetime, timezone

import aiohttp
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from agent.data.storage import save_parquet

_METH_ADDRESS = "0xcDA86A272531e8640cD7F1a92c01839911B90bb0"
_URL = f"https://api.dexscreener.com/latest/dex/tokens/{_METH_ADDRESS}"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def fetch_dexscreener() -> None:
    print("[dexscreener] WARNING: DexScreener history limited — saving snapshot only")

    async with aiohttp.ClientSession() as session:
        async with session.get(_URL) as resp:
            resp.raise_for_status()
            data = await resp.json()

    pairs = data.get("pairs") or []
    weth_pairs = [
        p for p in pairs
        if "weth" in (p.get("quoteToken", {}).get("symbol") or "").lower()
        or "eth" in (p.get("quoteToken", {}).get("symbol") or "").lower()
    ]
    if not weth_pairs:
        weth_pairs = pairs

    if not weth_pairs:
        print("[dexscreener] WARNING: no pairs found, saving empty parquet")
        df = pd.DataFrame(columns=["timestamp", "price_usd", "price_native", "liquidity_usd", "volume_24h"])
        save_parquet(df, "dexscreener_meth", "raw")
        return

    best = max(weth_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))

    df = pd.DataFrame(
        [
            {
                "timestamp": datetime.now(tz=timezone.utc),
                "price_usd": float(best.get("priceUsd") or 0),
                "price_native": float(best.get("priceNative") or 0),
                "liquidity_usd": float((best.get("liquidity") or {}).get("usd") or 0),
                "volume_24h": float((best.get("volume") or {}).get("h24") or 0),
            }
        ]
    )

    save_parquet(df, "dexscreener_meth", "raw")
    print(f"[dexscreener] ✓ snapshot saved → dexscreener_meth.parquet (price=${df['price_usd'].iloc[0]:.4f})")
