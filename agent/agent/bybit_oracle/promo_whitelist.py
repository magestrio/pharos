"""Manual whitelist of Bybit Earn products whose effective APR is NOT
reachable via the public OpenAPI surface.

Why this exists: Bybit promo subsidies (e.g. WLFI rewards on top of USD1
Flexible Saving) are advertised in the UI as a single headline APR but
the API only exposes the underlying base APR — via `estimateApr` AND via
`/v5/earn/apr-history`. Phase A.3 / A.18 live probes confirmed this:
USD1 productId 1131 returns `estimateApr=0.65%` and apr-history reports
a flat 0.5% even though the UI quotes 7.52% under an active "Hold USD1
Earn WLFI" promotion. `bonusEvents` on the product payload is empty for
this case — i.e. there is no machine-readable promo channel for it.

The snapshot collector (.6) calls `get_promo_effective_apr(category,
product_id)` for every candidate and, when a hit is returned, ranks the
product on the whitelist APR instead of the base APR. The whitelist is
**manual and frozen** — the assumption is that a human operator updates
it from the UI before each demo / live cycle (decision documented in
.21 task line — UI scraping was explicitly out of scope for hackathon).

When the cycle's age vs `last_checked` exceeds the configured staleness
threshold the collector should warn loudly — a stale whitelist entry
that under- or over-reports promo APR will mislead the LLM ranker.

To extend: add an entry to `PROMO_OVERRIDES` below. Each entry needs the
URL of the announcement / product page used to source the APR so a
later operator can re-verify before demo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class PromoOverride:
    """One entry in the manual promo-APR whitelist.

    `effective_apr` is the headline rate from the UI as a Decimal in
    fractional form — i.e. `Decimal("0.0752")` for 7.52%, NOT `7.52`.
    Keep this convention strict so the ranker can multiply directly
    without unit conversion.
    """

    coin: str
    category: str  # "FlexibleSaving" | "OnChain"
    product_id: str
    effective_apr: Decimal
    source_url: str  # announcement / product-page URL the APR was read from
    last_checked: date
    note: str = ""


# Hackathon demo whitelist. Update before each demo by walking the UI:
#   https://www.bybit.com/en/earn → Flexible Saving → look for products
#   with a promo banner (e.g. "Hold X, Earn Y"). Capture the headline
#   APR shown in the product card, NOT the `estimateApr` from the API.
PROMO_OVERRIDES: tuple[PromoOverride, ...] = (
    PromoOverride(
        coin="USD1",
        category="FlexibleSaving",
        product_id="1131",
        effective_apr=Decimal("0.0752"),
        source_url="https://www.bybit.com/en/earn",
        last_checked=date(2026, 5, 27),
        note=(
            "API estimateApr=0.65%, apr-history flat 0.5%. UI shows 7.52% "
            "under 'Hold USD1, Earn WLFI' campaign. bonusEvents on the "
            "product payload is empty — no machine-readable channel."
        ),
    ),
    # TODO before demo: add up to 4 more (BYUSDT, partner-token Earn,
    # whatever is on the UI banner that day). Re-verify last_checked
    # within 7 days of demo.
)


_OVERRIDES_BY_KEY: dict[tuple[str, str], PromoOverride] = {
    (entry.category, entry.product_id): entry for entry in PROMO_OVERRIDES
}


def get_promo_effective_apr(category: str, product_id: str) -> Decimal | None:
    """Return the manually-curated effective APR for a whitelisted Earn
    product, or `None` if the product is not on the whitelist (in which
    case the caller should fall back to the API-reported APR).
    """
    entry = _OVERRIDES_BY_KEY.get((category, product_id))
    return entry.effective_apr if entry else None


def get_promo_override(category: str, product_id: str) -> PromoOverride | None:
    """Return the full `PromoOverride` entry (including source + age)
    for debug / logging contexts that need to surface why a product's
    effective APR differs from its API APR.
    """
    return _OVERRIDES_BY_KEY.get((category, product_id))
