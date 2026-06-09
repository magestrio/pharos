"""Action data types for the execution layer (ah.25 split).

Pure data definitions with no execute-layer dependencies — the leaf of the
`execute` package import DAG. Every other submodule may import from here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class ActionKind(StrEnum):
    SUBSCRIBE_EARN = "subscribe_earn"
    REDEEM_EARN = "redeem_earn"
    SUBSCRIBE_ADVANCE_EARN = "subscribe_advance_earn"
    SUBSCRIBE_LM = "subscribe_lm"
    REDEEM_LM = "redeem_lm"
    CLAIM_LM = "claim_lm"
    OPEN_PERP_SHORT = "open_perp_short"
    CLOSE_PERP = "close_perp"
    SWAP_SPOT = "swap_spot"
    ALPHA_PURCHASE = "alpha_purchase"
    ALPHA_REDEEM = "alpha_redeem"
    # Funding-carry compound actions (`bybit-strategy-expansion.5`).
    # OPEN dispatches: set_leverage(1) → spot Buy → paired-notional
    # check → perp Sell, atomic-pair guard between legs. CLOSE: spot
    # Sell → perp Buy reduce-only, same guard.
    OPEN_FUNDING_CARRY = "open_funding_carry"
    CLOSE_FUNDING_CARRY = "close_funding_carry"
    SKIP_OUT_OF_SCOPE = "skip_out_of_scope"


@dataclass
class Action:
    """One planned executor step. `amount` is in the product's coin
    (treated as USD-equivalent under `_STABLES`); `order_link_id`
    encodes the snapshot timestamp + sequence index for Bybit-side
    idempotency.

    `position_id` is populated only for REDEEM_LM actions — Bybit's
    remove-liquidity endpoint addresses a specific LP position by its
    server-side id (`/v5/earn/liquidity-mining/position.positionId`),
    not by product, since one product can host multiple positions
    (e.g. opened in different cycles). Other kinds leave it `None`.
    """

    kind: ActionKind
    category: str
    product_id: str
    coin: str
    amount: Decimal
    order_link_id: str
    reason: str
    position_id: str | None = None
    # Spot-swap side. "Sell" (default) is the legacy USDC→stable flow
    # where we sell USDC (base) for a stable quote (USDCUSDT,
    # USDCUSD1). "Buy" is for non-stable Earn picks where we acquire
    # the target coin via {coin}USDT pair, paying USDT (quote). Field
    # is ignored for non-SWAP_SPOT kinds.
    side: str = "Sell"
    # Native-coin amount, populated only when `amount` (USD) and the
    # native-coin units differ. Non-stable SUBSCRIBE_EARN/_LM picks
    # set this to USD / mark_price so the dispatch can pass the right
    # units to Bybit's place_earn_order (which always expects native
    # coin amount, never USD).
    amount_native: Decimal | None = None
    # Per-action overrides for dispatch parameters that don't fit the
    # flat field set. Currently used by REDEEM_LM to carry
    # `remove_rate` (1-100) for partial exits; default behavior when
    # absent is the full-exit path (remove_rate=100).
    extra: dict[str, Any] = field(default_factory=dict)

    def to_log(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["amount"] = str(self.amount)
        if self.amount_native is not None:
            d["amount_native"] = str(self.amount_native)
        return d


@dataclass
class ActionResult:
    action: Action
    status: str  # "dry-run" | "ok" | "skipped" | "error"
    response: dict[str, Any] | None = None
    error: str | None = None
    started_at: str = ""
    finished_at: str = ""

    def to_log(self) -> dict[str, Any]:
        return {
            "action": self.action.to_log(),
            "status": self.status,
            "response": self.response,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class _CurrentPos:
    coin: str
    amount_usd: Decimal
    # Native-coin balance (e.g. 4.9005 LIT). Distinct from `amount_usd`
    # because non-stable positions whose perp mark goes missing (Bybit
    # delisted, snapshot's perp_market fan-out budget exhausted, etc.)
    # silently collapse to amount_usd=0 — the diff layer needs the
    # native value to still emit a REDEEM and avoid naked spot exposure.
    amount_native: Decimal = Decimal(0)
    # Redeemable (non-`Processing`) portion of the position. `None` means
    # "not computed" (Alpha / carry / legacy callers) → treat as fully
    # redeemable. OnChain stakes in `Processing` status (≈4 days after a
    # fresh subscribe) CANNOT be redeemed — `place-order` Redeem reverts
    # retCode=180020 — so the diff must not emit a REDEEM for a position
    # whose entire balance is still Processing.
    redeemable_native: Decimal | None = None
    redeemable_usd: Decimal | None = None


@dataclass
class _TargetPos:
    coin: str
    amount_usd: Decimal
