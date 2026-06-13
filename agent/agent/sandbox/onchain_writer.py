"""On-chain writer for `DecisionLog` + the canonical ERC-8004
`ReputationRegistry`.

After each cycle the loop calls `record_decision` so every decision the
agent emits is anchored on-chain — `(agentId, decisionId, ipfsCid,
actionHash)`. Separately (best-effort, throttled) it calls
`push_apr_reputation` so the canonical ERC-8004 ReputationRegistry sees
the agent's realized APR — attested straight from the agent EOA via
`giveFeedback`, with no vault/oracle coupling (the vUSDC-derived
`ReputationOracle` never initializes because the on-chain vault is empty;
reputation here reflects the live Bybit book — see `reputation.py`).

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
from decimal import Decimal
from typing import Any

from eth_account import Account
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError

log = logging.getLogger(__name__)

# Below this gas balance the EOA can't reliably anchor — the loop surfaces a
# low-gas alert (state-6) so the operator tops up before the audit trail
# develops gaps. MNT, override via env. CLAUDE.md keeps the prod EOA ~4 MNT.
MIN_GAS_MNT: Decimal = Decimal(os.environ.get("VAULT8004_MIN_GAS_MNT", "1.0"))

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

# Canonical ERC-8004 ReputationRegistry. `giveFeedback` is permissionless
# for a registered agentId (verified on Mantle: a call for agentId=99
# simulates clean from the agent EOA, a bogus id reverts). VALUE_DECIMALS=2
# convention: a `value` of 1234 reads as 12.34%.
_REPUTATION_REGISTRY_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "giveFeedback",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "value", "type": "int128"},
            {"name": "valueDecimals", "type": "uint8"},
            {"name": "tag1", "type": "string"},
            {"name": "tag2", "type": "string"},
            {"name": "endpoint", "type": "string"},
            {"name": "feedbackURI", "type": "string"},
            {"name": "feedbackHash", "type": "bytes32"},
        ],
        "outputs": [],
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


def derive_execution_hash(executed_actions: list[dict[str, Any]]) -> bytes:
    """`keccak256` over the canonical executed-action ledger — what the
    agent ACTUALLY did this cycle, including per-action `status`. This is
    the on-chain commitment when execution happened, replacing
    `derive_ids`' intent hash (decision venues+hedges). A partial
    failure (some actions `error`/`orphan`) yields a different hash than
    a clean batch, so an observer reading DecisionLog sees reality, not
    the LLM's plan.

    `error` strings are excluded (free-text, non-deterministic across
    transient Bybit messages); the `status` enum already distinguishes
    ok / error / orphan / skipped.
    """
    ledger = [
        {
            "kind": a.get("kind"),
            "category": a.get("category"),
            "product_id": a.get("product_id"),
            "coin": a.get("coin"),
            "amount": str(a.get("amount")) if a.get("amount") is not None else None,
            "status": a.get("status"),
        }
        for a in executed_actions
    ]
    payload = json.dumps(
        ledger, sort_keys=True, separators=(",", ":"), default=str
    )
    return Web3.keccak(text=payload)


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
        registry: Contract | None,
    ) -> None:
        self.w3 = w3
        self.account = account
        self.agent_id = agent_id
        self.decision_log = decision_log
        self.registry = registry

    @classmethod
    def from_env(cls) -> OnchainWriter | None:
        pk = os.environ.get("PRIVATE_KEY")
        rpc = os.environ.get("MANTLE_RPC_URL")
        dlog_addr = os.environ.get("DECISION_LOG_ADDRESS")
        registry_addr = os.environ.get("REGISTRY_8004")
        agent_id_raw = os.environ.get("AGENT_ID")

        if not (pk and rpc and dlog_addr and agent_id_raw):
            log.info(
                "onchain writer disabled: missing one of "
                "PRIVATE_KEY, MANTLE_RPC_URL, DECISION_LOG_ADDRESS, AGENT_ID"
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

        registry: Contract | None = None
        if registry_addr and registry_addr != _ZERO_ADDRESS:
            registry = w3.eth.contract(
                address=Web3.to_checksum_address(registry_addr),
                abi=_REPUTATION_REGISTRY_ABI,
            )

        # Sanity: refuse to run if the EOA isn't the configured agent
        # on the DecisionLog — recordDecision is `onlyAgent` and would
        # revert every call otherwise.
        try:
            onchain_agent = decision_log.functions.agent().call()
            if onchain_agent.lower() != account.address.lower():
                log.warning(
                    "DecisionLog.agent=%s != PRIVATE_KEY addr=%s — "
                    "writer disabled (every recordDecision would revert)",
                    onchain_agent,
                    account.address,
                )
                return None
        except Exception as e:
            log.warning("DecisionLog.agent() call failed: %s", e)
            return None

        log.info(
            "onchain writer ready: agent=%s decision_log=%s registry=%s agent_id=%d",
            account.address,
            dlog_addr,
            registry_addr or "(none)",
            agent_id,
        )
        return cls(
            w3=w3,
            account=account,
            agent_id=agent_id,
            decision_log=decision_log,
            registry=registry,
        )

    def _send(self, fn: Any, *, gas: int = 250_000) -> str | None:
        try:
            tx = fn.build_transaction({
                "from": self.account.address,
                # `pending`, not the default `latest` (state-7): a prior tx
                # still in the mempool already consumed `latest`'s nonce, so
                # `latest` here would reuse it and the new tx gets dropped as a
                # replacement. `pending` counts the in-flight tx and hands out
                # the next free nonce.
                "nonce": self.w3.eth.get_transaction_count(
                    self.account.address, "pending"
                ),
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
        *,
        ipfs_cid: str | None = None,
        executed_actions: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Submit a `DecisionRecorded` tx. Returns tx hash hex or `None`.

        `ipfs_cid` (explicit) wins over `decision._meta.ipfs_cid` so the
        caller can pass a freshly-pinned CID without round-tripping
        through the decision dict.

        `executed_actions` (when provided) makes `actionHash` commit to
        what was ACTUALLY executed (`derive_execution_hash`) instead of
        the intended allocation. Pass it only for live cycles that ran
        execute; for dry-run / hold (no_actions) leave it None so the
        intent hash is anchored (intent == execution in those cases).
        """
        decision_id, intent_hash = derive_ids(decision, snapshot_filename)
        action_hash = (
            derive_execution_hash(executed_actions)
            if executed_actions is not None
            else intent_hash
        )
        cid = ipfs_cid if ipfs_cid is not None else str(
            (decision.get("_meta") or {}).get("ipfs_cid") or ""
        )
        return self.anchor_prepared(decision_id, cid, action_hash)

    def anchor_prepared(
        self, decision_id: bytes, cid: str, action_hash: bytes
    ) -> str | None:
        """Send `recordDecision` for already-derived ids — the low-level
        anchor the retry queue replays without the original `executed_actions`
        (state-6). Skips if already on-chain (`recordDecision` reverts on
        duplicates, which would just spam on restarts). Returns tx hash or
        `None` (dup OR send failure — callers distinguish via
        `decision_exists`)."""
        try:
            if self.decision_log.functions.exists(decision_id).call():
                log.debug(
                    "decision id=%s already on-chain — skipping",
                    decision_id.hex(),
                )
                return None
        except Exception as e:
            log.warning("exists() pre-check failed: %s — attempting send anyway", e)

        fn = self.decision_log.functions.recordDecision(
            self.agent_id,
            decision_id,
            cid,
            action_hash,
        )
        return self._send(fn)

    def decision_exists(self, decision_id: bytes) -> bool:
        """True if `decision_id` is already anchored. Used to drop a queued
        anchor whose tx landed late, and to tell a genuine send failure (→
        enqueue) from an already-recorded skip (→ done). RPC error → False
        (conservative: treat as not-anchored so it stays queued)."""
        try:
            return bool(self.decision_log.functions.exists(decision_id).call())
        except Exception as e:
            log.warning("exists() check failed: %s", e)
            return False

    def gas_balance_mnt(self) -> Decimal | None:
        """EOA native (MNT) balance for the low-gas alert. `None` on RPC
        error — the caller then skips the alert rather than false-warning."""
        try:
            wei = self.w3.eth.get_balance(self.account.address)
            return Decimal(wei) / Decimal(10**18)
        except Exception as e:
            log.warning("gas balance read failed: %s", e)
            return None

    def push_apr_reputation(self, apr_bps: int) -> str | None:
        """Attest the agent's realized annualized APR (signed bps,
        VALUE_DECIMALS=2) to the canonical ERC-8004 ReputationRegistry via
        `giveFeedback`. Permissionless for a registered agentId — no oracle
        contract, no vUSDC coupling. The score itself is computed off-chain
        from the live equity series (`reputation.compute_realized_apr_bps`)
        and the loop throttles cadence. Returns tx hash, or `None` when no
        registry is configured / the send fails."""
        if self.registry is None:
            return None
        fn = self.registry.functions.giveFeedback(
            self.agent_id,
            int(apr_bps),
            2,  # VALUE_DECIMALS — 1234 reads as 12.34%
            "apr",
            "cumulative",
            "",
            "",
            b"\x00" * 32,
        )
        return self._send(fn)
