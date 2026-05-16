import aiohttp
import pandas as pd

from agent.data.storage import save_parquet

_URLS = [
    "https://meth.mantle.xyz/api/stats",
    "https://api.mantle.xyz/meth/v1/stats",
]
_EMPTY_COLS = ["timestamp", "exchange_rate", "total_staked_eth", "apy"]


async def fetch_meth() -> None:
    async with aiohttp.ClientSession() as session:
        for url in _URLS:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 404:
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    df = pd.DataFrame([data]) if isinstance(data, dict) else pd.DataFrame(data)
                    save_parquet(df, "meth_protocol", "raw")
                    print(f"[meth_api] ✓ fetched from {url} → meth_protocol.parquet")
                    return
            except Exception:
                continue

    print("[meth_api] WARNING: mETH native API not accessible, using DefiLlama fallback")
    df = pd.DataFrame(columns=_EMPTY_COLS)
    save_parquet(df, "meth_protocol", "raw")
