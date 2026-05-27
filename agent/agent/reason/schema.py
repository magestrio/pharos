"""Decision schema for the Vault8004 agent.

Extensible venue model: a `Decision` carries a list of `VenueAllocation`
entries summing to 1.0 of the book. Each venue has a registered id from
`VENUE_REGISTRY` (in `agent.reason.venues`), an optional list of product
picks (when the venue is a curated category like Bybit Earn FlexibleSaving),
and an optional list of perp `Hedge` orders that delta-neutralize non-USD
picks.

Adding a new venue (another DeFi protocol, another Bybit category):
    1. Add a new id + metadata in `agent.reason.venues.VENUE_REGISTRY`
    2. Update the snapshot generator to feed APR / risk metadata for it
    3. The validator and prompt pick it up automatically

The schema is INTENTIONALLY decoupled from the old
TargetAllocation / BybitSubAllocation pair (now in `schema_legacy.py`),
which is kept around for any caller that hasn't migrated yet.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.reason.venues import VENUE_REGISTRY, VenueId


class Pick(BaseModel):
    """One product / pool inside a venue.

    `weight` is the share WITHIN the venue ([0, 1]). The effective share
    of the total book held in this pick is `venue.weight × pick.weight`.
    Bybit Earn ranker categories (Flex / OnChain / LM) require picks
    when the venue is non-zero; non-pickable venues (cash, Aave V3 USDC
    as single pool) leave the list empty.
    """

    model_config = ConfigDict(extra="ignore")

    product_id: str
    weight: float = Field(ge=0, le=1)
    notes: list[str] = Field(default_factory=list)


class VenueAllocation(BaseModel):
    """One row of the top-level allocation.

    `weight` is the share of the TOTAL book ([0, 1]) parked in this venue.
    `picks` is optional and only meaningful for venues that aggregate
    multiple products; the validator pulls the "requires_picks" flag from
    `VENUE_REGISTRY`.
    """

    model_config = ConfigDict(extra="ignore")

    venue_id: VenueId
    weight: float = Field(ge=0, le=1)
    picks: list[Pick] = Field(default_factory=list)

    @model_validator(mode="after")
    def _picks_sum_to_one_when_present(self) -> "VenueAllocation":
        if not self.picks:
            return self
        total = sum(p.weight for p in self.picks)
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"venue {self.venue_id} picks sum to {total:.4f}, expected 1.0 ± 0.001"
            )
        return self


class Hedge(BaseModel):
    """A perp hedge order paired with non-USD Earn exposure.

    `coin` matches the perp's underlying (e.g. `TON` ⇒ `TONUSDT`).
    `notional_usd` is the absolute USD size of the perp leg; direction
    is encoded in the sign: positive = long, negative = short. The agent
    is expected to short-hedge non-USD Earn picks so the combined
    position is delta-neutral; the validator does not enforce sizing
    today (planned: cross-check vs picks once snapshot carries the
    USD-equivalent of each pick).
    """

    model_config = ConfigDict(extra="ignore")

    coin: str
    notional_usd: float
    notes: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    """One cycle's allocation decision.

    `venues[].weight` sums to 1.0 across all entries (cash + every
    active venue). `hedges` is empty when the picks are USD-denominated
    (cash, USDC FlexibleSaving) and grows for non-USD picks. `thesis`
    is the operator-facing rationale; `confidence` and `risk_flags` are
    the agent's self-reported abort signals (validator gates on both).
    """

    model_config = ConfigDict(extra="ignore")

    thesis: str = Field(min_length=20)
    venues: list[VenueAllocation]
    hedges: list[Hedge] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    risk_flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    expected_blended_apr_pct: float = Field(ge=0)

    @model_validator(mode="after")
    def _venues_well_formed(self) -> "Decision":
        ids = [v.venue_id for v in self.venues]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate venue_id in venues: {ids}")
        unknown = [vid for vid in ids if vid not in VENUE_REGISTRY]
        if unknown:
            raise ValueError(
                f"venue_id(s) not in VENUE_REGISTRY: {unknown} "
                f"(known: {sorted(VENUE_REGISTRY)})"
            )
        total = sum(v.weight for v in self.venues)
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"venue weights sum to {total:.4f}, expected 1.0 ± 0.001"
            )
        return self

    def venue(self, venue_id: VenueId) -> VenueAllocation | None:
        """Lookup helper for the validator and downstream consumers."""
        for v in self.venues:
            if v.venue_id == venue_id:
                return v
        return None
