from dataclasses import dataclass
from enum import IntEnum

from agent.config import settings
from agent.gather.models import VaultState
from agent.gather.vault_state import ADAPTER_VENUES
from agent.reason.schema import TargetAllocation


DELTA_THRESHOLD = 0.02  # skip rebalance if |target - current| / TVL < 2%

ZERO_ADDR = "0x0000000000000000000000000000000000000000"


class AllocationCallKind(IntEnum):
    DEPOSIT = 0
    WITHDRAW = 1


@dataclass
class AllocationCall:
    adapter: str
    kind: AllocationCallKind
    amount: int  # raw 6-decimal USDC for AaveV3UsdcAdapter & BybitAttestor


def _venue_address(name: str) -> str:
    return {
        "aave_v3_usdc": settings.AAVE_V3_USDC_ADAPTER,
        "aave_v3_weth": settings.AAVE_V3_WETH_ADAPTER,
        "bybit_attestor": settings.BYBIT_ATTESTOR_ADAPTER,
    }[name]


def _current_usdc(current: VaultState, venue: str) -> float:
    for a in current.allocations:
        if a.name == venue:
            return a.balance_assets
    return 0.0


def build_allocation_calls(
    current: VaultState,
    target: TargetAllocation,
) -> list[AllocationCall]:
    """Diff current per-venue balances against target. Emit Withdraws
    first (free up USDC) then Deposits. Skip any venue whose delta is
    below DELTA_THRESHOLD of TVL. Cash is residual — no call.

    Raises if `target.aave_v3_weth > 0` or a non-zero current WETH leg
    exists: there is no USDC<->WETH swap rail yet (weth-funding-gap).
    """
    # TODO(weth-funding-gap): AaveV3WethAdapter.deposit pulls WETH from
    # CapitalManager, but CapitalManager holds only USDC. Until a swap
    # adapter lands (Aave Pool swap or Moe LB Router), routing to
    # aave_v3_weth is unsupported. Reason phase must keep aave_v3_weth=0.
    if target.aave_v3_weth > 0 or _current_usdc(current, "aave_v3_weth") > 0:
        raise ValueError(
            "aave_v3_weth leg unavailable (weth-funding-gap): "
            f"target={target.aave_v3_weth:.2%}, "
            f"current={_current_usdc(current, 'aave_v3_weth'):.6f} USDC. "
            "Set target.aave_v3_weth=0 until a USDC<->WETH swap rail exists."
        )

    total = current.total_assets_usd
    if total <= 0:
        return []

    withdraws: list[AllocationCall] = []
    deposits: list[AllocationCall] = []

    for venue in ADAPTER_VENUES:
        if venue == "aave_v3_weth":
            continue  # gated above

        target_pct = getattr(target, venue)
        target_usdc = target_pct * total
        current_usdc = _current_usdc(current, venue)
        delta_usdc = target_usdc - current_usdc

        if abs(delta_usdc) / total < DELTA_THRESHOLD:
            continue

        addr = _venue_address(venue)
        if not addr or addr == ZERO_ADDR:
            raise ValueError(
                f"venue {venue} adapter address not configured (set "
                f"{venue.upper()}_ADAPTER) but target_pct={target_pct:.2%}"
            )

        amount_raw = int(round(abs(delta_usdc) * 1e6))

        kind = AllocationCallKind.WITHDRAW if delta_usdc < 0 else AllocationCallKind.DEPOSIT
        call = AllocationCall(adapter=addr, kind=kind, amount=amount_raw)
        (withdraws if delta_usdc < 0 else deposits).append(call)

    return withdraws + deposits
