import json

import httpx
import pytest

from agent.config import settings
from agent.execute.ipfs import PINATA_V3_URL, upload_rationale
from agent.reason.schema import BybitSubAllocation, Decision, TargetAllocation


def _decision() -> Decision:
    return Decision(
        thesis="Test thesis with enough text to satisfy the 20-char minimum.",
        target_allocation=TargetAllocation(
            cash_usdc=0.10,
            aave_v3_usdc=0.50,
            aave_v3_weth=0.0,
            bybit_attestor=0.40,
        ),
        bybit_sub_allocation=BybitSubAllocation(
            flexible_usdc=0.5,
            sol_basis_trade=0.2,
            eth_basis_trade=0.2,
            buffer_cash=0.1,
        ),
        confidence=0.7,
        risk_flags=[],
        expected_blended_apr_pct=6.5,
    )


def _install_mock_transport(monkeypatch, handler):
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


@pytest.mark.asyncio
async def test_upload_returns_cid_and_posts_decision_payload(monkeypatch):
    monkeypatch.setattr(settings, "PINATA_JWT", "test-jwt")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"data": {"cid": "bafy12345abcdef"}})

    _install_mock_transport(monkeypatch, handler)

    cid = await upload_rationale(_decision())

    assert cid == "bafy12345abcdef"
    assert seen["url"] == PINATA_V3_URL
    assert seen["auth"] == "Bearer test-jwt"
    # Multipart body should embed the JSON Decision and the V3 fields.
    body = seen["body"]
    assert b"decision.json" in body
    assert b"network" in body and b"public" in body
    # Decision fields present
    assert b"thesis" in body
    assert b"aave_v3_usdc" in body


@pytest.mark.asyncio
async def test_upload_raises_without_jwt(monkeypatch):
    monkeypatch.setattr(settings, "PINATA_JWT", "")
    with pytest.raises(RuntimeError, match="PINATA_JWT"):
        await upload_rationale(_decision())


@pytest.mark.asyncio
async def test_upload_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(settings, "PINATA_JWT", "test-jwt")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError):
        await upload_rationale(_decision())


@pytest.mark.asyncio
async def test_upload_raises_on_missing_cid(monkeypatch):
    monkeypatch.setattr(settings, "PINATA_JWT", "test-jwt")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": "abc"}})

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="data.cid"):
        await upload_rationale(_decision())


@pytest.mark.asyncio
async def test_upload_payload_is_valid_json(monkeypatch):
    """The bytes posted as `file` MUST be parseable JSON and round-trip
    back to a Decision-shaped dict. Catches silent serialization regressions."""
    monkeypatch.setattr(settings, "PINATA_JWT", "test-jwt")
    captured_file_bytes: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        # crude multipart parser: locate the JSON between the first
        # application/json header and the next boundary marker
        marker = b"Content-Type: application/json\r\n\r\n"
        start = body.index(marker) + len(marker)
        end = body.index(b"\r\n--", start)
        captured_file_bytes["raw"] = body[start:end]
        return httpx.Response(200, json={"data": {"cid": "bafyx"}})

    _install_mock_transport(monkeypatch, handler)
    await upload_rationale(_decision())

    parsed = json.loads(captured_file_bytes["raw"])
    assert parsed["thesis"].startswith("Test thesis")
    assert parsed["target_allocation"]["aave_v3_usdc"] == 0.50
    assert parsed["confidence"] == 0.7
