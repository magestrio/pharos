import asyncio
import click
from agent.gather.vault_state import get_vault_state
from agent.gather.market_data import get_market_data
from agent.gather.allora import get_allora_signals
from agent.gather.risk_metrics import get_risk_metrics


@click.group()
def cli():
    pass


@cli.command()
def state():
    """Take a live snapshot of all 4 gather tools."""
    async def run():
        market = await get_market_data()
        click.secho("=== MARKET ===", fg="cyan", bold=True)
        click.echo(market.model_dump_json(indent=2))

        vault = None
        try:
            vault = await get_vault_state()
            click.secho("\n=== VAULT ===", fg="cyan", bold=True)
            click.echo(vault.model_dump_json(indent=2))
        except Exception as e:
            click.secho(f"\nvault: not deployed yet ({e})", fg="yellow")

        allora = await get_allora_signals()
        click.secho("\n=== ALLORA ===", fg="cyan", bold=True)
        click.echo(allora.model_dump_json(indent=2))

        if vault:
            risk = await get_risk_metrics(market, vault)
            click.secho("\n=== RISK ===", fg="cyan", bold=True)
            click.echo(risk.model_dump_json(indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    cli()
