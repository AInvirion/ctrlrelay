"""CLI entry point for dev-sync."""

import typer
from rich.console import Console

from dev_sync import __version__

app = typer.Typer(
    name="dev-sync",
    help="Local-first orchestrator for Claude Code across multiple GitHub repos.",
    no_args_is_help=True,
)
console = Console()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"dev-sync version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """dev-sync orchestrator CLI."""


# Subcommand groups
config_app = typer.Typer(help="Configuration commands.")
app.add_typer(config_app, name="config")


@config_app.command("validate")
def config_validate(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Validate orchestrator.yaml configuration."""
    # Placeholder - will be implemented in Task 5
    console.print(f"[yellow]Validating config at {config_path}...[/yellow]")
    console.print("[red]Config validation not yet implemented[/red]")
    raise typer.Exit(1)


@config_app.command("repos")
def config_repos() -> None:
    """List configured repositories."""
    console.print("[yellow]Not yet implemented[/yellow]")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
