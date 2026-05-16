from dataclasses import dataclass

from agent.gather.vault_state import VaultState
from agent.reason.schema import TargetAllocation


@dataclass
class AllocationCall:
    adapter: str
    data: bytes


def build_allocation_calls(
    current: VaultState,
    target: TargetAllocation,
) -> list[AllocationCall]:
    raise NotImplementedError
