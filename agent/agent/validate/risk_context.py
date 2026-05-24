from pydantic import BaseModel


class RiskContext(BaseModel):
    """Live risk metrics fed into the deterministic validator.

    `None` means the metric is currently unavailable. Conditional rules
    treat `None` as fail-closed: the rule is considered triggered, so the
    decision must respect the forced-exit constraint."""

    bybit_attestor_lag_minutes: float | None = None
    usdc_peg_deviation_bps: float | None = None
    aave_v3_usdc_utilization: float | None = None
    aave_v3_weth_utilization: float | None = None
