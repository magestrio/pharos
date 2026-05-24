"""Earn product selection policy.

Splits the "which Earn product to stake into" decision out of the orchestrator
so it can evolve independently: the MVP picker is deterministic (Flexible
Saving USDC, highest APR); the hackathon-pitch picker will be LLM-driven and
will weigh risk score, perp hedge availability, funding rate trend, etc.

Both implementations expose the same surface so the orchestrator can swap
them by config without changes:

    picker = FlexibleUsdcPicker()  # or LlmPicker(...) later
    picked = await picker.pick(bybit_client)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

from .bybit_client import BybitClient, EarnProduct
from .structured_log import get_logger

log = get_logger(__name__)


class NoProductAvailable(RuntimeError):
    """Raised when the picker can't find any acceptable Earn product.

    Permanent for this cycle — the orchestrator advances the FSM row to
    `failed` and surfaces. NOT a transient: retrying the same picker against
    the same Bybit account state will return the same empty set.
    """


@dataclass(frozen=True)
class PickedProduct:
    product_id: str
    target_coin: str  # asset the Earn product accepts; orchestrator swaps USDC → this
    estimated_apr: Decimal  # zero if Bybit didn't report one — caller decides if acceptable


class ProductPicker(Protocol):
    async def pick(self, client: BybitClient) -> PickedProduct: ...


def _apr(product: EarnProduct) -> Decimal:
    """Parse Bybit's `estimateApr` string (e.g. "4.5") as Decimal. Missing,
    empty, or malformed APRs sort to the bottom by returning 0 — we'd rather
    pick a known-rate product over an opaque one.
    """
    raw = product.estimateApr
    if not raw:
        return Decimal(0)
    try:
        return Decimal(raw)
    except InvalidOperation:
        log.warning(
            "earn_product_apr_unparseable",
            extra={"product_id": product.productId, "raw_apr": raw},
        )
        return Decimal(0)


class FlexibleUsdcPicker:
    """MVP: highest-APR Flexible Saving USDC product. No risk model, no
    LLM — deterministic to make `.15` smoke reproducible.

    The Bybit Flexible Saving category has principal protection and zero
    redeem lockup, which is the only profile that lets the vault's async
    withdraw stay sub-minute. Other categories (Fixed, Dual Asset) have
    lockup windows that would break `.13`'s withdraw SLO.
    """

    CATEGORY = "FlexibleSaving"
    COIN = "USDC"

    async def pick(self, client: BybitClient) -> PickedProduct:
        products = await client.list_earn_products(category=self.CATEGORY, coin=self.COIN)
        if not products:
            raise NoProductAvailable(
                f"no {self.CATEGORY} products for {self.COIN} — check Bybit account "
                f"region/whitelist for Earn access"
            )
        top = max(products, key=_apr)
        chosen = PickedProduct(
            product_id=top.productId,
            target_coin=top.coin,
            estimated_apr=_apr(top),
        )
        log.info(
            "earn_product_picked",
            extra={
                "product_id": chosen.product_id,
                "coin": chosen.target_coin,
                "apr": str(chosen.estimated_apr),
                "candidates": len(products),
            },
        )
        return chosen
