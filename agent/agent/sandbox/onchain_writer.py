"""On-chain writer for `DecisionLog` + `ReputationOracle`.

After each cycle the loop calls `record_decision` so every decision the
agent emits is anchored on-chain — `(agentId, decisionId, ipfsCid,
actionHash)`. Periodically (and best-effort) it also calls
`update_reputation` so the canonical ERC-8004 ReputationRegistry sees
the live APR score.

Both methods are best-effort: any RPC blip / revert / missing config
returns `None`. The off-chain decision file + Postgres row remain the
source of truth — the on-chain log is the public audit trail.

Sync over web3.py — wrap in `asyncio.to_thread` from async callers.
Not thread-safe (nonce reuse will collide); serialize within a single
loop cycle.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from eth_account import Account
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError

log = logging.getLogger(__name__)

_DECISION_LOG_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "recordDecision",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "decisionId", "type": "bytes32"},
            {"name": "ipfsCid", "type": "string"},
            {"name": "actionHash", "type": "bytes32"},
        ],
        "outputs": [{"name": "nonce", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "exists",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "agent",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]

_REPUTATION_ORACLE_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "updateReputation",
        "stateMutability": "nonpayable",
        "inputs": [],
        "outputs": [{"name": "scoreBps", "type": "int128"}],
    },
    {
        "type": "function",
        "name": "canUpdate",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def derive_ids(
    decision: dict[str, Any],
    snapshot_filename: str,
) -> tuple[bytes, bytes]:
    """`(decisionId, actionHash)` as 32-byte values.

    - `decisionId = keccak256(snapshot_filename ":" written_at)` — stable
      for a given (cycle, decision) pair; collisions would mean the
      same cycle ran twice.
    - `actionHash = keccak256(canonical_json(venues + hedges))` —
      summarizes what the agent intends to execute, independent of
      thesis/notes/confidence prose.
    """
    meta = decision.get("_meta") or {}
    written_at = str(meta.get("written_at") or "")
    decision_id = Web3.keccak(text=f"{snapshot_filename}:{written_at}")
    payload = json.dumps(
        {
            "venues": decision.get("venues") or [],
            "hedges": decision.get("hedges") or [],
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    action_hash = Web3.keccak(text=payload)
    return decision_id, action_hash


class OnchainWriter:
    """Bundles the agent EOA + DecisionLog/Oracle contracts.

    Build via `OnchainWriter.from_env()` — returns `None` when any
    required env var is missing, so the loop can degrade gracefully
    without on-chain anchoring (file + DB stay).
    """

    def __init__(
        self,
        *,
        w3: Web3,
        account: Account,
        agent_id: int,
        decision_log: Contract,
        oracle: Contract | None,
    ) -> None:
        self.w3 = w3
        self.account = account
        self.agent_id = agent_id
        self.decision_log = decision_log
        self.oracle = oracle

    @classmethod
    def from_env(cls) -> OnchainWriter | None:
        pk = os.environ.get("AGENT_PRIVATE_KEY")
        rpc = os.environ.get("MANTLE_RPC_URL")
        dlog_addr = os.environ.get("DECISION_LOG_ADDRESS")
        oracle_addr = os.environ.get("REPUTATION_ORACLE_ADDRESS")
        agent_id_raw = os.environ.get("AGENT_ID")

        if not (pk and rpc and dlog_addr and agent_id_raw):
            log.info(
                "onchain writer disabled: missing one of "
                "AGENT_PRIVATE_KEY, MANTLE_RPC_URL, DECISION_LOG_ADDRESS, AGENT_ID"
            )
            return None
        if dlog_addr == _ZERO_ADDRESS:
            log.info("onchain writer disabled: DECISION_LOG_ADDRESS is zero")
            return None

        try:
            agent_id = int(agent_id_raw)
        except ValueError:
            log.warning("AGENT_ID must be an int, got %r", agent_id_raw)
            return None

        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        if not w3.is_connected():
            log.warning("web3 cannot reach %s — onchain writer disabled", rpc)
            return None

        account = Account.from_key(pk)
        decision_log = w3.eth.contract(
            address=Web3.to_checksum_address(dlog_addr),
            abi=_DECISION_LOG_ABI,
        )

        oracle: Contract | None = None
        if oracle_addr and oracle_addr != _ZERO_ADDRESS:
            oracle = w3.eth.contract(
                address=Web3.to_checksum_address(oracle_addr),
                abi=_REPUTATION_ORACLE_ABI,
            )

        # Sanity: refuse to run if the EOA isn't the configured agent
        # on the DecisionLog — recordDecision is `onlyAgent` and would
        # revert every call otherwise.
        try:
            onchain_agent = decision_log.functions.agent().call()
            if onchain_agent.lower() != account.address.lower():
                log.warning(
                    "DecisionLog.agent=%s != AGENT_PRIVATE_KEY=%s — "
                    "writer disabled (every recordDecision would revert)",
                    onchain_agent,
                    account.address,
                )
                return None
        except Exception as e:
            log.warning("DecisionLog.agent() call failed: %s", e)
            return None

        log.info(
            "onchain writer ready: agent=%s decision_log=%s oracle=%s agent_id=%d",
            account.address,
            dlog_addr,
            oracle_addr or "(none)",
            agent_id,
        )
        return cls(
            w3=w3,
            account=account,
            agent_id=agent_id,
            decision_log=decision_log,
            oracle=oracle,
        )

    def _send(self, fn: Any, *, gas: int = 250_000) -> str | None:
        try:
            tx = fn.build_transaction({
                "from": self.account.address,
                "nonce": self.w3.eth.get_transaction_count(self.account.address),
                "gas": gas,
            })
            signed = self.account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = self.w3.eth.send_raw_transaction(raw)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.status != 1:
                log.warning("tx reverted: %s", tx_hash.hex())
                return None
            return tx_hash.hex()
        except ContractLogicError as e:
            log.warning("contract revert: %s", e)
            return None
        except Exception as e:
            log.warning("tx send failed: %s", e)
            return None

    def record_decision(
        self,
        decision: dict[str, Any],
        snapshot_filename: str,
    ) -> str | None:
        """Submit a `DecisionRecorded` tx. Returns tx hash hex or `None`."""
        decision_id, action_hash = derive_ids(decision, snapshot_filename)

        # Skip if already recorded — `recordDecision` reverts on
        # duplicates ("duplicate decision"), which would just spam
        # warnings on agent restarts.
        try:
            if self.decision_log.functions.exists(decision_id).call():
                log.debug(
                    "decision %s already on-chain (id=%s) — skipping",
                    snapshot_filename,
                    decision_id.hex(),
                )
                return None
        except Exception as e:
            log.warning("exists() pre-check failed: %s — attempting send anyway", e)

        ipfs_cid = str((decision.get("_meta") or {}).get("ipfs_cid") or "")
        fn = self.decision_log.functions.recordDecision(
            self.agent_id,
            decision_id,
            ipfs_cid,
            action_hash,
        )
        return self._send(fn)

    def update_reputation(self) -> str | None:
        """Submit `updateReputation()` if the oracle is wired AND
        `canUpdate()` returns true (throttle gate). Returns `None`
        otherwise — the oracle's own MIN_INTERVAL handles cadence."""
        if self.oracle is None:
            return None
        try:
            if not self.oracle.functions.canUpdate().call():
                return None
        except Exception as e:
            log.warning("canUpdate() check failed: %s", e)
            return None
        return self._send(self.oracle.functions.updateReputation())
