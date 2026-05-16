import asyncio
import httpx
from datetime import datetime, timezone
from agent.gather.models import MarketData

# Same pool IDs as data/fetchers/defillama_yields.py — intentionally separate (different responsibilities)
_POOL_IDS = {
    "susde":     "66985a81-9c51-46ca-9977-42b4fe7bc6df",  # Ethena sUSDe, Ethereum
    "aave_usdc": "32cb38a5-b9b9-441a-bf07-8fab47b999d3",  # Aave V3 USDC Mantle
    "cmeth":     "b96d8236-36d4-4be4-92f7-422beeac7073",  # cmETH Lendle Mantle
}


async def get_market_data() -> MarketData:
    """Live market data from public APIs."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        cg_task = client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum,mantle-staked-ether", "vs_currencies": "usd"},
        )
        fr_task = client.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": "ETHUSDT", "limit": 21},
        )
        tvl_task = client.get("https://api.llama.fi/v2/historicalChainTvl/Mantle")

        cg_resp, fr_resp, tvl_resp = await asyncio.gather(cg_task, fr_task, tvl_task)

        cg = cg_resp.json()
        eth_price = cg["ethereum"]["usd"]
        meth_price = cg["mantle-staked-ether"]["usd"]

        fr_data = fr_resp.json()
        latest_funding = float(fr_data[-1]["fundingRate"])
        funding_7d_avg = sum(float(x["fundingRate"]) for x in fr_data) / len(fr_data)

        tvl_data = tvl_resp.json()
        mantle_tvl = tvl_data[-1]["tvl"] if tvl_data else 0.0

        yields: dict[str, float] = {}
        for name, pool_id in _POOL_IDS.items():
            r = await client.get(f"https://yields.llama.fi/chart/{pool_id}")
            data = r.json().get("data", [])
            yields[name] = data[-1]["apy"] / 100 if data else 0.0

    # TODO Week 2: подключить mETH Protocol API
    meth_apy = 0.035

    return MarketData(
        eth_price_usd=eth_price,
        meth_price_usd=meth_price,
        meth_eth_ratio=meth_price / eth_price,
        meth_exchange_rate=1.0,  # TODO Week 2: mETH Protocol API
        meth_apy=meth_apy,
        cmeth_apy=yields.get("cmeth", 0.0),
        susde_apy=yields.get("susde", 0.0),
        aave_usdc_apy=yields.get("aave_usdc", 0.0),
        funding_rate_8h=latest_funding,
        funding_rate_7d_avg=funding_7d_avg,
        mantle_tvl_usd=mantle_tvl,
        timestamp=datetime.now(timezone.utc),
    )
