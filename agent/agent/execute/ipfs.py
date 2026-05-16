from agent.reason.schema import Decision


async def upload_rationale(decision: Decision) -> str:
    """Upload decision rationale to IPFS via Pinata. Returns CID."""
    raise NotImplementedError
