import time

import aiohttp
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from agent.data.storage import save_parquet


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def _fetch(url: str) -> list:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
            r.raise_for_status()
            return await r.json()


async def fetch_funding() -> None:
    start_ms = int((time.time() - 90 * 86400) * 1000)
    all_rows: list = []
    cursor = start_ms

    while True:
        url = (
            f"https://fapi.binance.com/fapi/v1/fundingRate"
            f"?symbol=ETHUSDT&startTime={cursor}&limit=1000"
        )
        data = await _fetch(url)
        if not data:
            break
        all_rows.extend(data)
        if len(data) < 1000:
            break
        cursor = data[-1]["fundingTime"] + 1

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df["funding_rate_8h"] = df["fundingRate"].astype(float)
    df["annualized_apr"] = df["funding_rate_8h"] * 3 * 365
    df = df[["timestamp", "funding_rate_8h", "annualized_apr"]].sort_values("timestamp").reset_index(drop=True)
    save_parquet(df, "funding_rates", "raw")
    print(f"[funding] ✓ {len(df)} rows → funding_rates.parquet (expected ~270 for 90 days)")
