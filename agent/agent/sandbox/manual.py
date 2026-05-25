"""Manual Bybit API exploration CLI.

Thin wrapper over `BybitClient` for Phase A of the bybit-sandbox epic:
hit Bybit endpoints by hand, see what real responses look like, and
capture each one to JSON so Phase B prompt design has concrete payloads
to work from.

Mutating commands (subscribe, redeem, spot-order) refuse to run without
`--live` to avoid accidental real-money calls during exploration.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv
from pydantic import BaseModel

from agent.bybit_oracle.bybit_client import BybitClient
from agent.bybit_oracle.config import OracleSettings

CAPTURE_DIR = Path(__file__).parent / "captures"


def _serialize(payload: Any) -> Any:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    if isinstance(payload, list):
        return [_serialize(item) for item in payload]
    if isinstance(payload, dict):
        return {k: _serialize(v) for k, v in payload.items()}
    return payload


def _capture(cmd: str, payload: Any) -> Path:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = CAPTURE_DIR / f"{ts}-{cmd}.json"
    path.write_text(json.dumps(_serialize(payload), indent=2, default=str))
    return path


def _print(payload: Any) -> None:
    click.echo(json.dumps(_serialize(payload), indent=2, default=str))


def _build_client(env_file: str | None) -> BybitClient:
    if env_file:
        load_dotenv(env_file, override=True)
    return BybitClient.from_settings(OracleSettings())


def _require_live(live: bool, cmd: str) -> None:
    if not live:
        raise click.ClickException(
            f"{cmd} is a mutating call against the real sub-account; pass --live to execute"
        )


@click.group()
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Override default .env (e.g. ~/.config/vault8004/bybit-sandbox.env).",
)
@click.pass_context
def cli(ctx: click.Context, env_file: str | None) -> None:
    """Manual Bybit API exploration. Captures every response under agent/sandbox/captures/."""
    ctx.ensure_object(dict)
    ctx.obj["env_file"] = env_file


@cli.command()
@click.option("--category", default=None, help="FlexibleSaving, OnChain, FixedSaving, ...")
@click.option("--coin", default=None, help="USDC, USDT, BTC, ...")
@click.pass_context
def products(ctx: click.Context, category: str | None, coin: str | None) -> None:
    """List Earn products."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.list_earn_products(category=category, coin=coin)
        _print(result)
        path = _capture("products", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command()
@click.option("--category", default=None, help="Filter by Earn category.")
@click.pass_context
def positions(ctx: click.Context, category: str | None) -> None:
    """Show open Earn positions."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.get_earn_positions(category=category)
        _print(result)
        path = _capture("positions", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command()
@click.option("--coin", default=None, help="Restrict to a single coin (default: all).")
@click.option(
    "--account-type",
    default="UNIFIED",
    show_default=True,
    help="UNIFIED, FUND, CONTRACT, ...",
)
@click.pass_context
def wallet(ctx: click.Context, coin: str | None, account_type: str) -> None:
    """Show wallet balances."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.get_wallet_balance(coin=coin, account_type=account_type)
        _print(result)
        path = _capture("wallet", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command()
@click.argument("product_id")
@click.argument("amount")
@click.option("--live", is_flag=True, help="Required to execute against the real account.")
@click.pass_context
def subscribe(ctx: click.Context, product_id: str, amount: str, live: bool) -> None:
    """Stake `amount` into Earn product `product_id`."""
    _require_live(live, "subscribe")

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.place_earn_order(
                product_id=product_id, amount=amount, side="Stake"
            )
        _print(result)
        path = _capture("subscribe", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command()
@click.argument("product_id")
@click.argument("amount")
@click.option("--live", is_flag=True, help="Required to execute against the real account.")
@click.pass_context
def redeem(ctx: click.Context, product_id: str, amount: str, live: bool) -> None:
    """Redeem `amount` from Earn product `product_id`."""
    _require_live(live, "redeem")

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.redeem_from_earn(product_id=product_id, amount=amount)
        _print(result)
        path = _capture("redeem", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command(name="spot-order")
@click.argument("symbol")
@click.argument("side", type=click.Choice(["Buy", "Sell"], case_sensitive=False))
@click.argument("qty")
@click.option(
    "--order-type",
    type=click.Choice(["Market", "Limit"], case_sensitive=False),
    default="Market",
    show_default=True,
)
@click.option("--price", default=None, help="Required for Limit orders.")
@click.option("--live", is_flag=True, help="Required to execute against the real account.")
@click.pass_context
def spot_order(
    ctx: click.Context,
    symbol: str,
    side: str,
    qty: str,
    order_type: str,
    price: str | None,
    live: bool,
) -> None:
    """Place a spot order. Caller is responsible for lot-size / min-notional rules."""
    _require_live(live, "spot-order")

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.place_spot_order(
                symbol=symbol,
                side=side.capitalize(),  # type: ignore[arg-type]
                qty=qty,
                order_type=order_type.capitalize(),  # type: ignore[arg-type]
                price=price,
            )
        _print(result)
        path = _capture("spot-order", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command(name="perp-status")
@click.argument("symbol")
@click.pass_context
def perp_status(ctx: click.Context, symbol: str) -> None:
    """Show perp ticker + instrument info for `symbol` (e.g. BTCUSDT)."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            tickers = await client.get_tickers(category="linear", symbol=symbol)
            instruments = await client.get_instruments_info(category="linear", symbol=symbol)
        combined: dict[str, Any] = {
            "symbol": symbol,
            "ticker": tickers[0] if tickers else None,
            "instrument": instruments[0] if instruments else None,
        }
        _print(combined)
        path = _capture("perp-status", combined)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


if __name__ == "__main__":
    cli()
