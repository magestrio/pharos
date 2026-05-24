import asyncio
import logging
from collections.abc import Awaitable, Callable

from agent.gather.bybit import get_perp_market_data
from agent.gather.risk_context import get_risk_context
from agent.scheduler.triggers import TriggerEvaluator, utc_now


log = logging.getLogger(__name__)


OnTriggerCallback = Callable[[str], Awaitable[None]]


async def _poll_once(evaluator: TriggerEvaluator, on_trigger: OnTriggerCallback) -> None:
    risk = await get_risk_context()
    perp = await get_perp_market_data()
    now = utc_now()

    fired_reasons: list[str] = []

    if risk.usdc_peg_deviation_bps is not None:
        outcome = evaluator.evaluate_peg(risk.usdc_peg_deviation_bps, now)
        if outcome.fire:
            fired_reasons.append(outcome.reason)

    for pool, util in (
        ("aave_v3_usdc", risk.aave_v3_usdc_utilization),
        ("aave_v3_weth", risk.aave_v3_weth_utilization),
    ):
        if util is None:
            continue
        outcome = evaluator.evaluate_aave_util(pool, util, now)
        if outcome.fire:
            fired_reasons.append(outcome.reason)

    if perp.is_available:
        for venue in perp.venues:
            if venue.funding_rate_8h is None:
                continue
            outcome = evaluator.evaluate_funding(venue.symbol, venue.funding_rate_8h, now)
            if outcome.fire:
                fired_reasons.append(outcome.reason)

    if fired_reasons:
        # Bundle every signal that fired into one cycle — the consumer
        # will run at most once anyway thanks to the cycle lock, so
        # surfacing all reasons in one shot makes log readers' lives
        # easier than emitting N parallel triggers that collapse to 1.
        await on_trigger("; ".join(fired_reasons))


async def run_poller(
    evaluator: TriggerEvaluator,
    on_trigger: OnTriggerCallback,
    interval_seconds: float = 120.0,
) -> None:
    """Long-running poller. Catches and logs any exception per tick so a
    transient RPC/HTTP failure doesn't kill the whole loop."""
    log.info("poller starting (interval=%ss)", interval_seconds)
    while True:
        try:
            await _poll_once(evaluator, on_trigger)
        except Exception:
            log.exception("poller tick failed")
        await asyncio.sleep(interval_seconds)
