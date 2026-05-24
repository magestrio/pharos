import httpx

from agent.config import settings
from agent.reason.schema import Decision


PINATA_V3_URL = "https://uploads.pinata.cloud/v3/files"


async def upload_rationale(decision: Decision) -> str:
    """Pin decision rationale to IPFS via Pinata V3 Files API. Returns CID.

    Mirrors `scripts/pin-agent-metadata.sh`: V3 Files endpoint, Bearer JWT,
    multipart with `network=public`. Single-file upload yields a CID for
    the file itself (no parent folder) — the on-chain pointer is
    `keccak256(cid)`, see execute.tx.
    """
    if not settings.PINATA_JWT:
        raise RuntimeError("PINATA_JWT not configured (V3 key with Files: Write scope)")

    body = decision.model_dump_json(indent=2).encode()
    files = {
        "file": ("decision.json", body, "application/json"),
        "network": (None, "public"),
        "name": (None, "decision.json"),
    }
    headers = {"Authorization": f"Bearer {settings.PINATA_JWT}"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(PINATA_V3_URL, headers=headers, files=files)
        resp.raise_for_status()
        payload = resp.json()

    try:
        return payload["data"]["cid"]
    except (KeyError, TypeError) as e:
        raise RuntimeError(f"Pinata response missing data.cid: {payload!r}") from e
