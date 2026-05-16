from agent.reason.schema import Decision, TargetAllocation


def check_sum(a: TargetAllocation) -> tuple[bool, str | None]:
    total = a.mETH_staked + a.cmETH + a.sUSDe + a.lendle_usdc + a.cash
    if abs(total - 1.0) > 0.001:
        return False, f"allocations sum to {total:.4f}, expected 1.0 ± 0.001"
    return True, None


def check_cash(a: TargetAllocation) -> tuple[bool, str | None]:
    if a.cash < 0.03:
        return False, f"cash {a.cash:.2%} below minimum 3%"
    return True, None


def check_max_position(a: TargetAllocation) -> tuple[bool, str | None]:
    positions = {
        "mETH_staked": a.mETH_staked,
        "cmETH": a.cmETH,
        "sUSDe": a.sUSDe,
        "lendle_usdc": a.lendle_usdc,
        "cash": a.cash,
    }
    violations = [f"{k}={v:.2%}" for k, v in positions.items() if v > 0.60]
    if violations:
        return False, f"positions exceed 60% cap: {', '.join(violations)}"
    return True, None


def check_susde(a: TargetAllocation) -> tuple[bool, str | None]:
    if a.sUSDe > 0.50:
        return False, f"sUSDe {a.sUSDe:.2%} exceeds 50% cap"
    return True, None


def check_confidence(d: Decision) -> tuple[bool, str | None]:
    if d.confidence < 0.4:
        return False, f"confidence {d.confidence:.2f} below minimum 0.4"
    return True, None


def check_risk_flags(d: Decision) -> tuple[bool, str | None]:
    if d.risk_flags:
        return False, f"red risk flags present: {d.risk_flags}"
    return True, None


def validate(decision: Decision) -> tuple[bool, list[str]]:
    a = decision.target_allocation
    checks = [
        check_sum(a),
        check_cash(a),
        check_max_position(a),
        check_susde(a),
        check_confidence(decision),
        check_risk_flags(decision),
    ]
    errors = [msg for ok, msg in checks if not ok and msg]
    return len(errors) == 0, errors
