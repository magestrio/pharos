from agent.execute.builders import AllocationCall


async def execute_on_chain(decision_id: bytes, calls: list[AllocationCall]) -> str:
    """Submit executeAllocation tx to CapitalManager. Returns tx hash."""
    raise NotImplementedError
