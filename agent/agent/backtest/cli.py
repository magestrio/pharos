import click
from agent.backtest.engine import run_backtest
from agent.backtest.policy import DummyPolicy, HumanPMPolicy


@click.group()
def cli():
    pass


@cli.command()
@click.option("--capital", default=1000.0, show_default=True, help="Starting capital in USD")
@click.option("--dataset", default="daily_clean", show_default=True, help="Parquet dataset name")
def run(capital: float, dataset: str):
    """Run Dummy and HumanPM policies on historical data and compare."""
    for policy in [DummyPolicy(), HumanPMPolicy()]:
        click.secho(f"\n=== {policy.name} ===", fg="cyan", bold=True)
        result = run_backtest(policy, initial_capital_usd=capital, dataset_name=dataset)

        if not result.days:
            click.secho("No data.", fg="red")
            continue

        click.echo(f"Period:  {result.days[0].date.date()} → {result.days[-1].date.date()} ({len(result.days)} days)")
        click.echo(f"Initial: ${result.initial_capital_usd:,.2f}")
        click.echo(f"Final:   ${result.final_capital_usd:,.2f}")
        click.echo(f"Return:  {result.total_return_pct:+.4f}%")
        click.echo(f"APR:     {result.annualized_apr_pct:+.2f}%")
        click.echo(f"Sharpe:  {result.sharpe_ratio:.2f}")
        click.echo(f"Max DD:  {result.max_drawdown_pct:.4f}%")
        click.echo(f"Rebal:   {result.rebalance_count},  Skip: {result.skip_count}")


if __name__ == "__main__":
    cli()
