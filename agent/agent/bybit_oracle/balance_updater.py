"""Periodic balance attestation push (.14).

Polls Bybit on a short interval, computes the USDC equivalent of all
positions held by the attestor (wallet + Earn), and pushes `updateBalance`
on-chain when:

  - drift vs on-chain `attestedBalance` exceeds `threshold_bps` (default
    0.1%, 10 basis points), OR
  - more than `max_age_seconds` has passed since the last successful push
    (default 5 min) — guarantees observable freshness even without drift.

**MVP scope**: only USDC. Wallet USDC + USDC Earn positions are summed.
Multi-asset support (volatile spot positions × current price + hedge P&L)
is dead code at this layer because `FlexibleUsdcPicker` only stakes USDC.
Volatile pricing + hedge accounting attach here when those land.

**Contract bounds** (`.7`): `updateBalance` reverts if the new value
deviates more than +10% / -5% from the current `attestedBalance`. The
estimate-gas pre-check in `chain_writer._send_on_contract` catches this
before broadcast — `ChainSendError` is logged and the loop continues, no
process exit. Repeated bound violations are an ops signal (compromise,
not a transient).
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from .bybit_client import BybitClient, WalletAccount
from .chain_writer import ChainSendError, ChainWriter
from .config import OracleSettings, settings
from .structured_log import get_logger

log = get_logger(__name__)


_USDC_DECIMALS = Decimal(10) ** 6


def _sum_usdc_wallet(accounts: list[WalletAccount]) -> Decimal:
    total = Decimal(0)
    for account in accounts:
        for coin in account.coin:
            if coin.coin == "USDC":
                total += Decimal(coin.walletBalance)
    return total


def _decimal_to_micro(value: Decimal) -> int:
    return int(value * _USDC_DECIMALS)


class BalanceUpdater:
    def __init__(
        self,
        chain_writer: ChainWriter,
        bybit_client: BybitClient,
        cfg: OracleSettings | None = None,
    ) -> None:
        cfg = cfg or settings
        self._chain = chain_writer
        self._bybit = bybit_client
        self._interval = cfg.BALANCE_POLL_INTERVAL_SECONDS
        self._threshold_bps = cfg.BALANCE_THRESHOLD_BPS
        self._max_age = cfg.BALANCE_MAX_AGE_SECONDS
        # Initialize to 0 so the first iteration's "age check" trips and we
        # force an initial push (assuming attested > 0).
        self._last_push_ts: float = 0.0

    async def compute_attested_usdc(self) -> Decimal:
        """Sum USDC across spot wallet (all account types) + active Earn
        positions. Returns total in human-readable USDC (Decimal).

        MVP-only — for multi-asset, sum volatile positions × spot price
        + open perp hedge P&L. Hook here when those land.
        """
        wallet_accounts = await self._bybit.get_wallet_balance(coin="USDC")
        wallet_usdc = _sum_usdc_wallet(wallet_accounts)

        earn_positions = await self._bybit.get_earn_positions()
        earn_usdc = sum(
            (Decimal(p.amount) for p in earn_positions if p.coin == "USDC"),
            Decimal(0),
        )

        total = wallet_usdc + earn_usdc
        log.info(
            "balance_computed",
            extra={
                "wallet_usdc": str(wallet_usdc),
                "earn_usdc": str(earn_usdc),
                "total_usdc": str(total),
            },
        )
        return total

    def _should_push(
        self, current_micro: int, computed_micro: int, now: float
    ) -> bool:
        """Drift > threshold OR age > max_age."""
        delta = abs(computed_micro - current_micro)
        threshold_micro = current_micro * self._threshold_bps // 10000
        age = now - self._last_push_ts
        return delta > threshold_micro or age > self._max_age

    async def maybe_push(self) -> bool:
        """One iteration of the cron: compute → compare → push if needed.
        Returns True if a push was attempted (regardless of outcome), False
        if skipped (no prior attestation, or below threshold + within age).

        Exceptions never propagate — the loop must survive transient RPC /
        Bybit blips. Critical failures (e.g. invalid creds at startup) will
        re-fire on each iteration until ops intervenes.
        """
        try:
            current_micro = await asyncio.to_thread(self._chain.read_attested_balance)
        except Exception:
            log.exception("balance_read_attested_failed")
            return False

        if current_micro == 0:
            # Contract requires a prior `confirmDeposit` to seed attestedBalance.
            # Until the first deposit cycle completes, updateBalance reverts
            # ("no prior balance") — skip silently.
            log.info("balance_skip_no_prior_attested")
            return False

        try:
            computed_usdc = await self.compute_attested_usdc()
        except Exception:
            log.exception("balance_compute_failed")
            return False
        computed_micro = _decimal_to_micro(computed_usdc)

        now = time.time()
        if not self._should_push(current_micro, computed_micro, now):
            log.info(
                "balance_no_push",
                extra={
                    "current_micro": current_micro,
                    "computed_micro": computed_micro,
                    "age_seconds": now - self._last_push_ts,
                },
            )
            return False

        try:
            tx_hash = await asyncio.to_thread(
                self._chain.push_update_balance, computed_micro
            )
        except ChainSendError:
            # Most likely cause: contract bounds rejection (>+10% / <-5%).
            # Log and keep looping — bounds violations are operator-level.
            log.exception(
                "balance_push_rejected",
                extra={
                    "current_micro": current_micro,
                    "computed_micro": computed_micro,
                },
            )
            return True
        except Exception:
            log.exception("balance_push_failed")
            return True

        self._last_push_ts = now
        log.info(
            "balance_pushed",
            extra={
                "tx_hash": tx_hash,
                "from_micro": current_micro,
                "to_micro": computed_micro,
            },
        )
        return True

    async def run_loop(self) -> None:
        """Forever loop. Cancel via task.cancel() to stop."""
        log.info(
            "balance_updater_started",
            extra={
                "interval_seconds": self._interval,
                "threshold_bps": self._threshold_bps,
                "max_age_seconds": self._max_age,
            },
        )
        while True:
            await self.maybe_push()
            await asyncio.sleep(self._interval)
