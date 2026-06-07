from unittest.mock import MagicMock, PropertyMock

import httpx
import pytest
from eth_account import Account
from web3 import Web3
from web3.exceptions import Web3RPCError

from agent.bybit_oracle.chain_writer import (
    ChainSendError,
    ChainWriter,
    poll_mantle_usdc_credit,
)

# Deterministic test key — burner, never to touch any chain.
TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
ATTESTOR_ADDR = Account.from_key(TEST_PRIVATE_KEY).address
CONTRACT_ADDR = "0x" + "11" * 20
ABI = [
    {
        "type": "function",
        "name": "confirmDeposit",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "txId", "type": "uint256"},
            {"name": "newAttestedBalance", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "confirmWithdraw",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "txId", "type": "uint256"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "updateBalance",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "newAttestedBalance", "type": "uint256"}],
        "outputs": [],
    },
]

TX_HASH_BYTES = bytes.fromhex("ab" * 32)
TX_HASH_HEX = TX_HASH_BYTES.hex()


def _make_w3_mock(
    *,
    estimate_gas: int = 100_000,
    nonce: int = 5,
    gas_price: int = 1_000_000,
    receipt_status: int = 1,
    estimate_raises: Exception | None = None,
    send_raises: Exception | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Returns (w3_mock, contract_fn_mock). The fn_mock records `*args`
    passed to the contract function so tests can assert what was called.
    """
    w3 = MagicMock(name="Web3")

    # `contract.functions[name](*args)` is the public API — patch it so it
    # records args and returns a fn_mock with estimate_gas / build_transaction.
    fn_mock = MagicMock(name="ContractFn")
    if estimate_raises:
        fn_mock.estimate_gas.side_effect = estimate_raises
    else:
        fn_mock.estimate_gas.return_value = estimate_gas
    fn_mock.build_transaction.return_value = {
        "from": ATTESTOR_ADDR,
        "to": CONTRACT_ADDR,
        "nonce": nonce,
        "gas": int(estimate_gas * 1.2),
        "gasPrice": gas_price,
        "chainId": 5000,
        "data": "0x",
        "value": 0,
    }

    functions_holder = MagicMock(name="ContractFunctions")
    fn_factory = MagicMock(return_value=fn_mock, name="ContractFnFactory")
    functions_holder.__getitem__.return_value = fn_factory

    contract = MagicMock(name="Contract", address=CONTRACT_ADDR)
    contract.functions = functions_holder
    w3.eth.contract.return_value = contract

    w3.eth.get_transaction_count.return_value = nonce
    # gas_price is a property in real web3 — emulate via PropertyMock.
    type(w3.eth).gas_price = PropertyMock(return_value=gas_price)

    if send_raises:
        w3.eth.send_raw_transaction.side_effect = send_raises
    else:
        w3.eth.send_raw_transaction.return_value = TX_HASH_BYTES

    w3.eth.wait_for_transaction_receipt.return_value = {
        "status": receipt_status,
        "blockNumber": 12345,
        "gasUsed": 80_000,
    }

    # Pass through Web3.to_checksum_address to the real classmethod, since
    # ChainWriter calls Web3.to_checksum_address(contract_address) directly
    # on the class, not on the instance — so the patched class isn't involved.

    return w3, fn_mock


def _make_writer(w3: MagicMock, **overrides) -> ChainWriter:
    account = Account.from_key(TEST_PRIVATE_KEY)
    return ChainWriter(
        w3=w3,
        account=account,
        contract_address=CONTRACT_ADDR,
        abi=ABI,
        chain_id=overrides.get("chain_id", 5000),
        gas_buffer=overrides.get("gas_buffer", 1.2),
        receipt_timeout=overrides.get("receipt_timeout", 30),
    )


def test_push_confirm_deposit_happy_path():
    w3, fn = _make_w3_mock()
    writer = _make_writer(w3)

    tx_hash = writer.push_confirm_deposit(tx_id=42, new_attested_balance=1_000_000)

    assert tx_hash == TX_HASH_HEX
    w3.eth.contract.return_value.functions.__getitem__.assert_called_with("confirmDeposit")
    # The factory returned by functions["confirmDeposit"] was called with the args.
    factory = w3.eth.contract.return_value.functions.__getitem__.return_value
    factory.assert_called_with(42, 1_000_000)
    fn.estimate_gas.assert_called_once_with({"from": ATTESTOR_ADDR})
    w3.eth.send_raw_transaction.assert_called_once()
    w3.eth.wait_for_transaction_receipt.assert_called_once_with(TX_HASH_BYTES, timeout=30)


def test_push_confirm_withdraw_passes_args():
    w3, _ = _make_w3_mock()
    writer = _make_writer(w3)
    writer.push_confirm_withdraw(tx_id=7, amount=500_000)
    factory = w3.eth.contract.return_value.functions.__getitem__.return_value
    factory.assert_called_with(7, 500_000)
    w3.eth.contract.return_value.functions.__getitem__.assert_called_with("confirmWithdraw")


def test_push_update_balance_passes_args():
    w3, _ = _make_w3_mock()
    writer = _make_writer(w3)
    writer.push_update_balance(new_balance=42_000_000)
    factory = w3.eth.contract.return_value.functions.__getitem__.return_value
    factory.assert_called_with(42_000_000)
    w3.eth.contract.return_value.functions.__getitem__.assert_called_with("updateBalance")


def test_gas_buffer_applied():
    """estimate 100k → built tx with gas = 120k (1.2x buffer)."""
    w3, fn = _make_w3_mock(estimate_gas=100_000)
    writer = _make_writer(w3)
    writer.push_confirm_deposit(1, 1)

    build_args = fn.build_transaction.call_args[0][0]
    assert build_args["gas"] == 120_000


def test_nonce_uses_pending_tag():
    """Pending-nonce avoids collisions if a prior tx is still in mempool."""
    w3, _ = _make_w3_mock()
    writer = _make_writer(w3)
    writer.push_confirm_deposit(1, 1)
    w3.eth.get_transaction_count.assert_called_with(ATTESTOR_ADDR, "pending")


def test_build_tx_includes_chain_id_and_gas_price():
    w3, fn = _make_w3_mock(gas_price=2_000_000, nonce=11)
    writer = _make_writer(w3, chain_id=5000)
    writer.push_confirm_deposit(1, 1)

    build_args = fn.build_transaction.call_args[0][0]
    assert build_args["chainId"] == 5000
    assert build_args["gasPrice"] == 2_000_000
    assert build_args["nonce"] == 11
    assert build_args["from"] == ATTESTOR_ADDR


def test_revert_raises_chain_send_error_not_retried():
    """status==0 receipt = on-chain revert. Same call would revert again,
    so tenacity must NOT retry. We verify by counting send_raw_transaction
    calls — exactly one.
    """
    w3, _ = _make_w3_mock(receipt_status=0)
    writer = _make_writer(w3)
    with pytest.raises(ChainSendError, match="reverted on-chain"):
        writer.push_confirm_deposit(1, 1)
    assert w3.eth.send_raw_transaction.call_count == 1


def test_estimate_gas_revert_raises_without_sending():
    """estimate_gas reverting means the contract pre-check (onlyAttestor,
    sanity floor, etc.) failed — short-circuit before broadcasting.
    """
    w3, _ = _make_w3_mock(estimate_raises=Web3RPCError("execution reverted: no pending"))
    writer = _make_writer(w3)
    with pytest.raises(ChainSendError, match="estimate_gas reverted"):
        writer.push_confirm_deposit(1, 1)
    w3.eth.send_raw_transaction.assert_not_called()


def test_transient_transport_error_retried():
    """First two attempts fail with httpx.ConnectError, third succeeds.
    Verifies tenacity retries on transport errors.
    """
    w3, _ = _make_w3_mock()
    # First two calls raise, third returns the bytes hash.
    w3.eth.send_raw_transaction.side_effect = [
        httpx.ConnectError("network blip"),
        httpx.ConnectError("network blip"),
        TX_HASH_BYTES,
    ]
    # Override the default tenacity wait — tests must not actually sleep.
    writer = _make_writer(w3)
    # Patch the underlying retry's wait to zero for this test. Retry decorator
    # now lives on `_send_on_contract` (so `transfer_usdc` shares the policy).
    writer._send_on_contract.retry.wait = lambda *_a, **_k: 0  # type: ignore[attr-defined]

    tx_hash = writer.push_confirm_deposit(1, 1)
    assert tx_hash == TX_HASH_HEX
    assert w3.eth.send_raw_transaction.call_count == 3


def test_chain_send_error_not_retried_by_tenacity():
    """Verifies that ChainSendError is NOT in the retry list — only transient
    transport errors are. A revert that returns from `_send` via raise must
    propagate after a single attempt.
    """
    w3, _ = _make_w3_mock(receipt_status=0)
    writer = _make_writer(w3)
    with pytest.raises(ChainSendError):
        writer.push_confirm_deposit(1, 1)
    # estimate_gas got called exactly once, confirming no retry loop.
    factory = w3.eth.contract.return_value.functions.__getitem__.return_value
    assert factory.call_count == 1


def test_from_settings_requires_private_key():
    from agent.bybit_oracle.config import OracleSettings

    cfg = OracleSettings(_env_file=None)
    with pytest.raises(RuntimeError, match="ATTESTOR_PRIVATE_KEY"):
        ChainWriter.from_settings(cfg=cfg)


def test_address_property_matches_account():
    w3, _ = _make_w3_mock()
    writer = _make_writer(w3)
    assert writer.address == ATTESTOR_ADDR


# --- .13b: USDC approve ----------------------------------------------------


USDC_ADDR = "0x" + "22" * 20
SPENDER = "0x" + "33" * 20


def _make_writer_with_usdc(w3: MagicMock) -> ChainWriter:
    """Variant that includes a USDC contract for transfer_usdc / approve_usdc.
    The w3.eth.contract mock returns the same fn_mock for any contract
    instance — fine because we assert on call args, not contract identity.
    """
    account = Account.from_key(TEST_PRIVATE_KEY)
    return ChainWriter(
        w3=w3,
        account=account,
        contract_address=CONTRACT_ADDR,
        abi=ABI,
        usdc_address=USDC_ADDR,
        chain_id=5000,
        gas_buffer=1.2,
        receipt_timeout=30,
    )


def test_approve_usdc_calls_approve_with_spender_and_amount():
    w3, _ = _make_w3_mock()
    writer = _make_writer_with_usdc(w3)

    tx_hash = writer.approve_usdc(spender=SPENDER, amount_micro=50_000_000)
    assert tx_hash == TX_HASH_HEX

    # The USDC contract's `functions["approve"]` was invoked with the
    # correctly checksummed spender + raw amount.
    # Since both ChainWriter contracts use the same w3.eth.contract mock,
    # we inspect the latest `functions[name]` call.
    contract_fns = w3.eth.contract.return_value.functions
    contract_fns.__getitem__.assert_called_with("approve")
    factory = contract_fns.__getitem__.return_value
    args, _ = factory.call_args
    assert args[0] == Web3.to_checksum_address(SPENDER)
    assert args[1] == 50_000_000


def test_approve_usdc_raises_without_usdc_address():
    """ChainWriter constructed without usdc_address (listener-only mode)
    must refuse approve_usdc — explicit error beats silent NoneType crash.
    """
    w3, _ = _make_w3_mock()
    writer = _make_writer(w3)  # no usdc_address
    with pytest.raises(RuntimeError, match="approve_usdc.*usdc_address"):
        writer.approve_usdc(SPENDER, 1)


# --- .13d: Mantle USDC balance polling -------------------------------------


def _w3_with_balance_sequence(balances: list[int]) -> MagicMock:
    """Build a w3 mock where `usdc_contract.functions.balanceOf(addr).call()`
    returns successive values from `balances` (last value sticks).
    """
    w3 = MagicMock(name="Web3")
    type(w3.eth).gas_price = PropertyMock(return_value=1_000_000)

    contract = MagicMock(name="Contract", address=CONTRACT_ADDR)
    fn_mock = MagicMock(name="ContractFn")
    fn_mock.estimate_gas.return_value = 100_000
    fn_mock.build_transaction.return_value = {
        "from": ATTESTOR_ADDR, "nonce": 0, "gas": 120_000,
        "gasPrice": 1_000_000, "chainId": 5000, "data": "0x", "value": 0,
    }
    contract.functions = MagicMock()
    contract.functions.__getitem__.return_value = MagicMock(return_value=fn_mock)

    # balanceOf factory: each call returns a callable whose `.call()` produces
    # the next value in `balances`.
    iter_balances = iter(balances)

    def balance_of_factory(_addr):
        node = MagicMock()
        try:
            node.call.return_value = next(iter_balances)
        except StopIteration:
            # Keep returning the last value if we run out.
            node.call.return_value = balances[-1]
        return node

    contract.functions.balanceOf = balance_of_factory
    w3.eth.contract.return_value = contract
    return w3


def test_read_usdc_balance_calls_balance_of():
    w3 = _w3_with_balance_sequence([1_000_000])
    writer = _make_writer_with_usdc(w3)
    assert writer.read_usdc_balance(ATTESTOR_ADDR) == 1_000_000


def test_read_usdc_balance_raises_without_usdc():
    w3, _ = _make_w3_mock()
    writer = _make_writer(w3)
    with pytest.raises(RuntimeError, match="read_usdc_balance.*usdc_address"):
        writer.read_usdc_balance(ATTESTOR_ADDR)


@pytest.mark.asyncio
async def test_poll_mantle_usdc_credit_returns_delta():
    """Baseline 100M, then 100M (not yet), then 150M (credit landed).
    Caller asked for min_credit=40M → returns 50M.
    """
    w3 = _w3_with_balance_sequence([100_000_000, 100_000_000, 150_000_000])
    writer = _make_writer_with_usdc(w3)

    delta = await poll_mantle_usdc_credit(
        writer=writer,
        address=ATTESTOR_ADDR,
        baseline=100_000_000,
        min_credit=40_000_000,
        interval_seconds=0,
    )
    assert delta == 50_000_000


@pytest.mark.asyncio
async def test_poll_mantle_usdc_credit_immediate():
    """First poll already shows full credit — must return without sleeping."""
    w3 = _w3_with_balance_sequence([200_000_000])
    writer = _make_writer_with_usdc(w3)
    delta = await poll_mantle_usdc_credit(
        writer=writer,
        address=ATTESTOR_ADDR,
        baseline=100_000_000,
        min_credit=50_000_000,
        interval_seconds=0,
    )
    assert delta == 100_000_000


@pytest.mark.asyncio
async def test_poll_mantle_usdc_credit_timeout():
    """Balance never grows past baseline — TimeoutError after deadline."""
    w3 = _w3_with_balance_sequence([100_000_000])  # stuck at baseline
    writer = _make_writer_with_usdc(w3)
    with pytest.raises(TimeoutError, match="mantle USDC not credited"):
        await poll_mantle_usdc_credit(
            writer=writer,
            address=ATTESTOR_ADDR,
            baseline=100_000_000,
            min_credit=10_000_000,
            timeout_seconds=0.05,
            interval_seconds=0.01,
        )


