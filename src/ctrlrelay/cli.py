"""CLI entry point for ctrlrelay."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ctrlrelay import __version__
from ctrlrelay.core.config import ConfigError, load_config

app = typer.Typer(
    name="ctrlrelay",
    help="Local-first orchestrator for Claude Code across multiple GitHub repos.",
    no_args_is_help=True,
)
console = Console()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"ctrlrelay version {__version__}")
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
    """ctrlrelay orchestrator CLI."""


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
    from ctrlrelay.core.audit import audit_all, format_report

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
    from ctrlrelay.core.audit import discover_skills

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
    return Path("~/.ctrlrelay/ctrlrelay.sock").expanduser().resolve()


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
    foreground: bool = typer.Option(
        False,
        "--foreground",
        "-F",
        help="Run in the foreground (for launchd/systemd/debugging). Default is to daemonize.",
    ),
) -> None:
    """Start the Telegram bridge.

    Daemonizes by default so the terminal returns to you. Pass --foreground
    under a process supervisor (launchd Type=simple, systemd Type=simple) or
    when debugging interactively.
    """
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

    pid_file.parent.mkdir(parents=True, exist_ok=True)

    if foreground:
        import asyncio
        import signal

        from ctrlrelay.bridge import BridgeServer

        # Install early SIGTERM/SIGINT handlers so a supervisor stop between
        # now and loop.add_signal_handler below still runs the `finally`
        # that unlinks the PID file. loop.add_signal_handler replaces them
        # once the asyncio loop is running.
        def _raise_systemexit_on_signal(sig: int, _frame: object) -> None:
            raise SystemExit(0)

        for _sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(_sig, _raise_systemexit_on_signal)

        pid_file.write_text(str(os.getpid()))
        console.print(f"Starting bridge on {socket_path}")
        console.print("Press Ctrl+C to stop")

        server = BridgeServer(
            socket_path=socket_path,
            bot_token=bot_token,
            chat_id=telegram_config.chat_id,
        )

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)

            async def _run_server() -> None:
                # Wrap start() in a finally that awaits stop() so the loop
                # can't close before _telegram.close() and the socket unlink
                # have actually completed. Scheduling stop() as a bare task
                # in the signal handler would not guarantee that ordering.
                try:
                    await server.start()
                finally:
                    await server.stop()

            main_task = loop.create_task(_run_server())

            def _handle_stop(sig: int) -> None:
                main_task.cancel()

            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, _handle_stop, sig)

            try:
                loop.run_until_complete(main_task)
            except asyncio.CancelledError:
                pass
        finally:
            loop.close()
            pid_file.unlink(missing_ok=True)
    else:
        # Pass the token via environment, never argv. Putting it on the command
        # line would expose it to anyone who can read `ps` / /proc/*/cmdline.
        cmd = [
            sys.executable,
            "-m",
            "ctrlrelay.bridge",
            "--socket-path",
            str(socket_path),
            "--bot-token-env",
            telegram_config.bot_token_env,
            "--chat-id",
            str(telegram_config.chat_id),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Claim the PID file BEFORE the liveness probe; a second concurrent
        # `bridge start` in the 1-second window would otherwise see no PID
        # file, spawn its own child, and both would rebind the shared socket.
        pid_file.write_text(str(proc.pid))
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            console.print(f"[green]Bridge started (PID {proc.pid})[/green]")
            return
        # Child exited within 1s. Zero = clean no-op; non-zero = crash.
        pid_file.unlink(missing_ok=True)
        if proc.returncode == 0:
            console.print(
                "[yellow]Bridge exited immediately with no work to do.[/yellow]"
            )
            return
        console.print(
            f"[red]Bridge failed to start[/red] "
            f"(child exited with code {proc.returncode})"
        )
        raise typer.Exit(1)


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
        console.print("[yellow]Socket exists but no PID file[/yellow]")
        console.print(
            "[dim]The bridge may be running under a supervisor that pre-dates "
            "the PID-file change — restart it to refresh state.[/dim]"
        )
        console.print(f"Socket: {socket_path}")
        raise typer.Exit(1)

    console.print("[dim]Bridge not running[/dim]")
    raise typer.Exit(1)


@bridge_app.command("test")
def bridge_test(
    message: str = typer.Option(
        "Test message from ctrlrelay bridge",
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
        console.print("[red]Bridge not running.[/red] Start it with: ctrlrelay bridge start")
        raise typer.Exit(1)

    async def send_test():
        from ctrlrelay.transports import SocketTransport

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

    from ctrlrelay.core.dispatcher import make_agent_dispatcher
    from ctrlrelay.core.github import GitHubCLI
    from ctrlrelay.core.state import StateDB
    from ctrlrelay.core.worktree import WorktreeManager
    from ctrlrelay.dashboard.client import DashboardClient
    from ctrlrelay.pipelines.secops import run_secops_all

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
    dispatcher = make_agent_dispatcher(config.agent)
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
        # Surface the blocking question / error text so an operator
        # running secops at the CLI doesn't have to dig into state.db
        # to see what the agent was asking. The scheduler closure
        # already relays these via Telegram; this is the manual path.
        if result.blocked and result.question:
            console.print(f"    [yellow]Question:[/yellow] {result.question}")
        elif not result.success and result.error:
            console.print(f"    [red]Error:[/red] {result.error}")

    if not all(r.success for r in results):
        raise typer.Exit(1)


@run_app.command("dev")
def run_dev(
    issue: int = typer.Option(
        ...,
        "--issue",
        "-i",
        help="GitHub issue number to implement",
    ),
    repo: str = typer.Option(
        None,
        "--repo",
        "-r",
        help="Run on specific repo only",
    ),
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Run dev pipeline for a GitHub issue."""
    import asyncio

    from ctrlrelay.core.dispatcher import make_agent_dispatcher
    from ctrlrelay.core.github import GitHubCLI
    from ctrlrelay.core.state import StateDB
    from ctrlrelay.core.worktree import WorktreeManager
    from ctrlrelay.pipelines.dev import run_dev_issue

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

    if len(repos) > 1 and not repo:
        console.print(
            "[red]Error:[/red] Multiple repos configured. "
            "Use --repo to specify which one."
        )
        raise typer.Exit(1)

    repo_config = repos[0]
    branch_template = repo_config.dev_branch_template

    db = StateDB(config.paths.state_db)
    dispatcher = make_agent_dispatcher(config.agent)
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
            from ctrlrelay.dashboard.client import DashboardClient
            dashboard = DashboardClient(
                url=config.dashboard.url,
                auth_token=token,
                node_id=config.node_id,
                queue_dir=config.paths.state_db.parent / "event_queue",
            )

    console.print(f"Running dev pipeline for issue #{issue} on {repo_config.name}...")

    async def _run():
        return await run_dev_issue(
            repo=repo_config.name,
            issue_number=issue,
            branch_template=branch_template,
            dispatcher=dispatcher,
            github=github,
            worktree=worktree,
            dashboard=dashboard,
            state_db=db,
            transport=None,
            contexts_dir=config.paths.contexts,
        )

    try:
        result = asyncio.run(_run())
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    finally:
        db.close()

    if result.success:
        pr_url = result.outputs.get("pr_url", "") if result.outputs else ""
        console.print(f"[green]Success:[/green] {result.summary}")
        if pr_url:
            console.print(f"  PR: {pr_url}")
    elif result.blocked:
        console.print(f"[blue]Blocked:[/blue] {result.summary}")
        if result.question:
            console.print(f"  Question: {result.question}")
        raise typer.Exit(1)
    else:
        console.print(f"[red]Failed:[/red] {result.summary}")
        if result.error:
            console.print(f"  Error: {result.error}")
        raise typer.Exit(1)


# Poller subcommand group
poller_app = typer.Typer(help="Issue poller commands.")
app.add_typer(poller_app, name="poller")


def _get_poller_pid_file(config_path: str) -> Path:
    """Get PID file path for poller process."""
    try:
        config = load_config(config_path)
        return config.paths.state_db.parent / "poller.pid"
    except ConfigError:
        pass
    return Path("~/.ctrlrelay/poller.pid").expanduser().resolve()


@poller_app.command("start")
def poller_start(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
    foreground: bool = typer.Option(
        False,
        "--foreground",
        "-F",
        help="Run in the foreground (for launchd/systemd/debugging). Default is to daemonize.",
    ),
    interval: int = typer.Option(
        300,
        "--interval",
        "-i",
        help="Polling interval in seconds",
    ),
) -> None:
    """Start the issue poller.

    Daemonizes by default so the terminal returns to you. Pass --foreground
    under a process supervisor (launchd Type=simple, systemd Type=simple) or
    when debugging interactively.
    """
    import asyncio
    import os
    import signal
    import subprocess
    import sys

    try:
        config = load_config(Path(config_path))
    except ConfigError as e:
        console.print(f"[red]Error loading config:[/red] {e}")
        raise typer.Exit(1)

    pid_file = _get_poller_pid_file(config_path)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if pid == os.getpid():
                # The daemon parent wrote our own PID here before spawning us
                # as the `--foreground` child. Don't treat it as a conflict.
                pass
            else:
                os.kill(pid, 0)
                console.print(f"[yellow]Poller already running (PID {pid})[/yellow]")
                raise typer.Exit(1)
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    if not foreground:
        cmd = [
            sys.executable,
            "-m",
            "ctrlrelay.cli",
            "poller",
            "start",
            "--config",
            config_path,
            "--interval",
            str(interval),
            "--foreground",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Claim the PID file BEFORE the liveness probe; otherwise a second
        # concurrent `start` in the 1-second window would see no PID file and
        # spawn a duplicate poller.
        pid_file.write_text(str(proc.pid))
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            console.print(f"[green]Poller started (PID {proc.pid})[/green]")
            return
        # Child exited. Zero = clean no-op (e.g. `repos: []`); non-zero = crash.
        pid_file.unlink(missing_ok=True)
        if proc.returncode == 0:
            console.print(
                "[yellow]Poller exited immediately with no work to do "
                "(check `repos:` in your config).[/yellow]"
            )
            return
        console.print(
            f"[red]Poller failed to start[/red] "
            f"(child exited with code {proc.returncode})"
        )
        raise typer.Exit(1)

    # Install SIGTERM/SIGINT handlers BEFORE any startup work so a supervisor
    # stop during `gh api user` / `seed_current()` still unwinds through the
    # `finally` that unlinks poller.pid. Converting the signal into SystemExit
    # lets Python's normal unwind run `finally` blocks. Once the asyncio loop
    # is up below, `loop.add_signal_handler` overrides these to drive a
    # graceful cancel of the poll loop.
    def _raise_systemexit_on_signal(sig: int, _frame: object) -> None:
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _raise_systemexit_on_signal)

    pid_file.write_text(str(os.getpid()))
    try:
        from ctrlrelay.core.dispatcher import make_agent_dispatcher
        from ctrlrelay.core.github import GitHubCLI
        from ctrlrelay.core.poller import IssuePoller, run_poll_loop
        from ctrlrelay.core.scheduler import make_scheduler
        from ctrlrelay.core.state import StateDB
        from ctrlrelay.core.worktree import WorktreeManager
        from ctrlrelay.pipelines.dev import run_dev_issue
        from ctrlrelay.pipelines.post_merge import pr_watch_task
        from ctrlrelay.pipelines.secops import run_secops_all

        # Build a DashboardClient if configured BEFORE the gh probe runs,
        # so even if gh fails the user gets clear failure ordering and so
        # tests can short-circuit at gh while still observing this wiring.
        # Mirrors what `ctrlrelay run secops` does for the manual path.
        scheduled_dashboard = None
        if config.dashboard.enabled and config.dashboard.url:
            token = os.environ.get(config.dashboard.auth_token_env, "")
            if token:
                from ctrlrelay.dashboard.client import DashboardClient
                scheduled_dashboard = DashboardClient(
                    url=config.dashboard.url,
                    auth_token=token,
                    node_id=config.node_id,
                    queue_dir=config.paths.state_db.parent / "event_queue",
                )

        github = GitHubCLI()

        # Get GitHub username
        try:
            from ctrlrelay.core.github import _find_gh
            gh_bin = _find_gh()
            result = subprocess.run(
                [gh_bin, "api", "user", "--jq", ".login"],
                capture_output=True,
                text=True,
                check=True,
            )
            username = result.stdout.strip()
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Failed to get GitHub username:[/red] {e}")
            raise typer.Exit(1)

        if not username:
            console.print("[red]Could not determine GitHub username.[/red]")
            raise typer.Exit(1)

        repo_names = [r.name for r in config.repos]
        if not repo_names:
            console.print("[yellow]No repos configured.[/yellow]")
            return

        state_file = config.paths.state_db.parent / "poller_state.json"

        first_run = not state_file.exists()

        poller = IssuePoller(
            github=github,
            username=username,
            repos=repo_names,
            state_file=state_file,
        )

        # NOTE: first-run seeding moved into `_main()` so the APScheduler
        # cron is registered + running BEFORE the slow seed_current() pass
        # (one GitHub API call per repo) takes place. Otherwise the 6am
        # scheduled fire can pass during startup and APScheduler's misfire
        # grace only catches up on fires that happened AFTER registration.

        state_db = StateDB(config.paths.state_db)
        dispatcher = make_agent_dispatcher(config.agent)
        worktree = WorktreeManager(
            worktrees_dir=config.paths.worktrees,
            bare_repos_dir=config.paths.bare_repos,
        )

        # Set up transport for notifications
        transport = None
        if config.transport.type.value == "telegram" and config.transport.telegram:
            from ctrlrelay.transports import SocketTransport
            socket_path = config.transport.telegram.socket_path.expanduser().resolve()
            if socket_path.exists():
                transport = SocketTransport(socket_path)
                console.print(f"[dim]Telegram transport enabled via {socket_path}[/dim]")
            else:
                console.print(f"[yellow]Telegram socket not found at {socket_path}[/yellow]")
                console.print(
                    "[yellow]Run 'ctrlrelay bridge start' to enable notifications[/yellow]"
                )

        # Track in-flight PR-watch background tasks so they outlive
        # handle_issue and aren't garbage-collected. Cleared via
        # done_callback as each task terminates.
        pr_watch_tasks: set[asyncio.Task] = set()

        async def _watch_transport_factory():
            """Build a fresh connected SocketTransport for a single watcher,
            independent of the transport used in handle_issue (which is
            closed on exit).

            Return None ONLY when Telegram notifications aren't
            configured at all — that's a legitimate "no channel" signal
            and the retry loop treats it as clean success. A configured-
            but-currently-missing socket is a transient outage (bridge
            restart, daemon crash mid-merge), so RAISE to trigger the
            retry path; the next attempt will reach the socket once the
            bridge is back.
            """
            if config.transport.type.value != "telegram" or not config.transport.telegram:
                return None
            from ctrlrelay.transports import SocketTransport
            socket_path = config.transport.telegram.socket_path.expanduser().resolve()
            if not socket_path.exists():
                raise FileNotFoundError(
                    f"Telegram bridge socket missing at {socket_path}; "
                    "retryable — bridge may be restarting"
                )
            watch_transport = SocketTransport(socket_path)
            await watch_transport.connect()
            return watch_transport

        async def handle_issue(repo: str, issue: dict) -> None:
            issue_number = issue["number"]
            title = issue.get("title", "")
            console.print(
                f"[green]New issue detected:[/green] #{issue_number} in {repo} — {title}"
            )

            # Connect transport for notifications
            connected_transport = None
            if transport:
                try:
                    await transport.connect()
                    connected_transport = transport
                    await transport.send(f"🔔 New issue #{issue_number} in {repo}: {title}")
                except Exception as e:
                    console.print(f"[yellow]Transport error: {e}[/yellow]")

            # Find matching repo config
            repo_configs = [r for r in config.repos if r.name == repo]
            if not repo_configs:
                console.print(f"[yellow]No config found for repo {repo}, skipping.[/yellow]")
                if connected_transport:
                    await transport.close()
                return

            repo_config = repo_configs[0]
            try:
                result = await run_dev_issue(
                    repo=repo,
                    issue_number=issue_number,
                    branch_template=repo_config.dev_branch_template,
                    dispatcher=dispatcher,
                    github=github,
                    worktree=worktree,
                    dashboard=None,
                    state_db=state_db,
                    transport=connected_transport,
                    contexts_dir=config.paths.contexts,
                )

                # Lock-conflict retry hook. The poller marks issues seen
                # BEFORE handle_issue runs, so a failed attempt would
                # permanently drop the issue. If run_dev_issue couldn't
                # acquire the per-repo lock (common when a scheduled
                # secops sweep is mid-run on the same repo), un-mark the
                # issue so the next poll picks it up. Any other failure
                # still stays seen — those aren't transient.
                if (
                    not result.success
                    and not result.blocked
                    and result.error
                    and "locked by another session" in result.error.lower()
                ):
                    poller.unmark_seen(repo, issue_number)
                    console.print(
                        f"[yellow]#{issue_number} in {repo}: repo locked "
                        "(secops running?) — un-marked for retry next "
                        "poll.[/yellow]"
                    )
                    if connected_transport:
                        try:
                            await transport.send(
                                f"⏳ #{issue_number} in {repo} "
                                "deferred (repo busy); will retry."
                            )
                        except Exception:
                            pass
                    return

                # Spawn the PR watcher FIRST, before any best-effort
                # notification. The poller has already marked this issue
                # as seen in poller_state.json, so if a transient
                # transport.send failure below raised through the outer
                # finally, we'd permanently lose the watcher and the
                # issue would never auto-close on merge.
                pr_number_raw = result.outputs.get("pr_number")
                pr_url_str = result.outputs.get("pr_url", "")
                if pr_number_raw is not None:
                    try:
                        pr_number = int(pr_number_raw)
                    except (TypeError, ValueError):
                        pr_number = None
                    if pr_number is not None:
                        task = asyncio.create_task(
                            pr_watch_task(
                                repo=repo,
                                issue_number=issue_number,
                                pr_url=pr_url_str,
                                pr_number=pr_number,
                                session_id=result.session_id,
                                github=github,
                                transport_factory=_watch_transport_factory,
                            )
                        )
                        pr_watch_tasks.add(task)
                        task.add_done_callback(pr_watch_tasks.discard)

                # Send result notification — best-effort. A failed send
                # must NOT prevent the merge watcher (spawned above)
                # from running, so swallow transport errors here.
                if connected_transport:
                    try:
                        if result.success:
                            pr_url = result.outputs.get("pr_url", "")
                            await transport.send(f"✅ PR ready: {pr_url}")
                        elif result.blocked:
                            await transport.send(
                                f"⏸️ Blocked on #{issue_number}: {result.question}"
                            )
                        else:
                            await transport.send(
                                f"❌ Failed on #{issue_number}: "
                                f"{result.error or result.summary}"
                            )
                    except Exception as e:
                        console.print(
                            f"[yellow]Transport error sending result: {e}[/yellow]"
                        )
            finally:
                if connected_transport:
                    await transport.close()

        console.print(f"[green]Starting poller[/green] for {len(repo_names)} repo(s) as {username}")
        console.print(f"  Interval: {interval}s | Press Ctrl+C to stop")

        async def _run_scheduled_secops() -> None:
            """Scheduler callback: run the secops sweep across all repos.

            Shares the poller's open state_db, github, dispatcher, and
            worktree. Per-repo locks in the state DB prevent collisions
            with an in-flight dev pipeline (and the dev handler now
            retries on lock-conflict so issues aren't silently dropped).

            Builds a fresh SocketTransport per run so blocked/failed
            results notify Telegram — the dashboard only pushes for
            successful runs, so without a transport here operators would
            lose visibility into scheduled failures.
            """
            if not config.repos:
                return
            n_repos = len(config.repos)
            console.print(
                f"[dim]Scheduled secops: starting across {n_repos} repo(s)[/dim]"
            )

            secops_transport = None
            if config.transport.type.value == "telegram" and config.transport.telegram:
                from ctrlrelay.transports import SocketTransport
                sock = config.transport.telegram.socket_path.expanduser().resolve()
                if sock.exists():
                    try:
                        candidate = SocketTransport(sock)
                        await candidate.connect()
                        secops_transport = candidate
                    except Exception as e:
                        console.print(
                            f"[yellow]Scheduled secops: transport connect "
                            f"failed ({e}) — running without notifications[/yellow]"
                        )

            # Tell the operator the sweep started so a long run isn't
            # silent. Without this, a 10-min sweep that ends with a
            # blocked-on-input result looks like "out of nowhere" pings.
            if secops_transport:
                try:
                    await secops_transport.send(
                        f"🔄 Scheduled secops: starting sweep across "
                        f"{n_repos} repo(s)"
                    )
                except Exception as e:
                    console.print(
                        f"[yellow]Scheduled secops: start-notify failed: "
                        f"{e}[/yellow]"
                    )

            try:
                results = await run_secops_all(
                    repos=config.repos,
                    dispatcher=dispatcher,
                    github=github,
                    worktree=worktree,
                    dashboard=scheduled_dashboard,
                    state_db=state_db,
                    transport=secops_transport,
                    contexts_dir=config.paths.contexts,
                )
                ok = sum(1 for r in results if r.success)
                console.print(
                    f"[dim]Scheduled secops: {ok}/{len(results)} succeeded[/dim]"
                )

                # Per-repo notifications for the cases an operator must
                # act on. The aggregate "N blocked" message we used to
                # send was useless on its own — it didn't say which
                # repos blocked or what the question was. Fan out one
                # message per blocked or failed result with the actual
                # question/error and session id so the operator can
                # respond directly via the bridge.
                #
                # `run_secops_all` returns results in the same order as
                # the input `repos` list, so zip is safe — only repos
                # with successful lock-acquisition produce results.
                if secops_transport:
                    try:
                        for repo_cfg, result in zip(
                            config.repos, results, strict=False
                        ):
                            if result.blocked:
                                question = (
                                    result.question or "(no question text)"
                                )
                                await secops_transport.send(
                                    f"⏸️ Scheduled secops blocked on "
                                    f"{repo_cfg.name}\n"
                                    f"Session: `{result.session_id}`\n"
                                    f"\n{question}"
                                )
                            elif not result.success:
                                err = result.error or result.summary
                                await secops_transport.send(
                                    f"❌ Scheduled secops failed on "
                                    f"{repo_cfg.name}\n"
                                    f"Session: `{result.session_id}`\n"
                                    f"\n{err}"
                                )
                        # Final at-a-glance summary — kept because it's
                        # the single message the operator scans first.
                        blocked_n = sum(1 for r in results if r.blocked)
                        failed_n = sum(
                            1 for r in results
                            if not r.success and not r.blocked
                        )
                        if blocked_n or failed_n:
                            parts = []
                            if blocked_n:
                                parts.append(f"{blocked_n} blocked")
                            if failed_n:
                                parts.append(f"{failed_n} failed")
                            await secops_transport.send(
                                f"📋 Scheduled secops sweep done: "
                                f"{ok}/{len(results)} ok, "
                                f"{', '.join(parts)}"
                            )
                        else:
                            await secops_transport.send(
                                f"✅ Scheduled secops sweep done: "
                                f"{ok}/{len(results)} ok"
                            )
                    except Exception as e:
                        console.print(
                            f"[yellow]Scheduled secops: notify failed: {e}[/yellow]"
                        )
            finally:
                if secops_transport:
                    try:
                        await secops_transport.close()
                    except Exception:
                        pass

        async def _main() -> None:
            # Register + start the scheduler FIRST, before any potentially
            # slow startup work. Otherwise a 6am fire that lands during
            # seed_current's per-repo GitHub calls would be lost —
            # APScheduler's misfire_grace_time only rescues fires that
            # happened AFTER the job was registered.
            scheduler = make_scheduler(timezone=config.timezone)
            scheduler.add_cron_job(
                name="secops",
                cron_expr=config.schedules.secops_cron,
                func=_run_scheduled_secops,
            )
            scheduler.start()
            console.print(
                f"[dim]Scheduler: secops cron={config.schedules.secops_cron} "
                f"tz={config.timezone}[/dim]"
            )

            # Now the slow startup: first-run seeding (one gh call per
            # repo). Done inside _main so the scheduler is already up.
            if first_run:
                console.print(
                    "[dim]First run: seeding with current assignments..."
                    "[/dim]"
                )
                await poller.seed_current()

            try:
                await run_poll_loop(
                    poller=poller, handler=handle_issue, interval=interval,
                )
            finally:
                # Scheduler shutdown is async so it can cancel + await any
                # in-flight job tasks (e.g. a scheduled secops sweep that was
                # running when SIGTERM arrived). Without awaiting here the
                # loop closes before jobs finalize — state_db locks stay
                # held and worktrees stay dirty.
                await scheduler.shutdown()
                # Cancel any in-flight PR watchers so a poller stop/restart
                # doesn't leak asyncio tasks. Their handlers log
                # dev.pr.watch_cancelled and close their transport via the
                # finally block.
                if pr_watch_tasks:
                    for t in list(pr_watch_tasks):
                        t.cancel()
                    await asyncio.gather(*list(pr_watch_tasks), return_exceptions=True)

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            main_task = loop.create_task(_main())

            def _handle_stop(sig: int) -> None:
                main_task.cancel()

            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, _handle_stop, sig)

            try:
                loop.run_until_complete(main_task)
            except asyncio.CancelledError:
                console.print("\n[yellow]Poller stopped.[/yellow]")
        finally:
            loop.close()
            state_db.close()
    finally:
        pid_file.unlink(missing_ok=True)


@poller_app.command("stop")
def poller_stop(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Stop the issue poller."""
    import os
    import signal

    pid_file = _get_poller_pid_file(config_path)

    if not pid_file.exists():
        console.print("[yellow]Poller not running (no PID file)[/yellow]")
        return

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Stopped poller (PID {pid})[/green]")
        pid_file.unlink(missing_ok=True)
    except ProcessLookupError:
        console.print("[yellow]Poller process not found[/yellow]")
        pid_file.unlink(missing_ok=True)
    except ValueError:
        console.print("[red]Invalid PID file[/red]")
        pid_file.unlink(missing_ok=True)


@poller_app.command("status")
def poller_status(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Check poller status."""
    import os

    pid_file = _get_poller_pid_file(config_path)

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            console.print(f"[green]Poller running (PID {pid})[/green]")
            return
        except (ProcessLookupError, ValueError):
            pass

    console.print("[dim]Poller not running[/dim]")
    raise typer.Exit(1)


@app.command("version")
def version() -> None:
    """Print the package version."""
    console.print(__version__)


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
    from ctrlrelay.core.state import StateDB

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
