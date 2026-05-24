"""Hedge-engine integration point for the deposit orchestrator.

When a volatile asset (ETH, BTC, …) is staked on Bybit Earn, the vault's
attestedBalance becomes exposed to spot price moves. The sibling `hedge-engine`
epic opens an offsetting short perp on Bybit to neutralize this delta —
the staked asset still earns yield, but P&L from price moves is offset by
the perp short.

That sibling epic isn't built yet (`#blocked-by:hedge-engine`). To keep the
deposit orchestrator complete, this module ships only the contract surface
plus a `NullHedgeTrigger` that always reports "skipped". When hedge-engine
arrives, a `RealHedgeTrigger` implements the same protocol; orchestrator
wiring in `main.py` swaps the implementation without changes elsewhere.

Skipping is the right default for the MVP `.15` smoke — `FlexibleUsdcPicker`
only stakes USDC, which has no spot delta to hedge.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol

from .structured_log import get_logger

log = get_logger(__name__)

HedgeOutcome = Literal["hedged", "skipped"]
HedgeCloseOutcome = Literal["closed", "skipped"]


class HedgeTrigger(Protocol):
    async def maybe_trigger(self, coin: str, amount: Decimal) -> HedgeOutcome: ...

    async def maybe_close(self, coin: str, amount: Decimal) -> HedgeCloseOutcome: ...


class NullHedgeTrigger:
    """Always skips. Logs the call so it's visible when a real hedge would
    have fired in production.
    """

    async def maybe_trigger(self, coin: str, amount: Decimal) -> HedgeOutcome:
        log.info(
            "hedge_skipped_stub",
            extra={
                "coin": coin,
                "amount": str(amount),
                "reason": "hedge-engine not yet implemented",
            },
        )
        return "skipped"

    async def maybe_close(self, coin: str, amount: Decimal) -> HedgeCloseOutcome:
        log.info(
            "hedge_close_skipped_stub",
            extra={
                "coin": coin,
                "amount": str(amount),
                "reason": "hedge-engine not yet implemented",
            },
        )
        return "skipped"
