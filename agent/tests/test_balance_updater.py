from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.bybit_oracle.balance_updater import BalanceUpdater
from agent.bybit_oracle.bybit_client import EarnPosition, WalletAccount, WalletCoin
from agent.bybit_oracle.chain_writer import ChainSendError
from agent.bybit_oracle.config import OracleSettings


def _cfg(**overrides) -> OracleSettings:
    defaults = {
        "BALANCE_POLL_INTERVAL_SECONDS": 60.0,
        "BALANCE_THRESHOLD_BPS": 10,
        "BALANCE_MAX_AGE_SECONDS": 300.0,
    }
    defaults.update(overrides)
    return OracleSettings(_env_file=None, **defaults)


def _wallet(usdc_balance: str | None) -> list[WalletAccount]:
    if usdc_balance is None:
        return [WalletAccount(accountType="UNIFIED", coin=[])]
    return [
        WalletAccount(
            accountType="UNIFIED",
            coin=[WalletCoin(coin="USDC", walletBalance=usdc_balance)],
        )
    ]


def _earn(usdc_amount: str | None) -> list[EarnPosition]:
    if usdc_amount is None:
        return []
    return [
        EarnPosition(
            productId="p1", coin="USDC", amount=usdc_amount, category="FlexibleSaving"
        )
    ]


def _make_updater(
    *,
    current_attested_micro: int = 100_000_000,  # 100 USDC
    wallet_usdc: str = "1.0",
    earn_usdc: str = "99.0",
    cfg: OracleSettings | None = None,
):
    chain = MagicMock(name="ChainWriter")
    chain.read_attested_balance.return_value = current_attested_micro
    chain.push_update_balance.return_value = "0xupdate-tx"

    bybit = AsyncMock(name="BybitClient")
    bybit.get_wallet_balance.return_value = _wallet(wallet_usdc)
    bybit.get_earn_positions.return_value = _earn(earn_usdc)

    return BalanceUpdater(chain_writer=chain, bybit_client=bybit, cfg=cfg or _cfg()), chain, bybit


@pytest.mark.asyncio
async def test_compute_sums_wallet_plus_earn_usdc():
    updater, _chain, _bybit = _make_updater(wallet_usdc="5.5", earn_usdc="94.5")
    total = await updater.compute_attested_usdc()
    assert total == Decimal("100.0")


@pytest.mark.asyncio
async def test_compute_ignores_non_usdc_coins_in_wallet():
    """Non-USDC coins must not pollute the USDC total (volatile pricing
    happens at a different layer — see module docstring).
    """
    updater, _chain, bybit = _make_updater()
    bybit.get_wallet_balance.return_value = [
        WalletAccount(
            accountType="UNIFIED",
            coin=[
                WalletCoin(coin="USDC", walletBalance="10.0"),
                WalletCoin(coin="ETH", walletBalance="0.5"),  # ignored
            ],
        )
    ]
    bybit.get_earn_positions.return_value = []
    assert await updater.compute_attested_usdc() == Decimal("10.0")


@pytest.mark.asyncio
async def test_compute_ignores_non_usdc_earn_positions():
    updater, _chain, bybit = _make_updater()
    bybit.get_wallet_balance.return_value = _wallet("0")
    bybit.get_earn_positions.return_value = [
        EarnPosition(productId="p1", coin="USDC", amount="50", category="x"),
        EarnPosition(productId="p2", coin="ETH", amount="0.1", category="x"),  # ignored
    ]
    assert await updater.compute_attested_usdc() == Decimal("50")


@pytest.mark.asyncio
async def test_skip_push_when_no_prior_attested():
    """attestedBalance=0 means no deposit has ever been confirmed — contract
    rejects updateBalance with "no prior balance". Skip silently.
    """
    updater, chain, _ = _make_updater(current_attested_micro=0)
    assert await updater.maybe_push() is False
    chain.push_update_balance.assert_not_called()


@pytest.mark.asyncio
async def test_skip_push_when_delta_below_threshold_and_age_within_max():
    """100 USDC attested, computed 100.05 → 0.05% drift, BPS threshold 10
    (= 0.1%) → no push. Last push timestamp is now to keep age within max.
    """
    updater, chain, _ = _make_updater(
        current_attested_micro=100_000_000,  # 100 USDC
        wallet_usdc="0.05",  # 0.05 USDC
        earn_usdc="100.0",
    )
    # Total = 100.05 → 50bps deviation? Let me recompute: delta = 50_000 micro,
    # current = 100_000_000 micro, threshold = 100_000_000 * 10 / 10000 = 100_000 micro.
    # delta (50_000) < threshold (100_000) → no push.
    import time as _time
    updater._last_push_ts = _time.time()  # fresh push, age within max

    assert await updater.maybe_push() is False
    chain.push_update_balance.assert_not_called()


@pytest.mark.asyncio
async def test_push_when_delta_exceeds_threshold():
    """Computed differs by 1% from attested → push (threshold 0.1%)."""
    updater, chain, _ = _make_updater(
        current_attested_micro=100_000_000,
        wallet_usdc="0",
        earn_usdc="101.0",  # 101 USDC vs 100 attested → 1% drift
    )
    import time as _time
    updater._last_push_ts = _time.time()  # not aged

    assert await updater.maybe_push() is True
    chain.push_update_balance.assert_called_once_with(101_000_000)


@pytest.mark.asyncio
async def test_push_when_age_exceeds_max_even_without_delta():
    """Even with zero drift, force push if max_age elapsed — guarantees
    observable freshness even on perfectly-still positions.
    """
    updater, chain, _ = _make_updater(
        current_attested_micro=100_000_000,
        wallet_usdc="0",
        earn_usdc="100.0",  # exactly equal to attested
        cfg=_cfg(BALANCE_MAX_AGE_SECONDS=0.1),  # very short max_age
    )
    # _last_push_ts left at 0 (init) → age is huge → trip the age check.
    assert await updater.maybe_push() is True
    chain.push_update_balance.assert_called_once_with(100_000_000)


@pytest.mark.asyncio
async def test_push_records_timestamp_on_success():
    updater, _chain, _ = _make_updater()
    import time as _time
    before = _time.time()
    await updater.maybe_push()
    assert updater._last_push_ts >= before


@pytest.mark.asyncio
async def test_chain_send_error_logged_loop_survives():
    """Bounds rejection (>+10% / <-5%) raises ChainSendError. Must not bubble
    — loop continues, ts NOT updated so next iteration retries.
    """
    updater, chain, _ = _make_updater(
        current_attested_micro=100_000_000,
        wallet_usdc="0",
        earn_usdc="500.0",  # +400% — way over +10% bound
    )
    chain.push_update_balance.side_effect = ChainSendError("bounds violated")

    initial_ts = updater._last_push_ts
    # No exception propagates.
    assert await updater.maybe_push() is True  # attempted
    assert updater._last_push_ts == initial_ts  # ts NOT updated on failure


@pytest.mark.asyncio
async def test_read_attested_failure_does_not_crash_loop():
    updater, chain, _bybit = _make_updater()
    chain.read_attested_balance.side_effect = RuntimeError("rpc blip")
    # Must not propagate.
    assert await updater.maybe_push() is False


@pytest.mark.asyncio
async def test_compute_failure_does_not_crash_loop():
    updater, _chain, bybit = _make_updater()
    bybit.get_wallet_balance.side_effect = RuntimeError("bybit down")
    assert await updater.maybe_push() is False


@pytest.mark.asyncio
async def test_should_push_at_exact_threshold_boundary():
    """Threshold check uses strict `>`, so a delta exactly equal to the
    threshold should NOT push (matches the inequality we documented).
    """
    updater, _chain, _ = _make_updater(cfg=_cfg(BALANCE_THRESHOLD_BPS=10))
    import time as _time
    updater._last_push_ts = _time.time()
    # current=10_000_000_000 (10k USDC), threshold = 10_000_000 (0.1%)
    # Computed at exactly current + threshold → delta == threshold → no push.
    assert (
        updater._should_push(
            current_micro=10_000_000_000,
            computed_micro=10_010_000_000,
            now=_time.time(),
        )
        is False
    )
    # Computed one micro above → push.
    assert (
        updater._should_push(
            current_micro=10_000_000_000,
            computed_micro=10_010_000_001,
            now=_time.time(),
        )
        is True
    )
