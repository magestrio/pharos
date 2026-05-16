import asyncio
from datetime import datetime, timezone

import aiohttp
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from agent.data.storage import save_parquet

_SEMAPHORE = asyncio.Semaphore(1)
_COIN_IDS = ["ethereum", "mantle-staked-ether"]
_BASE_URL = "https://api.coingecko.com/api/v3/coins/{id}/market_chart"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def _fetch_coin(session: aiohttp.ClientSession, coin_id: str) -> None:
    async with _SEMAPHORE:
        url = _BASE_URL.format(id=coin_id)
        params = {"vs_currency": "usd", "days": "90", "interval": "daily"}
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

    prices = data["prices"]
    market_caps = data["market_caps"]
    volumes = data["total_volumes"]

    df = pd.DataFrame(
        {
            "timestamp": [datetime.fromtimestamp(p[0] / 1000, tz=timezone.utc) for p in prices],
            "price_usd": [p[1] for p in prices],
            "market_cap": [m[1] for m in market_caps],
            "volume_24h": [v[1] for v in volumes],
        }
    )

    save_parquet(df, f"coingecko_{coin_id}", "raw")
    print(f"[coingecko] ✓ {len(df)} rows → coingecko_{coin_id}.parquet")
    await asyncio.sleep(2.5)


async def fetch_coingecko() -> None:
    async with aiohttp.ClientSession() as session:
        for coin_id in _COIN_IDS:
            await _fetch_coin(session, coin_id)
