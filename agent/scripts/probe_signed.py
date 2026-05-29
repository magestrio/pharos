import asyncio
import os
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import load_dotenv
from agent.bybit_oracle.bybit_client import BybitAPIError, BybitClient
from agent.bybit_oracle.config import OracleSettings


async def main():
    env_path = Path.home() / ".config" / "vault8004" / "bybit-sandbox.env"
    load_dotenv(env_path, override=True)
    cfg = OracleSettings()

    # Test 1: real client with real key
    async with BybitClient.from_settings(cfg) as real_client:
        try:
            out = await real_client.list_earn_products(category="FlexibleSaving")
            print(f"REAL key  → list_earn_products: list[{len(out)}] — works")
        except BybitAPIError as e:
            print(f"REAL key  → list_earn_products: FAIL {e.ret_code} {e.ret_msg[:100]}")

    # Test 2: fake key — if endpoint is public, this still works
    fake_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fake_client = BybitClient(
        api_key="FAKE_KEY_NEVER_REGISTERED",
        private_key=fake_key,
        base_url=cfg.BYBIT_BASE_URL,
    )
    async with fake_client as c:
        try:
            out = await c.list_earn_products(category="FlexibleSaving")
            print(f"FAKE key  → list_earn_products: list[{len(out)}] — ENDPOINT IS PUBLIC")
        except BybitAPIError as e:
            print(f"FAKE key  → list_earn_products: FAIL {e.ret_code} {e.ret_msg[:100]} — endpoint is signed")


asyncio.run(main())
