"""Mantle on-chain signer for BybitAttestor.

Sends `confirmDeposit`, `confirmWithdraw`, `updateBalance` from a single EOA
key. Sync — wrap in `asyncio.to_thread(...)` from async handlers. Not
thread-safe; nonce reuse will collide. Callers must serialize.

Safe-multisig wiring is post-MVP — for hackathon `.15` smoke ($50 USDC),
EOA with the attestor private key is sufficient.

`tenacity` retries cover transient transport errors (RPC blip, timeout).
Reverts (`status == 0`) propagate as `ChainSendError` immediately — replaying
the same tx with the same args will revert again, so retry is the wrong
remedy. Callers should advance the FSM row to `failed` and surface to ops.
"""

from __future__ import annotations

from typing import Any

import httpx
from eth_account import Account
from eth_account.signers.local import LocalAccount
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import Web3RPCError

from .abi import load_bybit_attestor_abi
from .config import OracleSettings, settings
from .structured_log import get_logger

log = get_logger(__name__)


# Minimal ERC-20 surface — only what we need to send USDC, read balances,
# and approve the BybitAttestor contract to pull USDC in confirmWithdraw.
# Avoids pulling a full token ABI into the repo.
_ERC20_MINIMAL_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "transfer",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "approve",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


class ChainSendError(RuntimeError):
    """A tx was mined but reverted on-chain. Do NOT retry — same call will
    revert again. Caller's job to advance FSM to `failed` and alert.
    """


_TRANSIENT = (httpx.HTTPError, ConnectionError, TimeoutError)


class ChainWriter:
    def __init__(
        self,
        w3: Web3,
        account: LocalAccount,
        contract_address: str,
        abi: list[dict[str, Any]],
        usdc_address: str | None = None,
        chain_id: int = 5000,
        gas_buffer: float = 1.2,
        receipt_timeout: int = 120,
    ) -> None:
        self._w3 = w3
        self._account = account
        self._chain_id = chain_id
        self._gas_buffer = gas_buffer
        self._receipt_timeout = receipt_timeout
        self._contract: Contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract_address), abi=abi
        )
        # USDC token is optional — only the orchestrator needs it for bridging
        # USDC from the attestor wallet to Bybit. Read-only paths (just
        # confirmDeposit / confirmWithdraw / updateBalance) can leave it None.
        self._usdc: Contract | None = (
            w3.eth.contract(
                address=Web3.to_checksum_address(usdc_address),
                abi=_ERC20_MINIMAL_ABI,
            )
            if usdc_address
            else None
        )

    @classmethod
    def from_settings(
        cls,
        cfg: OracleSettings | None = None,
        w3: Web3 | None = None,
    ) -> ChainWriter:
        cfg = cfg or settings
        key = cfg.MANTLE_ATTESTOR_PRIVATE_KEY.get_secret_value()
        if not key:
            raise RuntimeError(
                "MANTLE_ATTESTOR_PRIVATE_KEY is required to push on-chain txs"
            )
        account: LocalAccount = Account.from_key(key)
        w3 = w3 or Web3(Web3.HTTPProvider(cfg.MANTLE_RPC_URL))
        return cls(
            w3=w3,
            account=account,
            contract_address=cfg.BYBIT_ATTESTOR_ADDRESS,
            abi=load_bybit_attestor_abi(),
            usdc_address=cfg.MANTLE_USDC_ADDRESS,
            chain_id=cfg.MANTLE_CHAIN_ID,
            gas_buffer=cfg.MANTLE_GAS_BUFFER,
            receipt_timeout=cfg.MANTLE_TX_RECEIPT_TIMEOUT,
        )

    @property
    def address(self) -> str:
        return self._account.address

    def push_confirm_deposit(self, tx_id: int, new_attested_balance: int) -> str:
        return self._send("confirmDeposit", tx_id, new_attested_balance)

    def push_confirm_withdraw(self, tx_id: int, amount: int) -> str:
        return self._send("confirmWithdraw", tx_id, amount)

    def push_update_balance(self, new_balance: int) -> str:
        return self._send("updateBalance", new_balance)

    def read_attested_balance(self) -> int:
        """Read `attestedBalance()` from the BybitAttestor contract.
        Used by the orchestrator to compute `newBalance = current + amount`
        for confirmDeposit's sanity check.
        """
        return int(self._contract.functions.attestedBalance().call())

    def transfer_usdc(self, to_address: str, amount_micro: int) -> str:
        """Send `amount_micro` (uint256 micro-USDC, 6 decimals) USDC from the
        attestor wallet to `to_address` on Mantle. Used by the orchestrator
        to bridge escrow-released USDC to the Bybit deposit address.

        Requires `usdc_address` to have been passed at construction; raises
        otherwise. ERC-20 transfer returns bool — wraps `_send` with a
        contract overriden to the USDC instance for this call.
        """
        if self._usdc is None:
            raise RuntimeError(
                "transfer_usdc called but ChainWriter has no usdc_address configured"
            )
        return self._send_on_contract(
            self._usdc, "transfer", Web3.to_checksum_address(to_address), amount_micro
        )

    def approve_usdc(self, spender: str, amount_micro: int) -> str:
        """Approve `spender` to pull up to `amount_micro` USDC from the
        attestor wallet. Used by the withdraw orchestrator: BybitAttestor's
        `confirmWithdraw` does `safeTransferFrom(attestor, this, amount)` —
        which only works if the attestor first approved the contract.

        Pattern: orchestrator calls approve right before confirmWithdraw,
        with the exact amount (not unlimited) so a compromised contract
        can't drain extra. Cost: one extra tx per withdraw cycle.
        """
        if self._usdc is None:
            raise RuntimeError(
                "approve_usdc called but ChainWriter has no usdc_address configured"
            )
        return self._send_on_contract(
            self._usdc,
            "approve",
            Web3.to_checksum_address(spender),
            amount_micro,
        )

    def _send(self, fn_name: str, *args: Any) -> str:
        return self._send_on_contract(self._contract, fn_name, *args)

    @retry(
        retry=retry_if_exception_type(_TRANSIENT),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def _send_on_contract(self, contract: Contract, fn_name: str, *args: Any) -> str:
        sender = self._account.address
        fn = contract.functions[fn_name](*args)

        nonce = self._w3.eth.get_transaction_count(sender, "pending")
        # estimate_gas will revert-simulate the call — catches obvious failures
        # (e.g. onlyAttestor mismatch, sanity floor) before we burn a real tx.
        try:
            gas_estimate = fn.estimate_gas({"from": sender})
        except Web3RPCError as exc:
            log.error(
                "chain_estimate_gas_reverted",
                extra={"fn": fn_name, "call_args": args, "err": str(exc)},
            )
            raise ChainSendError(f"{fn_name} estimate_gas reverted: {exc}") from exc

        gas = int(gas_estimate * self._gas_buffer)
        gas_price = self._w3.eth.gas_price
        tx = fn.build_transaction(
            {
                "from": sender,
                "nonce": nonce,
                "gas": gas,
                "gasPrice": gas_price,
                "chainId": self._chain_id,
            }
        )
        signed = self._account.sign_transaction(tx)
        tx_hash_bytes = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash = tx_hash_bytes.hex()
        log.info(
            "chain_tx_sent",
            extra={
                "fn": fn_name,
                "tx_hash": tx_hash,
                "nonce": nonce,
                "gas": gas,
                "gas_price": gas_price,
            },
        )

        receipt = self._w3.eth.wait_for_transaction_receipt(
            tx_hash_bytes, timeout=self._receipt_timeout
        )
        if receipt["status"] != 1:
            log.error(
                "chain_tx_reverted",
                extra={"fn": fn_name, "tx_hash": tx_hash, "block": receipt["blockNumber"]},
            )
            raise ChainSendError(f"{fn_name} tx reverted on-chain: {tx_hash}")

        log.info(
            "chain_tx_confirmed",
            extra={
                "fn": fn_name,
                "tx_hash": tx_hash,
                "block": receipt["blockNumber"],
                "gas_used": receipt["gasUsed"],
            },
        )
        return tx_hash
