from decimal import Decimal

import pytest

from agent.bybit_oracle.hedge import NullHedgeTrigger


@pytest.mark.asyncio
async def test_null_hedge_always_skips():
    """Stub MUST return 'skipped' for every input — orchestrator routes on
    this string. A regression that returns anything else would silently
    advance the FSM to HEDGED without an actual hedge being open.
    """
    trigger = NullHedgeTrigger()
    assert await trigger.maybe_trigger("USDC", Decimal("100")) == "skipped"
    assert await trigger.maybe_trigger("ETH", Decimal("0.025")) == "skipped"
    assert await trigger.maybe_trigger("BTC", Decimal("0.001")) == "skipped"
