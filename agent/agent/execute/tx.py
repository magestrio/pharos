from eth_account import Account
from web3 import Web3

from agent.config import settings
from agent.execute.builders import AllocationCall


_CM_EXECUTE_ABI = [{
    "name": "executeAllocation",
    "type": "function",
    "stateMutability": "nonpayable",
    "inputs": [
        {"name": "decisionId", "type": "bytes32"},
        {"name": "calls", "type": "tuple[]", "components": [
            {"name": "adapter", "type": "address"},
            {"name": "kind", "type": "uint8"},
            {"name": "amount", "type": "uint256"},
        ]},
        {"name": "minTotalAssetsAfter", "type": "uint256"},
    ],
    "outputs": [],
}]


SKIPPED = "skipped:no-calls"


async def execute_on_chain(cid: str, calls: list[AllocationCall]) -> str:
    """Submit executeAllocation to CapitalManager.

    `cid` is the IPFS CID string returned by the rationale upload step;
    the on-chain `decisionId` is `keccak256(cid)` — a deterministic
    bytes32 pointer to the off-chain rationale. (Truncating the raw CID
    string to 32 bytes is unsafe: not collision-resistant and not
    base-aware.)

    Returns the tx hash hex, or SKIPPED if `calls` is empty (contract
    requires calls.length > 0).
    """
    if not calls:
        return SKIPPED

    decision_id = Web3.keccak(text=cid)

    w3 = Web3(Web3.HTTPProvider(settings.MANTLE_RPC_URL))
    cm = w3.eth.contract(address=settings.CAPITAL_MANAGER_ADDRESS, abi=_CM_EXECUTE_ABI)

    acct = Account.from_key(settings.AGENT_PRIVATE_KEY)

    encoded_calls = [(c.adapter, int(c.kind), c.amount) for c in calls]

    # minTotalAssetsAfter=0: rely on CapitalManager.maxSlippageBps (global)
    # and maxPerCallLossBps (per-call) for slippage protection. Tightening
    # this on the agent side is a future hardening pass.
    tx = cm.functions.executeAllocation(decision_id, encoded_calls, 0).build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": w3.eth.chain_id,
    })

    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()
