from agent.execute.builders import AllocationCall


async def execute_on_chain(decision_id: bytes, calls: list[AllocationCall]) -> str:
    """Submit executeAllocation tx to Vault8004. Returns tx hash."""
    raise NotImplementedError
