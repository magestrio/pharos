import aiohttp
import pandas as pd

from agent.data.storage import save_parquet

# All APY columns are stored in decimal form: 0.05 means 5% annualized.
# If the mETH API returns apy as percent (e.g. 3.52 for 3.52%), divide by 100 before saving.
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
                    # Convert percent → decimal if apy looks like it's in percent (> 1 is a safe heuristic)
                    if "apy" in df.columns and df["apy"].notna().any() and float(df["apy"].dropna().iloc[0]) > 1:
                        df["apy"] = df["apy"] / 100
                    save_parquet(df, "meth_protocol", "raw")
                    print(f"[meth_api] ✓ fetched from {url} → meth_protocol.parquet")
                    return
            except Exception:
                continue

    print("[meth_api] WARNING: mETH native API not accessible, using DefiLlama fallback")
    df = pd.DataFrame(columns=_EMPTY_COLS)
    save_parquet(df, "meth_protocol", "raw")
