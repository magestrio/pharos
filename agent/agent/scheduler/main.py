import asyncio
import logging

from agent.loop import _run_cycle_async
from agent.scheduler.poller import run_poller
from agent.scheduler.triggers import TriggerEvaluator, utc_now


log = logging.getLogger(__name__)


# 4-hour heartbeat. Catches anything the poller misses (e.g. silent
# event-source outage that never triggers a delta).
HEARTBEAT_SECONDS = 4 * 60 * 60

# Poller cadence. Funding settles every 8h on Bybit; Aave utilization
# evolves slowly. 120s is fast enough to react and slow enough to be
# polite to free-tier RPC/HTTP endpoints.
POLL_INTERVAL_SECONDS = 120


async def main_loop() -> None:
    evaluator = TriggerEvaluator()
    cycle_lock = asyncio.Lock()

    async def safe_run_cycle(reason: str) -> None:
        # Lock serializes cron + poller-triggered cycles so we never run
        # two `_run_cycle_async` in parallel (would race on memory,
        # nonce, IPFS, tx). A cycle already in flight when a second
        # trigger fires just queues behind it.
        async with cycle_lock:
            log.info("cycle starting (%s)", reason)
            try:
                await _run_cycle_async()
                evaluator.mark_decision_taken(utc_now())
                log.info("cycle completed")
            except Exception:
                log.exception("cycle failed")

    async def heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await safe_run_cycle("heartbeat")

    poller_task = asyncio.create_task(
        run_poller(evaluator, safe_run_cycle, POLL_INTERVAL_SECONDS),
        name="poller",
    )
    heartbeat_task = asyncio.create_task(heartbeat_loop(), name="heartbeat")

    log.info("vault8004 agent loop started")
    done, pending = await asyncio.wait(
        {poller_task, heartbeat_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    # If either task exits, the whole loop is unhealthy — surface and
    # let the process supervisor (systemd/k8s/etc) restart us cleanly.
    for task in pending:
        task.cancel()
    for task in done:
        task.result()  # re-raise on failure
