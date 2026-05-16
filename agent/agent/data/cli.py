import asyncio

import click

from agent.data.fetchers import (
    allora_schema,
    coingecko,
    defillama_tvl,
    defillama_yields,
    dexscreener,
    funding,
    meth_api,
)
from agent.data.merge import build_daily_dataset
from agent.data.storage import load_parquet


@click.group()
def cli() -> None:
    pass


@cli.command("fetch-all")
def fetch_all() -> None:
    """Download all sources into data/raw/"""

    async def run() -> None:
        await coingecko.fetch_coingecko()
        await dexscreener.fetch_dexscreener()
        await meth_api.fetch_meth()
        await defillama_yields.fetch_yields()
        await funding.fetch_funding()
        await defillama_tvl.fetch_tvl()
        await allora_schema.fetch_allora_schema()

    asyncio.run(run())


@cli.command()
def merge() -> None:
    """Merge raw parquets into data/processed/daily_90d.parquet"""
    df = build_daily_dataset()
    click.echo(f"merged shape: {df.shape}")


@cli.command()
def inspect() -> None:
    """Print summary of both processed datasets"""
    for name in ["daily_90d", "daily_clean"]:
        click.secho(f"\n=== {name} ===", fg="cyan", bold=True)
        df = load_parquet(name, "processed")
        click.echo(f"shape: {df.shape}")
        click.echo(f"date range: {df['date'].min()} → {df['date'].max()}")
        click.echo("NaN %:")
        click.echo((df.isna().mean() * 100).round(1).to_string())


if __name__ == "__main__":
    cli()
