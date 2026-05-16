import asyncio

from agent.gather.vault_state import get_vault_state
from agent.gather.market_data import get_market_data
from agent.gather.allora import get_allora_signals
from agent.gather.risk_metrics import get_risk_metrics
from agent.reason.client import reason
from agent.validate.rules import validate
from agent.execute.builders import build_allocation_calls
from agent.execute.ipfs import upload_rationale
from agent.execute.tx import execute_on_chain
import agent.memory as memory


async def _run_cycle_async() -> None:
    vault_state = await get_vault_state()
    market_data = await get_market_data()
    allora = await get_allora_signals()
    risk = await get_risk_metrics(market_data, vault_state)

    state = {
        "vault": vault_state.model_dump(),
        "market": market_data.model_dump(),
        "allora": allora.model_dump(),
        "risk": risk.model_dump(),
        "past_theses": memory.load()[-3:],
    }

    decision = await reason(state)

    ok, errors = validate(decision)
    if not ok:
        raise ValueError(f"Decision failed validation: {errors}")

    cid = await upload_rationale(decision)
    decision_id = cid.encode()[:32].ljust(32, b"\x00")

    calls = build_allocation_calls(vault_state, decision.target_allocation)
    tx_hash = await execute_on_chain(decision_id, calls)

    memory.append({"thesis": decision.thesis, "tx": tx_hash, "cid": cid})


def run_cycle() -> None:
    asyncio.run(_run_cycle_async())
