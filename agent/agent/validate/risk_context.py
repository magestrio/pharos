from pydantic import BaseModel


class RiskContext(BaseModel):
    """Live risk metrics fed into the deterministic validator.

    `None` means the metric is currently unavailable. Conditional rules
    treat `None` as fail-closed: the rule is considered triggered, so the
    decision must respect the forced-exit constraint.

    `weth_funding_available=False` indicates the USDC<->WETH swap rail is
    not yet wired (weth-funding-gap). While False, any non-zero
    aave_v3_weth allocation is rejected by check_weth_available."""

    bybit_attestor_lag_minutes: float | None = None
    usdc_peg_deviation_bps: float | None = None
    aave_v3_usdc_utilization: float | None = None
    aave_v3_weth_utilization: float | None = None
    weth_funding_available: bool = False
