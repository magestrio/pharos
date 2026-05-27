"""Manual Bybit API exploration CLI.

Thin wrapper over `BybitClient` for Phase A of the bybit-sandbox epic:
hit Bybit endpoints by hand, see what real responses look like, and
capture each one to JSON so Phase B prompt design has concrete payloads
to work from.

Mutating commands (subscribe, redeem, advance-place, spot-order) refuse
to run without `--live` to avoid accidental real-money calls during
exploration.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv
from pydantic import BaseModel

from agent.bybit_oracle.bybit_client import (
    ADVANCE_EARN_CATEGORIES,
    BybitClient,
)
from agent.bybit_oracle.config import OracleSettings

_LEGACY_EARN_CATEGORIES = ("FlexibleSaving", "OnChain")
_ADVANCE_EARN_CATEGORIES = tuple(sorted(ADVANCE_EARN_CATEGORIES))
_EARN_CATEGORIES = _LEGACY_EARN_CATEGORIES + _ADVANCE_EARN_CATEGORIES

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


def _default_link_id(prefix: str) -> str:
    """Generate a fresh idempotency key for manual one-off calls. Bybit
    rejects reuse within 30min so a per-invocation UUID keeps the CLI
    safe to re-run."""
    return f"manual-{prefix}-{uuid.uuid4().hex[:12]}"


def _parse_extra(extra_json: str | None) -> dict[str, Any] | None:
    if not extra_json:
        return None
    parsed = json.loads(extra_json)
    if not isinstance(parsed, dict):
        raise click.BadParameter("--extra must be a JSON object")
    return parsed


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


# ─── Listing / read-only ────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--category",
    type=click.Choice(_EARN_CATEGORIES, case_sensitive=True),
    default="FlexibleSaving",
    show_default=True,
    help="Earn category. Basic (/v5/earn/product): FlexibleSaving, OnChain. "
    "Advance (/v5/earn/advance/product): " + ", ".join(_ADVANCE_EARN_CATEGORIES) + ".",
)
@click.option("--coin", default=None, help="USDC, USDT, BTC, ...")
@click.pass_context
def products(ctx: click.Context, category: str, coin: str | None) -> None:
    """List Earn products. Dispatches to the right endpoint per category."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            if category in _LEGACY_EARN_CATEGORIES:
                result = await client.list_earn_products(category=category, coin=coin)
            else:
                result = await client.list_advance_earn_products(
                    category=category, coin=coin
                )
        _print(result)
        path = _capture(f"products-{category}", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command(name="advance-quote")
@click.option(
    "--category",
    type=click.Choice(_ADVANCE_EARN_CATEGORIES, case_sensitive=True),
    required=True,
    help="Advance-Earn category.",
)
@click.option("--product-id", default=None, help="Optional — omit to get all quotes.")
@click.pass_context
def advance_quote(ctx: click.Context, category: str, product_id: str | None) -> None:
    """Get advance-Earn product quote (/v5/earn/advance/product-extra-info).
    Required before placing a Stake order to capture initialPrice /
    breakevenPrice / apyE8 / etc. that must be echoed back."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.get_advance_product_quote(
                category=category, product_id=product_id
            )
        _print(result)
        path = _capture(f"advance-quote-{category}", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command()
@click.option("--category", default=None, help="Filter by basic-Earn category.")
@click.pass_context
def positions(ctx: click.Context, category: str | None) -> None:
    """Show open basic-Earn positions (FlexibleSaving / OnChain)."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.get_earn_positions(category=category)
        _print(result)
        path = _capture("positions", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command(name="advance-positions")
@click.option(
    "--category",
    type=click.Choice(_ADVANCE_EARN_CATEGORIES, case_sensitive=True),
    required=True,
)
@click.option("--product-id", required=True)
@click.pass_context
def advance_positions(ctx: click.Context, category: str, product_id: str) -> None:
    """Show open advance-Earn positions for a given product."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.get_advance_earn_positions(
                category=category, product_id=product_id
            )
        _print(result)
        path = _capture(f"advance-positions-{category}", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command(name="redeem-estimate")
@click.option(
    "--category",
    type=click.Choice(_ADVANCE_EARN_CATEGORIES, case_sensitive=True),
    required=True,
)
@click.option(
    "--position-ids",
    required=True,
    help="Comma-separated position IDs (e.g. 2847,2848).",
)
@click.pass_context
def redeem_estimate(ctx: click.Context, category: str, position_ids: str) -> None:
    """Estimate redeem amount for advance-Earn positions. Pass the result
    back into `advance-place --side Redeem` via the matching *RedeemExtra."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.get_redeem_estimate(
                category=category, position_ids=position_ids
            )
        _print(result)
        path = _capture(f"redeem-estimate-{category}", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command(name="hourly-yield")
@click.option("--category", default="FlexibleSaving", show_default=True)
@click.option("--product-id", default=None)
@click.option("--start", "start_time", type=int, default=None, help="Unix ms.")
@click.option("--end", "end_time", type=int, default=None, help="Unix ms.")
@click.option("--limit", type=int, default=None, help="1-100; default 50.")
@click.option("--cursor", default=None)
@click.pass_context
def hourly_yield(
    ctx: click.Context,
    category: str,
    product_id: str | None,
    start_time: int | None,
    end_time: int | None,
    limit: int | None,
    cursor: str | None,
) -> None:
    """Historical hourly yield (/v5/earn/hourly-yield). 7d window cap;
    paginate via the returned nextPageCursor."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.get_hourly_yield(
                category=category,
                product_id=product_id,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                cursor=cursor,
            )
        _print(result)
        path = _capture(f"hourly-yield-{category}", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command(name="apr-history")
@click.argument("product_id")
@click.option("--category", default="FlexibleSaving", show_default=True,
              help="FlexibleSaving | OnChain (only these two supported).")
@click.option("--days", type=int, default=30, show_default=True)
@click.pass_context
def apr_history(ctx: click.Context, product_id: str, category: str, days: int) -> None:
    """Daily APR history (/v5/earn/apr-history)."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.get_apr_history(
                category=category, product_id=product_id, days=days
            )
        _print(result)
        path = _capture(f"apr-history-{category}-{product_id}", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command(name="yield-history")
@click.option("--category", default="FlexibleSaving", show_default=True)
@click.option("--start", "start_time", type=int, required=True, help="Unix ms.")
@click.option("--end", "end_time", type=int, required=True, help="Unix ms.")
@click.option("--product-id", default=None)
@click.option("--limit", type=int, default=None)
@click.option("--cursor", default=None)
@click.pass_context
def yield_history(
    ctx: click.Context,
    category: str,
    start_time: int,
    end_time: int,
    product_id: str | None,
    limit: int | None,
    cursor: str | None,
) -> None:
    """Realized yield records (/v5/earn/yield). 7d window cap server-side."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.get_yield_history(
                category=category,
                start_time=start_time,
                end_time=end_time,
                product_id=product_id,
                limit=limit,
                cursor=cursor,
            )
        _print(result)
        path = _capture(f"yield-history-{category}", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command(name="asset-overview")
@click.option("--account-type", default=None, help="Omit for cross-account aggregate.")
@click.option("--valuation-currency", default=None, help="Defaults to USD on Bybit side.")
@click.option("--member-id", default=None, help="Required when master key queries subaccount.")
@click.pass_context
def asset_overview(
    ctx: click.Context,
    account_type: str | None,
    valuation_currency: str | None,
    member_id: str | None,
) -> None:
    """Single-call holdings across Spot/Derivatives/Earn/Funding
    (/v5/asset/asset-overview)."""

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.get_asset_overview(
                account_type=account_type,
                valuation_currency=valuation_currency,
                member_id=member_id,
            )
        _print(result)
        path = _capture(f"asset-overview-{account_type or 'all'}", result)
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


# ─── Mutating ──────────────────────────────────────────────────────────────


@cli.command()
@click.argument("product_id")
@click.argument("amount")
@click.option(
    "--category",
    type=click.Choice(_LEGACY_EARN_CATEGORIES, case_sensitive=True),
    default="FlexibleSaving",
    show_default=True,
    help="Basic Earn only. Use `advance-place` for advance categories.",
)
@click.option("--coin", required=True, help="USDC, USDT, ...")
@click.option(
    "--account-type",
    type=click.Choice(["FUND", "UNIFIED"], case_sensitive=True),
    default="FUND",
    show_default=True,
    help="OnChain only supports FUND.",
)
@click.option(
    "--order-link-id",
    default=None,
    help="Idempotency key; auto-generated if omitted.",
)
@click.option("--live", is_flag=True, help="Required to execute against the real account.")
@click.pass_context
def subscribe(
    ctx: click.Context,
    product_id: str,
    amount: str,
    category: str,
    coin: str,
    account_type: str,
    order_link_id: str | None,
    live: bool,
) -> None:
    """Stake `amount` into basic Earn product `product_id`."""
    _require_live(live, "subscribe")
    link_id = order_link_id or _default_link_id("stake")

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.place_earn_order(
                category=category,  # type: ignore[arg-type]
                product_id=product_id,
                amount=amount,
                side="Stake",
                coin=coin,
                account_type=account_type,  # type: ignore[arg-type]
                order_link_id=link_id,
            )
        _print(result)
        path = _capture("subscribe", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command()
@click.argument("product_id")
@click.argument("amount")
@click.option(
    "--category",
    type=click.Choice(_LEGACY_EARN_CATEGORIES, case_sensitive=True),
    default="FlexibleSaving",
    show_default=True,
)
@click.option("--coin", required=True)
@click.option(
    "--account-type",
    type=click.Choice(["FUND", "UNIFIED"], case_sensitive=True),
    default="FUND",
    show_default=True,
)
@click.option("--order-link-id", default=None)
@click.option("--live", is_flag=True, help="Required to execute against the real account.")
@click.pass_context
def redeem(
    ctx: click.Context,
    product_id: str,
    amount: str,
    category: str,
    coin: str,
    account_type: str,
    order_link_id: str | None,
    live: bool,
) -> None:
    """Redeem `amount` from basic Earn product `product_id`."""
    _require_live(live, "redeem")
    link_id = order_link_id or _default_link_id("redeem")

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.redeem_from_earn(
                category=category,  # type: ignore[arg-type]
                product_id=product_id,
                amount=amount,
                coin=coin,
                account_type=account_type,  # type: ignore[arg-type]
                order_link_id=link_id,
            )
        _print(result)
        path = _capture("redeem", result)
        click.secho(f"\ncaptured → {path}", fg="green")

    asyncio.run(run())


@cli.command(name="advance-place")
@click.argument("product_id")
@click.option(
    "--category",
    type=click.Choice(_ADVANCE_EARN_CATEGORIES, case_sensitive=True),
    required=True,
)
@click.option(
    "--side",
    type=click.Choice(["Stake", "Redeem"], case_sensitive=True),
    default="Stake",
    show_default=True,
)
@click.option("--coin", default=None, help="Required for Stake.")
@click.option("--amount", default=None, help="Required for Stake.")
@click.option(
    "--account-type",
    type=click.Choice(["FUND", "UNIFIED"], case_sensitive=True),
    default="FUND",
    show_default=True,
)
@click.option("--order-link-id", default=None)
@click.option(
    "--extra",
    default=None,
    help='JSON object holding the per-category *Extra block, e.g. '
    '\'{"smartLeverageStakeExtra": {"initialPrice": "68403", "breakevenPrice": "68650"}}\'. '
    "Fields must be echoed from `advance-quote` / `redeem-estimate`.",
)
@click.option("--live", is_flag=True, help="Required to execute against the real account.")
@click.pass_context
def advance_place(
    ctx: click.Context,
    product_id: str,
    category: str,
    side: str,
    coin: str | None,
    amount: str | None,
    account_type: str,
    order_link_id: str | None,
    extra: str | None,
    live: bool,
) -> None:
    """Place an advance-Earn order (/v5/earn/advance/place-order). The
    per-category *Extra block goes through --extra as JSON; quote first
    via `advance-quote` to grab the right fields."""
    _require_live(live, "advance-place")
    link_id = order_link_id or _default_link_id(f"adv-{side.lower()}")
    extra_dict = _parse_extra(extra)

    async def run() -> None:
        async with _build_client(ctx.obj["env_file"]) as client:
            result = await client.place_advance_earn_order(
                category=category,
                product_id=product_id,
                side=side,  # type: ignore[arg-type]
                coin=coin,
                amount=amount,
                account_type=account_type,  # type: ignore[arg-type]
                order_link_id=link_id,
                extra=extra_dict,
            )
        _print(result)
        path = _capture(f"advance-place-{category}-{side}", result)
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
