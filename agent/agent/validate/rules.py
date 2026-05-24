from agent.reason.schema import Decision, TargetAllocation
from agent.validate.risk_context import RiskContext


MAX_POSITION = 0.70
BYBIT_ATTESTOR_CAP = 0.50
MIN_CASH = 0.03
MIN_CONFIDENCE = 0.4

BYBIT_LAG_FORCED_EXIT_MIN = 60
USDC_PEG_DEVIATION_TRIGGER_BPS = 100
AAVE_UTILIZATION_FORCED_EXIT = 0.95
STABLECOIN_CAP_ON_PEG_DEVIATION = 0.30


def check_sum(a: TargetAllocation) -> tuple[bool, str | None]:
    total = a.cash_usdc + a.aave_v3_usdc + a.aave_v3_weth + a.bybit_attestor
    if abs(total - 1.0) > 0.001:
        return False, f"allocations sum to {total:.4f}, expected 1.0 ± 0.001"
    return True, None


def check_cash_usdc(a: TargetAllocation) -> tuple[bool, str | None]:
    if a.cash_usdc < MIN_CASH:
        return False, f"cash_usdc {a.cash_usdc:.2%} below minimum {MIN_CASH:.0%}"
    return True, None


def check_max_position(a: TargetAllocation) -> tuple[bool, str | None]:
    positions = {
        "cash_usdc": a.cash_usdc,
        "aave_v3_usdc": a.aave_v3_usdc,
        "aave_v3_weth": a.aave_v3_weth,
        "bybit_attestor": a.bybit_attestor,
    }
    violations = [f"{k}={v:.2%}" for k, v in positions.items() if v > MAX_POSITION]
    if violations:
        return False, f"positions exceed {MAX_POSITION:.0%} cap: {', '.join(violations)}"
    return True, None


def check_bybit_attestor_cap(a: TargetAllocation) -> tuple[bool, str | None]:
    if a.bybit_attestor > BYBIT_ATTESTOR_CAP:
        return False, f"bybit_attestor {a.bybit_attestor:.2%} exceeds {BYBIT_ATTESTOR_CAP:.0%} concentration cap"
    return True, None


def check_confidence(d: Decision) -> tuple[bool, str | None]:
    if d.confidence < MIN_CONFIDENCE:
        return False, f"confidence {d.confidence:.2f} below minimum {MIN_CONFIDENCE:.1f}"
    return True, None


def check_risk_flags(d: Decision) -> tuple[bool, str | None]:
    if d.risk_flags:
        return False, f"red risk flags present: {d.risk_flags}"
    return True, None


def check_bybit_lag(d: Decision, ctx: RiskContext) -> tuple[bool, str | None]:
    """Forced exit from Bybit when attestor lag > 60min OR metric unavailable."""
    lag = ctx.bybit_attestor_lag_minutes
    triggered = lag is None or lag > BYBIT_LAG_FORCED_EXIT_MIN
    if triggered and d.target_allocation.bybit_attestor != 0:
        lag_str = "unavailable" if lag is None else f"{lag:.0f}min"
        return False, (
            f"bybit_attestor lag={lag_str} requires forced exit "
            f"(target.bybit_attestor must be 0, got {d.target_allocation.bybit_attestor:.2%})"
        )
    return True, None


def check_usdc_peg(d: Decision, ctx: RiskContext) -> tuple[bool, str | None]:
    """Reduce combined USDC-denominated exposure when peg deviation > 100bps OR metric unavailable."""
    dev = ctx.usdc_peg_deviation_bps
    triggered = dev is None or dev > USDC_PEG_DEVIATION_TRIGGER_BPS
    if not triggered:
        return True, None
    stable_exposure = d.target_allocation.cash_usdc + d.target_allocation.aave_v3_usdc
    if stable_exposure > STABLECOIN_CAP_ON_PEG_DEVIATION:
        dev_str = "unavailable" if dev is None else f"{dev:.0f}bps"
        return False, (
            f"usdc_peg_deviation={dev_str} requires reduced stablecoin exposure "
            f"(cash_usdc + aave_v3_usdc = {stable_exposure:.2%} > {STABLECOIN_CAP_ON_PEG_DEVIATION:.0%} cap)"
        )
    return True, None


def check_aave_utilization(d: Decision, ctx: RiskContext) -> tuple[bool, str | None]:
    """Forced exit per-pool when utilization > 95% OR metric unavailable."""
    errors: list[str] = []
    for pool_name, util, target_pct in (
        ("aave_v3_usdc", ctx.aave_v3_usdc_utilization, d.target_allocation.aave_v3_usdc),
        ("aave_v3_weth", ctx.aave_v3_weth_utilization, d.target_allocation.aave_v3_weth),
    ):
        triggered = util is None or util > AAVE_UTILIZATION_FORCED_EXIT
        if triggered and target_pct != 0:
            util_str = "unavailable" if util is None else f"{util:.2%}"
            errors.append(
                f"{pool_name} utilization={util_str} requires forced exit "
                f"(target.{pool_name} must be 0, got {target_pct:.2%})"
            )
    if errors:
        return False, "; ".join(errors)
    return True, None


def validate(decision: Decision, risk_context: RiskContext) -> tuple[bool, list[str]]:
    a = decision.target_allocation
    checks = [
        check_sum(a),
        check_cash_usdc(a),
        check_max_position(a),
        check_bybit_attestor_cap(a),
        check_confidence(decision),
        check_risk_flags(decision),
        check_bybit_lag(decision, risk_context),
        check_usdc_peg(decision, risk_context),
        check_aave_utilization(decision, risk_context),
    ]
    errors = [msg for ok, msg in checks if not ok and msg]
    return len(errors) == 0, errors
