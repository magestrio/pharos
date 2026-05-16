import aiohttp
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from agent.data.storage import save_parquet

POOL_IDS = {
    "susde":     "66985a81-9c51-46ca-9977-42b4fe7bc6df",  # Ethena sUSDe, Ethereum
    "aave_usdc": "32cb38a5-b9b9-441a-bf07-8fab47b999d3",  # Aave V3 USDC Mantle
    "cmeth":     "b96d8236-36d4-4be4-92f7-422beeac7073",  # cmETH Lendle Mantle
}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def _fetch_chart(session: aiohttp.ClientSession, pool_id: str) -> dict:
    url = f"https://yields.llama.fi/chart/{pool_id}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        return await r.json()


async def fetch_yields() -> None:
    async with aiohttp.ClientSession() as session:
        for name, pid in POOL_IDS.items():
            try:
                data = await _fetch_chart(session, pid)
                rows = data.get("data", [])
                df = pd.DataFrame(rows)
                if df.empty:
                    print(f"[yields] ⚠ {name}: empty response")
                    continue
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                df = df.rename(columns={
                    "apyBase": "apy_base",
                    "apyReward": "apy_reward",
                    "tvlUsd": "tvl_usd",
                })
                cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=90)
                df = df[df["timestamp"] >= cutoff]
                df = df[["timestamp", "apy", "apy_base", "apy_reward", "tvl_usd"]]
                save_parquet(df, f"yields_{name}", "raw")
                print(f"[yields] ✓ {name}: {len(df)} rows → yields_{name}.parquet")
            except Exception as e:
                print(f"[yields] ✗ {name}: {e}")
