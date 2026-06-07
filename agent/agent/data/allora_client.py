"""Minimal async client for Allora consumer price-inference API.

Allora's public price feed exposes a directional 5-minute or 8-hour
forecast per supported asset (BTC / ETH / SOL today). One GET per
(asset, window) — `network_inference_normalized` is the predicted USD
price at end-of-window. The response is signed; we ignore the signature
for off-chain use and just store the numeric forecast in the snapshot
for the LLM to factor into the next decision.

All failures (missing key, HTTP error, malformed JSON) return `None`.
Snapshot collector aggregates non-None results and records the
failures in `Snapshot.errors`.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)

# `ethereum-11155111` is the chain slug Allora's consumer API uses to
# disambiguate verification networks (Sepolia in their case). We don't
# consume the on-chain signature so the slug is just an opaque path
# segment.
_BASE_URL = "https://api.allora.network/v2/allora/consumer/price/ethereum-11155111"
_TIMEOUT_SECONDS = 8.0

# Confirmed via probe 2026-06-05: only `5m` and `8h` windows are live
# per token; everything else returns 404 "Could not find model". Keep
# the public surface small — drop a window if it 404s consistently.
SUPPORTED_TOKENS = ("BTC", "ETH", "SOL")
SUPPORTED_WINDOWS = ("5m", "8h")


class AlloraInference(BaseModel):
    """Single Allora price forecast. Decimal kept verbatim (don't lose
    precision down-stream — the raw `network_inference` is base-units
    × 10^token_decimals)."""

    model_config = ConfigDict(extra="ignore")

    token: str           # "BTC" / "ETH" / "SOL"
    window: str          # "5m" / "8h"
    topic_id: int        # Allora topic — distinguishes the model family
    inference_usd: Decimal  # `network_inference_normalized` from the API
    timestamp: int       # Unix sec — when the inference was produced


class AlloraClient:
    """One-shot client. No internal caching — snapshot collector handles
    cadence (fetched once per cycle).
    """

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def fetch_inference(
        self, token: str, window: str
    ) -> AlloraInference | None:
        url = f"{_BASE_URL}/{token}/{window}"
        headers = {"x-api-key": self._key, "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            log.warning("allora: network error %s/%s: %s", token, window, e)
            return None

        try:
            payload: dict[str, Any] = resp.json()
        except ValueError:
            log.warning(
                "allora: non-JSON response %s/%s (HTTP %d)", token, window, resp.status_code
            )
            return None

        if resp.status_code != 200 or not payload.get("status"):
            msg = payload.get("apiResponseMessage")
            log.debug("allora: %s/%s rejected (HTTP %d): %s", token, window, resp.status_code, msg)
            return None

        data = (payload.get("data") or {}).get("inference_data") or {}
        inference_raw = data.get("network_inference_normalized")
        topic_id = data.get("topic_id")
        ts = data.get("timestamp")
        if inference_raw is None or topic_id is None or ts is None:
            log.warning("allora: incomplete payload %s/%s: %s", token, window, data)
            return None

        try:
            return AlloraInference(
                token=token,
                window=window,
                topic_id=int(topic_id),
                inference_usd=Decimal(str(inference_raw)),
                timestamp=int(ts),
            )
        except (ValueError, ArithmeticError) as e:
            log.warning("allora: malformed numeric fields %s/%s: %s", token, window, e)
            return None
