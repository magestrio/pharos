from datetime import datetime, timezone
from agent.gather.models import RiskMetrics, MarketData, VaultState


async def get_risk_metrics(market: MarketData, vault: VaultState) -> RiskMetrics:
    """Derived metrics from market + vault state. Pure logic, no HTTP."""
    red_flags: list[str] = []

    expected_meth_price = market.eth_price_usd * market.meth_exchange_rate
    depeg_pct = (market.meth_price_usd - expected_meth_price) / expected_meth_price
    depeg_bps = depeg_pct * 10_000

    if depeg_bps < -200:
        red_flags.append(f"meth_depeg: {depeg_bps:.0f} bps")

    funding_positive = market.funding_rate_7d_avg > 0
    if not funding_positive:
        red_flags.append(f"susde_funding_negative: 7d avg {market.funding_rate_7d_avg:.4f}")

    # TODO Week 2: read Aave utilization on-chain
    aave_util = 0.7

    # TODO Week 2: read cmETH cooldown status from adapter
    cmeth_cooldown = False

    return RiskMetrics(
        meth_depeg_bps=depeg_bps,
        susde_funding_7d_avg=market.funding_rate_7d_avg,
        susde_funding_is_positive=funding_positive,
        aave_usdc_utilization=aave_util,
        cmeth_cooldown_active=cmeth_cooldown,
        oracle_max_staleness_sec=0,
        red_flags=red_flags,
        timestamp=datetime.now(timezone.utc),
    )
