"""Standalone live smoke test for LM subscribe + redeem (`.47`).

Bypasses the snapshot → decide → validate → diff pipeline and exercises
the new `BybitClient.add_liquidity` / `remove_liquidity` methods
directly. Use this once after live capital deployment to confirm:

  1. add-liquidity accepts single-sided USDC at leverage=1
  2. positions endpoint returns the new position
  3. remove-liquidity with removeRate=100 fully exits
  4. USDC returns to UNIFIED after settlement

Default is dry-run (prints intended calls, no API hits). `--live` is
required to actually deploy capital.

Usage:
  uv run python -m agent.sandbox.lm_smoke --env-file ../.env  # dry-run
  uv run python -m agent.sandbox.lm_smoke --env-file ../.env --live
  uv run python -m agent.sandbox.lm_smoke --env-file ../.env --live \\
      --product-id 23 --amount 60  # BTC/USDC $60

Safety:
  * Aborts if USDC balance < amount + 5 (buffer for spread/fees)
  * Aborts if product not in `(23, 24)` unless `--allow-any-product`
  * 30s timeout on each poll loop
  * Logs every call's response + timing to stdout for audit
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from agent.bybit_oracle.bybit_client import BybitAPIError, BybitClient
from agent.bybit_oracle.config import OracleSettings


# Built-in max_leverage=1 LP pairs (BTC/USDC, ETH/USDC).
# Anything else needs `--allow-any-product` so the operator
# acknowledges they're staking into a leveraged pair manually.
_SAFE_PRODUCTS = {"23", "24"}


def _ts() -> str:
    """Short UTC timestamp prefix for log lines."""
    return datetime.now(UTC).strftime("%H:%M:%S")


async def _poll_position(
    client: BybitClient,
    product_id: str,
    *,
    timeout_s: int = 30,
    poll_interval_s: float = 2.0,
) -> dict | None:
    """Poll the positions endpoint until a position on `product_id`
    appears or `timeout_s` elapses. Returns the position row or None on
    timeout. Bybit's add-liquidity is async-settling; in practice the
    position shows up within 2–5s, but the timeout keeps us bounded if
    the order silently failed downstream of the 200 OK."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        positions = await client.get_liquidity_mining_positions(
            product_id=product_id
        )
        if positions:
            return positions[0]
        await asyncio.sleep(poll_interval_s)
    return None


async def _poll_position_closed(
    client: BybitClient,
    product_id: str,
    *,
    timeout_s: int = 60,
    poll_interval_s: float = 2.0,
) -> bool:
    """Poll until the position on `product_id` disappears from the
    active list (i.e. fully settled). Returns True on disappearance,
    False on timeout. Longer default than the open-side poll because
    Bybit's remove-liquidity can take 10–30s to settle on-pool."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        positions = await client.get_liquidity_mining_positions(
            product_id=product_id
        )
        if not positions:
            return True
        await asyncio.sleep(poll_interval_s)
    return False


async def _usdc_balance(client: BybitClient) -> Decimal:
    """USDC balance in UNIFIED (where LM subscribe pulls from).
    Returns 0 if no UNIFIED account or no USDC entry."""
    data = await client.get_asset_overview()
    for acct in data.get("list") or []:
        if acct.get("accountType") not in ("UnifiedTradingAccount", "UNIFIED"):
            continue
        for entry in acct.get("coinDetail") or []:
            if entry.get("coin") == "USDC":
                raw = entry.get("equity") or entry.get("walletBalance") or "0"
                return Decimal(str(raw))
    return Decimal(0)


async def run(args: argparse.Namespace) -> int:
    if args.env_file:
        env_path = Path(args.env_file).expanduser().resolve()
        if not env_path.is_file():
            print(f"[{_ts()}] ERROR env file not found: {env_path}")
            return 2
        load_dotenv(env_path, override=True)
        print(f"[{_ts()}] loaded env from {env_path}")

    product_id = str(args.product_id)
    amount = Decimal(str(args.amount))
    buffer = Decimal("5")

    if product_id not in _SAFE_PRODUCTS and not args.allow_any_product:
        print(
            f"[{_ts()}] ERROR product_id={product_id} not in safe set "
            f"{sorted(_SAFE_PRODUCTS)} (max_leverage=1 USDC pairs). "
            "Pass --allow-any-product to override."
        )
        return 2

    print(f"[{_ts()}] === LM smoke (product={product_id} amount=${amount}) ===")
    print(f"[{_ts()}] mode: {'LIVE' if args.live else 'dry-run'}")

    if not args.live:
        print(f"[{_ts()}] dry-run: would call add_liquidity(productId={product_id}, "
              f"quoteAmount={amount}, quoteAccountType=UNIFIED, leverage=1)")
        print(f"[{_ts()}] dry-run: would poll get_liquidity_mining_positions(productId={product_id})")
        print(f"[{_ts()}] dry-run: would call remove_liquidity(productId={product_id}, "
              "positionId=<from-poll>, removeRate=100, removeType=Normal)")
        print(f"[{_ts()}] dry-run done — re-run with --live to actually deploy")
        return 0

    async with BybitClient.from_settings(OracleSettings()) as client:
        # ─── Preflight ─────────────────────────────────────────────
        print(f"[{_ts()}] checking USDC balance...")
        usdc = await _usdc_balance(client)
        print(f"[{_ts()}] USDC available in UNIFIED: {usdc}")
        if usdc < amount + buffer:
            print(
                f"[{_ts()}] ABORT: USDC ({usdc}) < amount + buffer "
                f"({amount + buffer}). Top up the account or lower --amount."
            )
            return 2

        # ─── Subscribe ─────────────────────────────────────────────
        order_link_id = f"lm-smoke-{int(time.time())}"
        print(
            f"[{_ts()}] add_liquidity(productId={product_id}, "
            f"quoteAmount={amount}, leverage=1, orderLinkId={order_link_id})"
        )
        t0 = time.monotonic()
        try:
            add_out = await client.add_liquidity(
                product_id=product_id,
                order_link_id=order_link_id,
                quote_amount=str(amount),
                quote_account_type="UNIFIED",
                leverage="1",
            )
        except BybitAPIError as e:
            print(f"[{_ts()}] ABORT add_liquidity failed: retCode={e.ret_code} {e.ret_msg}")
            return 1
        dt = time.monotonic() - t0
        print(f"[{_ts()}] OK add_liquidity orderId={add_out.orderId} ({dt:.2f}s)")

        # ─── Poll for position ─────────────────────────────────────
        print(f"[{_ts()}] polling for position (≤30s)...")
        pos = await _poll_position(client, product_id, timeout_s=30)
        if pos is None:
            print(f"[{_ts()}] WARN: position never appeared — order may have "
                  "failed silently. Check Bybit UI; remove not attempted.")
            return 1
        pos_id = str(pos.get("positionId"))
        principal = pos.get("principalLiquidityValue") or pos.get("principalQuoteAmount")
        print(f"[{_ts()}] position open: positionId={pos_id} principal≈{principal}")
        print(f"[{_ts()}] raw position: {pos}")

        # ─── Settle delay before redeem ────────────────────────────
        delay = args.hold_seconds
        if delay > 0:
            print(f"[{_ts()}] holding position for {delay}s before redeem...")
            await asyncio.sleep(delay)

        # ─── Redeem (full exit) ────────────────────────────────────
        rm_link_id = f"{order_link_id}-rm"
        print(
            f"[{_ts()}] remove_liquidity(productId={product_id}, "
            f"positionId={pos_id}, removeRate=100, removeType=Normal, "
            f"orderLinkId={rm_link_id})"
        )
        t0 = time.monotonic()
        try:
            rm_out = await client.remove_liquidity(
                product_id=product_id,
                position_id=pos_id,
                order_link_id=rm_link_id,
                remove_rate=100,
                remove_type="Normal",
            )
        except BybitAPIError as e:
            print(f"[{_ts()}] WARN remove_liquidity failed: retCode={e.ret_code} {e.ret_msg}")
            print(f"[{_ts()}] position {pos_id} STILL OPEN — close manually via UI")
            return 1
        dt = time.monotonic() - t0
        print(f"[{_ts()}] OK remove_liquidity orderId={rm_out.orderId} ({dt:.2f}s)")

        # ─── Poll for close ────────────────────────────────────────
        print(f"[{_ts()}] polling for position close (≤60s)...")
        closed = await _poll_position_closed(client, product_id, timeout_s=60)
        if not closed:
            print(f"[{_ts()}] WARN: position {pos_id} still showing as active "
                  "after 60s. Bybit may be slow to settle; check UI.")
        else:
            print(f"[{_ts()}] position closed")

        # ─── Post-balance ──────────────────────────────────────────
        usdc_after = await _usdc_balance(client)
        delta = usdc_after - usdc
        print(f"[{_ts()}] USDC after: {usdc_after}  delta vs start: {delta:+}")
        print(f"[{_ts()}] === DONE ===")
        return 0


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Live smoke test for LM subscribe + redeem (.47)"
    )
    parser.add_argument(
        "--product-id",
        default="24",
        help="LM product id (24=ETH/USDC, 23=BTC/USDC; default 24)",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=55.0,
        help="USDC amount to subscribe (Bybit min is 50, default 55)",
    )
    parser.add_argument(
        "--hold-seconds",
        type=int,
        default=10,
        help="Seconds to hold position before redeem (default 10)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually place orders. Default is dry-run.",
    )
    parser.add_argument(
        "--allow-any-product",
        action="store_true",
        help="Bypass the (23, 24) safe-product gate. Use with care.",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="dotenv path (e.g. ../.env from agent/)",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(run(args))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    _main()
