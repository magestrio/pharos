"""Pin a decision's rationale JSON to IPFS via Pinata V3.

Mirrors `scripts/pin-agent-metadata.sh` (Files V3 multipart upload).
The CID is meant to be embedded in `DecisionLog.recordDecision` so the
on-chain event carries a public, content-addressed pointer to the full
rationale + venues + thesis (judges / explorers can follow it).

All entry points are best-effort: missing `PINATA_JWT`, network blip,
or non-200 response → `None`, agent loop continues with empty CID.
"""

from __future__ import annotations

import io
import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

_PINATA_URL = "https://uploads.pinata.cloud/v3/files"
_TIMEOUT_SECONDS = 15.0


def pin_decision_rationale(
    decision: dict[str, Any],
    snapshot_filename: str,
) -> str | None:
    """Pin a single decision JSON to IPFS. Returns CID on success, `None`
    on any failure (missing token, HTTP error, malformed response).

    `snapshot_filename` is used as the Pinata file `name` so the dashboard
    surfaces "decision-<ts>.json" rather than an opaque UUID — easier to
    audit pin history during a live run.
    """
    jwt = os.environ.get("PINATA_JWT")
    if not jwt:
        return None

    # Strip the existing `_meta.ipfs_cid` (if any) before pinning so a
    # re-pin doesn't embed last cycle's CID into this cycle's payload —
    # avoids confusing "the pinned JSON points at itself" inception.
    payload = dict(decision)
    meta = dict(payload.get("_meta") or {})
    meta.pop("ipfs_cid", None)
    payload["_meta"] = meta

    body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    name = f"decision-{snapshot_filename.removesuffix('.json')}.json"

    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.post(
                _PINATA_URL,
                headers={"Authorization": f"Bearer {jwt}"},
                files={
                    "file": (name, io.BytesIO(body), "application/json"),
                },
                data={
                    "network": "public",
                    "name": name,
                },
            )
    except httpx.HTTPError as e:
        log.warning("pinata: network error pinning %s: %s", name, e)
        return None

    if resp.status_code != 200:
        log.warning(
            "pinata: HTTP %d pinning %s — body: %s",
            resp.status_code,
            name,
            resp.text[:200],
        )
        return None

    try:
        cid = resp.json()["data"]["cid"]
    except (KeyError, ValueError) as e:
        log.warning("pinata: malformed response for %s: %s", name, e)
        return None

    if not isinstance(cid, str) or not cid:
        return None
    log.info("pinata: pinned %s → %s", name, cid)
    return cid
