from datetime import datetime, timedelta, timezone

import aiohttp
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from agent.data.storage import save_parquet

_CHAIN_URL = "https://api.llama.fi/v2/historicalChainTvl/Mantle"
_PROTOCOLS = ["merchant-moe", "lendle", "agni-finance"]


def _cutoff() -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(days=90)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
async def _fetch_chain_tvl(session: aiohttp.ClientSession) -> None:
    async with session.get(_CHAIN_URL) as resp:
        resp.raise_for_status()
        data = await resp.json()

    cut = _cutoff()
    rows = []
    for item in data:
        ts = datetime.fromtimestamp(item["date"], tz=timezone.utc)
        if ts >= cut:
            rows.append({"timestamp": ts, "tvl_usd": float(item["tvl"])})

    df = pd.DataFrame(rows)
    save_parquet(df, "tvl_mantle", "raw")
    print(f"[defillama_tvl] ✓ {len(df)} rows → tvl_mantle.parquet")


async def _fetch_protocol_tvl(session: aiohttp.ClientSession, protocol: str) -> None:
    url = f"https://api.llama.fi/protocol/{protocol}"
    try:
        async with session.get(url) as resp:
            if resp.status == 404:
                print(f"[defillama_tvl] WARNING: {protocol} returned 404, skipping")
                return
            resp.raise_for_status()
            data = await resp.json()
    except Exception as exc:
        print(f"[defillama_tvl] WARNING: {protocol} failed ({exc}), skipping")
        return

    cut = _cutoff()
    rows = []
    for item in data.get("tvl", []):
        ts = datetime.fromtimestamp(item["date"], tz=timezone.utc)
        if ts >= cut:
            rows.append({"timestamp": ts, "tvl_usd": float(item["totalLiquidityUSD"])})

    df = pd.DataFrame(rows)
    name = f"tvl_{protocol}"
    save_parquet(df, name, "raw")
    print(f"[defillama_tvl] ✓ {len(df)} rows → {name}.parquet")


async def fetch_tvl() -> None:
    async with aiohttp.ClientSession() as session:
        await _fetch_chain_tvl(session)
        for protocol in _PROTOCOLS:
            await _fetch_protocol_tvl(session, protocol)
