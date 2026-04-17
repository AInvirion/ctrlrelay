"""CLI entry point for dev-sync."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from dev_sync import __version__
from dev_sync.core.config import ConfigError, load_config

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


# Skills subcommand group
skills_app = typer.Typer(help="Skill management commands.")
app.add_typer(skills_app, name="skills")


def _resolve_skills_dir(skills_path: str | None, config_path: str) -> Path:
    """Resolve skills directory from flag or config."""
    if skills_path is not None:
        skills_dir = Path(skills_path).expanduser().resolve()
    else:
        try:
            config = load_config(config_path)
            skills_dir = config.paths.skills.expanduser().resolve()
        except ConfigError as e:
            console.print(f"[red]Error loading config:[/red] {e}")
            console.print("Use --path to specify skills directory directly.")
            raise typer.Exit(1)

    if not skills_dir.exists():
        console.print(f"[red]Skills directory not found:[/red] {skills_dir}")
        raise typer.Exit(1)

    return skills_dir


@skills_app.command("audit")
def skills_audit(
    skills_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Path to skills directory (default: from config)",
    ),
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Audit skills for orchestrator readiness."""
    from dev_sync.core.audit import audit_all, format_report

    skills_dir = _resolve_skills_dir(skills_path, config_path)

    console.print(f"Auditing skills in: {skills_dir}\n")

    audits = audit_all(skills_dir)

    if not audits:
        console.print("[yellow]No skills found.[/yellow]")
        return

    report = format_report(audits)
    console.print(report)

    # Exit with error if any skills not ready
    if not all(a.passed for a in audits):
        raise typer.Exit(1)


@skills_app.command("list")
def skills_list(
    skills_path: str = typer.Option(
        None,
        "--path",
        "-p",
        help="Path to skills directory (default: from config)",
    ),
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """List available skills."""
    from dev_sync.core.audit import discover_skills

    skills_dir = _resolve_skills_dir(skills_path, config_path)

    skills = discover_skills(skills_dir)

    if not skills:
        console.print("[yellow]No skills found.[/yellow]")
        return

    table = Table(title="Available Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Path", style="dim")

    for skill in skills:
        table.add_row(skill.name, str(skill.path))

    console.print(table)


# Bridge subcommand group
bridge_app = typer.Typer(help="Telegram bridge commands.")
app.add_typer(bridge_app, name="bridge")


def _get_socket_path(config_path: str) -> Path:
    """Get socket path from config."""
    try:
        config = load_config(config_path)
        if config.transport.telegram:
            return config.transport.telegram.socket_path.expanduser().resolve()
    except ConfigError:
        pass
    return Path("~/.dev-sync/dev-sync.sock").expanduser().resolve()


def _get_bridge_pid_file(socket_path: Path) -> Path:
    """Get PID file path for bridge process."""
    return socket_path.with_suffix(".pid")


@bridge_app.command("start")
def bridge_start(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
    daemon: bool = typer.Option(
        False,
        "--daemon",
        "-d",
        help="Run in background",
    ),
) -> None:
    """Start the Telegram bridge."""
    import os
    import subprocess
    import sys

    try:
        config = load_config(config_path)
    except ConfigError as e:
        console.print(f"[red]Error loading config:[/red] {e}")
        raise typer.Exit(1)

    if config.transport.type.value != "telegram":
        console.print("[yellow]Transport is not set to 'telegram' in config.[/yellow]")
        console.print("Set transport.type: telegram to use the bridge.")
        raise typer.Exit(1)

    telegram_config = config.transport.telegram
    if not telegram_config:
        console.print("[red]Telegram config not found.[/red]")
        raise typer.Exit(1)

    socket_path = telegram_config.socket_path.expanduser().resolve()
    pid_file = _get_bridge_pid_file(socket_path)

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            console.print(f"[yellow]Bridge already running (PID {pid})[/yellow]")
            raise typer.Exit(1)
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    bot_token = os.environ.get(telegram_config.bot_token_env)
    if not bot_token:
        env_var = telegram_config.bot_token_env
        console.print(f"[red]Bot token not found.[/red] Set {env_var} environment variable.")
        raise typer.Exit(1)

    if daemon:
        cmd = [
            sys.executable,
            "-m",
            "dev_sync.bridge",
            "--socket-path",
            str(socket_path),
            "--bot-token",
            bot_token,
            "--chat-id",
            str(telegram_config.chat_id),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file.write_text(str(proc.pid))
        console.print(f"[green]Bridge started (PID {proc.pid})[/green]")
    else:
        import asyncio

        from dev_sync.bridge import BridgeServer

        console.print(f"Starting bridge on {socket_path}")
        console.print("Press Ctrl+C to stop")

        server = BridgeServer(
            socket_path=socket_path,
            bot_token=bot_token,
            chat_id=telegram_config.chat_id,
        )

        try:
            asyncio.run(server.start())
        except KeyboardInterrupt:
            console.print("\n[yellow]Shutting down...[/yellow]")


@bridge_app.command("stop")
def bridge_stop(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Stop the Telegram bridge."""
    import os
    import signal

    socket_path = _get_socket_path(config_path)
    pid_file = _get_bridge_pid_file(socket_path)

    if not pid_file.exists():
        console.print("[yellow]Bridge not running (no PID file)[/yellow]")
        return

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Stopped bridge (PID {pid})[/green]")
        pid_file.unlink(missing_ok=True)
    except ProcessLookupError:
        console.print("[yellow]Bridge process not found[/yellow]")
        pid_file.unlink(missing_ok=True)
    except ValueError:
        console.print("[red]Invalid PID file[/red]")
        pid_file.unlink(missing_ok=True)


@bridge_app.command("status")
def bridge_status(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Check bridge status."""
    import os

    socket_path = _get_socket_path(config_path)
    pid_file = _get_bridge_pid_file(socket_path)

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            console.print(f"[green]Bridge running (PID {pid})[/green]")
            console.print(f"Socket: {socket_path}")
            return
        except (ProcessLookupError, ValueError):
            pass

    if socket_path.exists():
        console.print("[yellow]Socket exists but no running process[/yellow]")
        console.print(f"Socket: {socket_path}")
    else:
        console.print("[dim]Bridge not running[/dim]")


@bridge_app.command("test")
def bridge_test(
    message: str = typer.Option(
        "Test message from dev-sync bridge",
        "--message",
        "-m",
        help="Message to send",
    ),
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Send a test message to verify bridge is working."""
    import asyncio

    socket_path = _get_socket_path(config_path)

    if not socket_path.exists():
        console.print("[red]Bridge not running.[/red] Start it with: dev-sync bridge start")
        raise typer.Exit(1)

    async def send_test():
        from dev_sync.transports import SocketTransport

        transport = SocketTransport(socket_path)
        try:
            await transport.connect()
            await transport.send(message)
            console.print("[green]Message sent successfully![/green]")
        finally:
            await transport.close()

    try:
        asyncio.run(send_test())
    except Exception as e:
        console.print(f"[red]Failed to send message:[/red] {e}")
        raise typer.Exit(1)


# Run subcommand group
run_app = typer.Typer(help="Pipeline execution commands.")
app.add_typer(run_app, name="run")


@run_app.command("secops")
def run_secops(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
    repo: str = typer.Option(
        None,
        "--repo",
        "-r",
        help="Run on specific repo only",
    ),
) -> None:
    """Run secops pipeline on configured repos."""
    import asyncio

    from dev_sync.core.dispatcher import ClaudeDispatcher
    from dev_sync.core.github import GitHubCLI
    from dev_sync.core.state import StateDB
    from dev_sync.core.worktree import WorktreeManager
    from dev_sync.dashboard.client import DashboardClient
    from dev_sync.pipelines.secops import run_secops_all

    path = Path(config_path)

    try:
        config = load_config(path)
    except ConfigError as e:
        console.print(f"[red]Error loading config:[/red] {e}")
        raise typer.Exit(1)

    repos = config.repos
    if repo:
        repos = [r for r in repos if r.name == repo]
        if not repos:
            console.print(f"[red]Repo not found:[/red] {repo}")
            raise typer.Exit(1)

    if not repos:
        console.print("[yellow]No repos configured.[/yellow]")
        return

    db = StateDB(config.paths.state_db)
    dispatcher = ClaudeDispatcher(
        claude_binary=config.claude.binary,
        default_timeout=config.claude.default_timeout_seconds,
    )
    github = GitHubCLI()
    worktree = WorktreeManager(
        worktrees_dir=config.paths.worktrees,
        bare_repos_dir=config.paths.bare_repos,
    )

    dashboard = None
    if config.dashboard.enabled and config.dashboard.url:
        import os
        token = os.environ.get(config.dashboard.auth_token_env, "")
        if token:
            dashboard = DashboardClient(
                url=config.dashboard.url,
                auth_token=token,
                node_id=config.node_id,
                queue_dir=config.paths.state_db.parent / "event_queue",
            )

    console.print(f"Running secops on {len(repos)} repo(s)...")

    async def _run():
        return await run_secops_all(
            repos=repos,
            dispatcher=dispatcher,
            github=github,
            worktree=worktree,
            dashboard=dashboard,
            state_db=db,
            transport=None,
            contexts_dir=config.paths.contexts,
        )

    try:
        results = asyncio.run(_run())
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    finally:
        db.close()

    success_count = sum(1 for r in results if r.success)
    console.print(f"\n[bold]Results:[/bold] {success_count}/{len(results)} succeeded")

    for result in results:
        status = "[green]OK[/green]" if result.success else "[red]FAIL[/red]"
        console.print(f"  {status} {result.summary}")

    if not all(r.success for r in results):
        raise typer.Exit(1)


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
