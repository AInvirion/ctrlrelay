"""CLI entry point for dev-sync."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from dev_sync import __version__
from dev_sync.core.config import Config, ConfigError, load_config

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
    path = Path(config_path)

    if not path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {path}")
        raise typer.Exit(1)

    try:
        config = load_config(path)
    except ConfigError as e:
        console.print(f"[red]Validation failed:[/red] {e}")
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] Config valid: {path}")
    console.print(f"  Node ID: {config.node_id}")
    console.print(f"  Timezone: {config.timezone}")
    console.print(f"  Transport: {config.transport.type.value}")
    console.print(f"  Repos: {len(config.repos)}")


@config_app.command("repos")
def config_repos(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """List configured repositories."""
    path = Path(config_path)

    try:
        config = load_config(path)
    except ConfigError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if not config.repos:
        console.print("[yellow]No repositories configured.[/yellow]")
        return

    table = Table(title="Configured Repositories")
    table.add_column("Name", style="cyan")
    table.add_column("Path", style="dim")
    table.add_column("Deploy", style="green")

    for repo in config.repos:
        deploy = repo.deploy.provider if repo.deploy else "-"
        table.add_row(repo.name, str(repo.local_path), deploy)

    console.print(table)


@app.command("status")
def status(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Show orchestrator status and active sessions."""
    from dev_sync.core.state import StateDB

    try:
        config = load_config(config_path)
    except ConfigError as e:
        console.print(f"[red]Error loading config:[/red] {e}")
        raise typer.Exit(1)

    db_path = config.paths.state_db
    if not db_path.exists():
        console.print(f"[yellow]State database not found at {db_path}[/yellow]")
        console.print("Run a pipeline first to initialize the database.")
        return

    import sqlite3

    try:
        db = StateDB(db_path)
    except sqlite3.Error as e:
        console.print(f"[red]Error opening database:[/red] {e}")
        raise typer.Exit(1)

    try:
        # Show locks
        locks = db.list_locks()
        if locks:
            console.print("\n[bold]Active Locks:[/bold]")
            for lock in locks:
                console.print(f"  • {lock['repo']} → session {lock['session_id']}")
        else:
            console.print("\n[dim]No active locks[/dim]")

        # Show recent sessions
        rows = db.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 5"
        ).fetchall()

        if rows:
            console.print("\n[bold]Recent Sessions:[/bold]")
            table = Table()
            table.add_column("ID", style="dim", max_width=12)
            table.add_column("Pipeline")
            table.add_column("Repo")
            table.add_column("Status")

            for row in rows:
                status_style = {
                    "done": "green",
                    "failed": "red",
                    "running": "yellow",
                    "blocked": "blue",
                }.get(row["status"], "white")

                table.add_row(
                    row["id"][:12],
                    row["pipeline"],
                    row["repo"],
                    f"[{status_style}]{row['status']}[/{status_style}]",
                )
            console.print(table)
        else:
            console.print("\n[dim]No sessions recorded yet[/dim]")
    except sqlite3.Error as e:
        console.print(f"[red]Database error:[/red] {e}")
        raise typer.Exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    app()
