import os
from datetime import datetime, timezone
from agent.gather.models import AlloraSignal, AlloraSignals

# Placeholder topic IDs — UPDATE after registration via get_all_topics()
_TOPICS = {
    "eth_24h":          (13, "ETH 24h price prediction"),
    "eth_7d":           (14, "ETH 7d price prediction"),
    "funding_forecast": (20, "ETH funding rate"),
}


def _unavailable(topic_id: int, topic_name: str) -> AlloraSignal:
    return AlloraSignal(topic_id=topic_id, topic_name=topic_name, is_available=False)


async def get_allora_signals() -> AlloraSignals:
    now = datetime.now(timezone.utc)
    api_key = os.getenv("ALLORA_API_KEY")

    if not api_key:
        return AlloraSignals(
            eth_24h=_unavailable(*_TOPICS["eth_24h"]),
            eth_7d=_unavailable(*_TOPICS["eth_7d"]),
            funding_forecast=_unavailable(*_TOPICS["funding_forecast"]),
            timestamp=now,
        )

    try:
        from allora_sdk.v2.api_client import AlloraAPIClient, ChainSlug
        client = AlloraAPIClient(chain_slug=ChainSlug.MAINNET, api_key=api_key)

        signals: dict[str, AlloraSignal] = {}
        for key, (topic_id, topic_name) in _TOPICS.items():
            try:
                inf = await client.get_inference_by_topic_id(topic_id)
                signals[key] = AlloraSignal(
                    topic_id=topic_id,
                    topic_name=topic_name,
                    inference=float(inf.inference_data.network_inference_normalized),
                    is_available=True,
                )
            except Exception:
                signals[key] = _unavailable(topic_id, topic_name)

        return AlloraSignals(
            eth_24h=signals["eth_24h"],
            eth_7d=signals["eth_7d"],
            funding_forecast=signals["funding_forecast"],
            timestamp=now,
        )

    except ImportError:
        return AlloraSignals(
            eth_24h=_unavailable(*_TOPICS["eth_24h"]),
            eth_7d=_unavailable(*_TOPICS["eth_7d"]),
            funding_forecast=_unavailable(*_TOPICS["funding_forecast"]),
            timestamp=now,
        )
