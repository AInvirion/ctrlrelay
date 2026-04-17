# dev-sync Phase 0: Package Skeleton

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a working PyPI-installable package with CLI skeleton and config validation.

**Architecture:** Python package using `uv` for dependency management, Typer for CLI, Pydantic for config validation, SQLite for state. The package structure follows `src/` layout for proper isolation.

**Tech Stack:** Python 3.12, uv, Typer, Pydantic, SQLite, PyYAML

**Phase Gate:** `pip install -e .` works, `dev-sync config validate` passes on example config.

---

## File Structure

```
src/dev_sync/
├── __init__.py           # Version, public API exports
├── cli.py                # Typer CLI entry point
└── core/
    ├── __init__.py       # Core module exports
    ├── config.py         # Pydantic models + YAML loader
    └── state.py          # SQLite schema + access layer
tests/
├── __init__.py
├── conftest.py           # Pytest fixtures
├── test_config.py        # Config loading tests
└── test_state.py         # SQLite state tests
config/
└── orchestrator.yaml.example
pyproject.toml
```

---

### Task 1: Create pyproject.toml

**Files:**
- Create: `pyproject.toml`

- [ ] **Step 1: Create pyproject.toml with metadata and dependencies**

```toml
[project]
name = "dev-sync"
version = "0.1.0"
description = "Local-first orchestrator for Claude Code across multiple GitHub repos"
readme = "README.md"
requires-python = ">=3.12"
license = {text = "MIT"}
authors = [
    {name = "Oscar Valenzuela", email = "oscar.valenzuela.b@gmail.com"}
]
keywords = ["claude", "orchestrator", "automation", "github"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Environment :: Console",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.12",
    "Topic :: Software Development :: Build Tools",
]
dependencies = [
    "typer>=0.12.0",
    "pydantic>=2.0.0",
    "pyyaml>=6.0.0",
    "rich>=13.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-cov>=4.0.0",
    "ruff>=0.4.0",
]

[project.scripts]
dev-sync = "dev_sync.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/dev_sync"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 2: Create src directory structure**

Run:
```bash
mkdir -p src/dev_sync/core tests config
```

- [ ] **Step 3: Verify structure**

Run:
```bash
ls -la src/dev_sync/
```
Expected: `core/` directory exists

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "feat: add pyproject.toml with package metadata"
```

---

### Task 2: Create package __init__.py with version

**Files:**
- Create: `src/dev_sync/__init__.py`
- Create: `src/dev_sync/core/__init__.py`

- [ ] **Step 1: Write src/dev_sync/__init__.py**

```python
"""dev-sync: Local-first orchestrator for Claude Code."""

__version__ = "0.1.0"

# Public API - will be populated as modules are added
__all__ = ["__version__"]
```

- [ ] **Step 2: Write src/dev_sync/core/__init__.py**

```python
"""Core functionality for dev-sync orchestrator."""
```

- [ ] **Step 3: Verify imports work**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && python -c "from dev_sync import __version__; print(__version__)"
```
Expected: `0.1.0`

- [ ] **Step 4: Commit**

```bash
git add src/dev_sync/__init__.py src/dev_sync/core/__init__.py
git commit -m "feat: add package init files with version"
```

---

### Task 3: Create CLI skeleton with Typer

**Files:**
- Create: `src/dev_sync/cli.py`

- [ ] **Step 1: Write the CLI skeleton**

```python
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
```

- [ ] **Step 2: Install package in development mode**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && uv pip install -e ".[dev]"
```
Expected: Successfully installed dev-sync

- [ ] **Step 3: Test CLI help**

Run:
```bash
dev-sync --help
```
Expected: Shows help with "config" subcommand listed

- [ ] **Step 4: Test version flag**

Run:
```bash
dev-sync --version
```
Expected: `dev-sync version 0.1.0`

- [ ] **Step 5: Test config validate (should fail gracefully)**

Run:
```bash
dev-sync config validate || echo "Exit code: $?"
```
Expected: Shows "Config validation not yet implemented" and exit code 1

- [ ] **Step 6: Commit**

```bash
git add src/dev_sync/cli.py
git commit -m "feat: add CLI skeleton with Typer"
```

---

### Task 4: Create Pydantic config models

**Files:**
- Create: `src/dev_sync/core/config.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test for config loading**

Create `tests/__init__.py`:
```python
"""Tests for dev-sync package."""
```

Create `tests/conftest.py`:
```python
"""Pytest fixtures for dev-sync tests."""

import tempfile
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def sample_config_dict() -> dict:
    """Minimal valid configuration dictionary."""
    return {
        "version": "1",
        "node_id": "test-node",
        "timezone": "UTC",
        "paths": {
            "state_db": "~/.dev-sync/state.db",
            "worktrees": "~/.dev-sync/worktrees",
            "bare_repos": "~/.dev-sync/repos",
            "contexts": "~/dev-sync/contexts",
            "skills": "~/dev-sync/skills",
        },
        "claude": {
            "binary": "claude",
            "default_timeout_seconds": 1800,
            "output_format": "json",
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {
                "inbox": "~/.dev-sync/inbox.txt",
                "outbox": "~/.dev-sync/outbox.txt",
            },
        },
        "dashboard": {
            "enabled": False,
        },
        "repos": [],
    }


@pytest.fixture
def sample_config_file(sample_config_dict: dict, tmp_path: Path) -> Path:
    """Write sample config to a temporary file."""
    config_path = tmp_path / "orchestrator.yaml"
    config_path.write_text(yaml.dump(sample_config_dict))
    return config_path
```

Create `tests/test_config.py`:
```python
"""Tests for configuration loading and validation."""

from pathlib import Path

import pytest

from dev_sync.core.config import Config, load_config, ConfigError


class TestConfigLoading:
    def test_load_valid_config(self, sample_config_file: Path) -> None:
        """Loading a valid config file should return a Config object."""
        config = load_config(sample_config_file)
        assert isinstance(config, Config)
        assert config.node_id == "test-node"
        assert config.timezone == "UTC"

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        """Loading a non-existent file should raise ConfigError."""
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nonexistent.yaml")

    def test_load_invalid_yaml_raises(self, tmp_path: Path) -> None:
        """Loading invalid YAML should raise ConfigError."""
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("{{{{invalid yaml")
        with pytest.raises(ConfigError, match="parse"):
            load_config(bad_file)

    def test_config_validates_required_fields(self, tmp_path: Path) -> None:
        """Config missing required fields should raise validation error."""
        incomplete = tmp_path / "incomplete.yaml"
        incomplete.write_text("version: '1'\n")
        with pytest.raises(ConfigError, match="validation"):
            load_config(incomplete)


class TestConfigPaths:
    def test_paths_expand_tilde(self, sample_config_file: Path) -> None:
        """Path fields should expand ~ to home directory."""
        config = load_config(sample_config_file)
        assert "~" not in str(config.paths.state_db)
        assert str(config.paths.state_db).startswith("/")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && pytest tests/test_config.py -v
```
Expected: FAIL with "No module named 'dev_sync.core.config'"

- [ ] **Step 3: Write the config module**

Create `src/dev_sync/core/config.py`:
```python
"""Configuration loading and validation for dev-sync."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ConfigError(Exception):
    """Raised when configuration loading or validation fails."""


class TransportType(str, Enum):
    TELEGRAM = "telegram"
    FILE_MOCK = "file_mock"


class AutomationPolicy(str, Enum):
    AUTO = "auto"
    ASK = "ask"
    NEVER = "never"


class PathsConfig(BaseModel):
    """File system paths configuration."""

    state_db: Path
    worktrees: Path
    bare_repos: Path
    contexts: Path
    skills: Path

    @field_validator("*", mode="before")
    @classmethod
    def expand_path(cls, v: Any) -> Any:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class ClaudeConfig(BaseModel):
    """Claude CLI configuration."""

    binary: str = "claude"
    default_timeout_seconds: int = 1800
    output_format: str = "json"


class TelegramConfig(BaseModel):
    """Telegram transport configuration."""

    bot_token_env: str = "DEV_SYNC_TELEGRAM_TOKEN"
    chat_id: int = 0
    socket_path: Path = Path("~/.dev-sync/dev-sync.sock")

    @field_validator("socket_path", mode="before")
    @classmethod
    def expand_socket_path(cls, v: Any) -> Any:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class FileMockConfig(BaseModel):
    """File mock transport configuration for testing."""

    inbox: Path
    outbox: Path

    @field_validator("*", mode="before")
    @classmethod
    def expand_path(cls, v: Any) -> Any:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class TransportConfig(BaseModel):
    """Transport configuration (Telegram or file mock)."""

    type: TransportType = TransportType.FILE_MOCK
    telegram: TelegramConfig | None = None
    file_mock: FileMockConfig | None = None

    @model_validator(mode="after")
    def validate_transport_config(self) -> "TransportConfig":
        if self.type == TransportType.TELEGRAM and self.telegram is None:
            raise ValueError("telegram config required when type is 'telegram'")
        if self.type == TransportType.FILE_MOCK and self.file_mock is None:
            raise ValueError("file_mock config required when type is 'file_mock'")
        return self


class DashboardConfig(BaseModel):
    """Dashboard client configuration."""

    enabled: bool = True
    url: str = ""
    auth_token_env: str = "DEV_SYNC_DASHBOARD_TOKEN"
    sync_config_on_heartbeat: bool = False


class DeployConfig(BaseModel):
    """Deployment configuration for a repo."""

    provider: str = "digitalocean"
    app_id: str = ""


class CodeReviewConfig(BaseModel):
    """Code review configuration for a repo."""

    method: str = "mcp_then_cli"
    mcp_tool: str = "mcp__codex-reviewer__codex_review"
    cli_command: str = "codex review"


class AutomationConfig(BaseModel):
    """Automation policies for a repo."""

    dependabot_patch: AutomationPolicy = AutomationPolicy.AUTO
    dependabot_minor: AutomationPolicy = AutomationPolicy.ASK
    dependabot_major: AutomationPolicy = AutomationPolicy.NEVER
    codeql_dismiss: AutomationPolicy = AutomationPolicy.ASK
    secret_alerts: AutomationPolicy = AutomationPolicy.NEVER
    deploy_after_merge: AutomationPolicy = AutomationPolicy.AUTO


class RepoConfig(BaseModel):
    """Configuration for a single repository."""

    name: str
    local_path: Path
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    deploy: DeployConfig | None = None
    code_review: CodeReviewConfig = Field(default_factory=CodeReviewConfig)
    dev_branch_template: str = "fix/issue-{n}"

    @field_validator("local_path", mode="before")
    @classmethod
    def expand_local_path(cls, v: Any) -> Any:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class Config(BaseModel):
    """Root configuration model for dev-sync orchestrator."""

    version: str = "1"
    node_id: str
    timezone: str = "UTC"
    paths: PathsConfig
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    transport: TransportConfig
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    repos: list[RepoConfig] = Field(default_factory=list)


def load_config(path: Path | str) -> Config:
    """Load and validate configuration from a YAML file.

    Args:
        path: Path to the orchestrator.yaml file.

    Returns:
        Validated Config object.

    Raises:
        ConfigError: If the file cannot be loaded or validation fails.
    """
    path = Path(path)

    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse YAML: {e}") from e

    if data is None:
        raise ConfigError("Config file is empty")

    try:
        return Config.model_validate(data)
    except Exception as e:
        raise ConfigError(f"Config validation failed: {e}") from e
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && pytest tests/test_config.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Update core __init__.py to export config**

Edit `src/dev_sync/core/__init__.py`:
```python
"""Core functionality for dev-sync orchestrator."""

from dev_sync.core.config import (
    Config,
    ConfigError,
    RepoConfig,
    load_config,
)

__all__ = ["Config", "ConfigError", "RepoConfig", "load_config"]
```

- [ ] **Step 6: Commit**

```bash
git add src/dev_sync/core/config.py src/dev_sync/core/__init__.py tests/
git commit -m "feat: add Pydantic config models with validation"
```

---

### Task 5: Wire config validate CLI command

**Files:**
- Modify: `src/dev_sync/cli.py`
- Create: `config/orchestrator.yaml.example`

- [ ] **Step 1: Create example config file**

Create `config/orchestrator.yaml.example`:
```yaml
# dev-sync orchestrator configuration
# Copy to orchestrator.yaml and customize

version: "1"
node_id: "my-machine"
timezone: "America/Santiago"

paths:
  state_db: "~/.dev-sync/state.db"
  worktrees: "~/.dev-sync/worktrees"
  bare_repos: "~/.dev-sync/repos"
  contexts: "~/dev-sync/contexts"
  skills: "~/dev-sync/claude-config/skills"

claude:
  binary: "claude"
  default_timeout_seconds: 1800
  output_format: "json"

transport:
  type: "file_mock"  # Use "telegram" for production
  telegram:
    bot_token_env: "DEV_SYNC_TELEGRAM_TOKEN"
    chat_id: 123456789
    socket_path: "~/.dev-sync/dev-sync.sock"
  file_mock:
    inbox: "~/.dev-sync/inbox.txt"
    outbox: "~/.dev-sync/outbox.txt"

dashboard:
  enabled: false
  url: "https://dev-sync-dashboard.example.com"
  auth_token_env: "DEV_SYNC_DASHBOARD_TOKEN"

repos: []
  # Example repo configuration:
  # - name: "owner/repo"
  #   local_path: "~/Projects/repo"
  #   automation:
  #     dependabot_patch: auto
  #     dependabot_minor: ask
  #     dependabot_major: never
  #     codeql_dismiss: ask
  #     secret_alerts: never
  #     deploy_after_merge: auto
  #   deploy:
  #     provider: "digitalocean"
  #     app_id: "abc-123"
  #   dev_branch_template: "fix/issue-{n}"
```

- [ ] **Step 2: Update CLI with working config validate**

Edit `src/dev_sync/cli.py` - replace the entire file:
```python
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


if __name__ == "__main__":
    app()
```

- [ ] **Step 3: Copy example to usable config**

Run:
```bash
cp config/orchestrator.yaml.example config/orchestrator.yaml
```

- [ ] **Step 4: Test config validate with valid config**

Run:
```bash
dev-sync config validate -c config/orchestrator.yaml
```
Expected: Shows "✓ Config valid" with node_id, timezone, etc.

- [ ] **Step 5: Test config validate with invalid path**

Run:
```bash
dev-sync config validate -c nonexistent.yaml || echo "Exit code: $?"
```
Expected: Error message and exit code 1

- [ ] **Step 6: Commit**

```bash
git add src/dev_sync/cli.py config/
git commit -m "feat: wire config validate CLI command"
```

---

### Task 6: Create SQLite state module

**Files:**
- Create: `src/dev_sync/core/state.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Write the failing test for state module**

Create `tests/test_state.py`:
```python
"""Tests for SQLite state management."""

from pathlib import Path

import pytest

from dev_sync.core.state import StateDB


class TestStateDBInit:
    def test_creates_database_file(self, tmp_path: Path) -> None:
        """StateDB should create the database file if it doesn't exist."""
        db_path = tmp_path / "state.db"
        assert not db_path.exists()

        db = StateDB(db_path)
        db.close()

        assert db_path.exists()

    def test_creates_tables(self, tmp_path: Path) -> None:
        """StateDB should create all required tables on init."""
        db_path = tmp_path / "state.db"
        db = StateDB(db_path)

        # Check tables exist
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {row[0] for row in tables}

        expected = {"sessions", "repo_locks", "github_cursor", "telegram_pending", "automation_decisions"}
        assert expected.issubset(table_names)

        db.close()


class TestRepoLocks:
    def test_acquire_lock_succeeds_when_free(self, tmp_path: Path) -> None:
        """Should acquire lock when repo is not locked."""
        db = StateDB(tmp_path / "state.db")
        result = db.acquire_lock("owner/repo", "session-123")
        assert result is True
        db.close()

    def test_acquire_lock_fails_when_held(self, tmp_path: Path) -> None:
        """Should fail to acquire lock when already held."""
        db = StateDB(tmp_path / "state.db")
        db.acquire_lock("owner/repo", "session-123")
        result = db.acquire_lock("owner/repo", "session-456")
        assert result is False
        db.close()

    def test_release_lock(self, tmp_path: Path) -> None:
        """Should release lock so it can be re-acquired."""
        db = StateDB(tmp_path / "state.db")
        db.acquire_lock("owner/repo", "session-123")
        db.release_lock("owner/repo")
        result = db.acquire_lock("owner/repo", "session-456")
        assert result is True
        db.close()

    def test_get_lock_holder(self, tmp_path: Path) -> None:
        """Should return the session holding the lock."""
        db = StateDB(tmp_path / "state.db")
        db.acquire_lock("owner/repo", "session-123")
        holder = db.get_lock_holder("owner/repo")
        assert holder == "session-123"
        db.close()

    def test_get_lock_holder_when_free(self, tmp_path: Path) -> None:
        """Should return None when repo is not locked."""
        db = StateDB(tmp_path / "state.db")
        holder = db.get_lock_holder("owner/repo")
        assert holder is None
        db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && pytest tests/test_state.py -v
```
Expected: FAIL with "No module named 'dev_sync.core.state'"

- [ ] **Step 3: Write the state module**

Create `src/dev_sync/core/state.py`:
```python
"""SQLite state management for dev-sync orchestrator."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    pipeline TEXT NOT NULL,
    repo TEXT NOT NULL,
    issue_number INTEGER,
    worktree_path TEXT,
    status TEXT NOT NULL,
    blocked_question TEXT,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    claude_exit_code INTEGER,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS repo_locks (
    repo TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    acquired_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS github_cursor (
    repo TEXT PRIMARY KEY,
    last_checked_at INTEGER NOT NULL,
    last_seen_issue_update TEXT
);

CREATE TABLE IF NOT EXISTS telegram_pending (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    question TEXT NOT NULL,
    asked_at INTEGER NOT NULL,
    answered_at INTEGER,
    answer TEXT
);

CREATE TABLE IF NOT EXISTS automation_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    operation TEXT NOT NULL,
    policy TEXT NOT NULL,
    item_id TEXT,
    decision TEXT,
    decided_by TEXT,
    decided_at INTEGER,
    context TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repo);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_automation_repo ON automation_decisions(repo);
"""


class StateDB:
    """SQLite database for orchestrator state.

    Thread-safety: Each thread/async context should create its own StateDB instance.
    The underlying SQLite connection is not shared.
    """

    def __init__(self, db_path: Path | str) -> None:
        """Initialize the database, creating tables if needed.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Execute a SQL statement.

        Args:
            sql: SQL statement to execute.
            params: Parameters for the statement.

        Returns:
            Cursor with results.
        """
        return self._conn.execute(sql, params)

    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()

    # Repo locks

    def acquire_lock(self, repo: str, session_id: str) -> bool:
        """Attempt to acquire a lock on a repository.

        Args:
            repo: Repository name (e.g., "owner/repo").
            session_id: Session ID acquiring the lock.

        Returns:
            True if lock was acquired, False if already held.
        """
        try:
            self._conn.execute(
                "INSERT INTO repo_locks (repo, session_id, acquired_at) VALUES (?, ?, ?)",
                (repo, session_id, int(time.time())),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def release_lock(self, repo: str) -> None:
        """Release a lock on a repository.

        Args:
            repo: Repository name to unlock.
        """
        self._conn.execute("DELETE FROM repo_locks WHERE repo = ?", (repo,))
        self._conn.commit()

    def get_lock_holder(self, repo: str) -> str | None:
        """Get the session ID holding a lock.

        Args:
            repo: Repository name.

        Returns:
            Session ID if locked, None otherwise.
        """
        row = self._conn.execute(
            "SELECT session_id FROM repo_locks WHERE repo = ?", (repo,)
        ).fetchone()
        return row["session_id"] if row else None

    def list_locks(self) -> list[dict[str, Any]]:
        """List all current locks.

        Returns:
            List of lock records.
        """
        rows = self._conn.execute("SELECT * FROM repo_locks").fetchall()
        return [dict(row) for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && pytest tests/test_state.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Update core __init__.py to export state**

Edit `src/dev_sync/core/__init__.py`:
```python
"""Core functionality for dev-sync orchestrator."""

from dev_sync.core.config import (
    Config,
    ConfigError,
    RepoConfig,
    load_config,
)
from dev_sync.core.state import StateDB

__all__ = ["Config", "ConfigError", "RepoConfig", "load_config", "StateDB"]
```

- [ ] **Step 6: Commit**

```bash
git add src/dev_sync/core/state.py src/dev_sync/core/__init__.py tests/test_state.py
git commit -m "feat: add SQLite state module with repo locks"
```

---

### Task 7: Add status command showing state

**Files:**
- Modify: `src/dev_sync/cli.py`

- [ ] **Step 1: Add status command to CLI**

Add to `src/dev_sync/cli.py` after the config_app commands:

```python
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

    db = StateDB(db_path)

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

    db.close()
```

- [ ] **Step 2: Test status command (no DB yet)**

Run:
```bash
dev-sync status -c config/orchestrator.yaml
```
Expected: Shows "State database not found"

- [ ] **Step 3: Commit**

```bash
git add src/dev_sync/cli.py
git commit -m "feat: add status command showing state"
```

---

### Task 8: Run full test suite and verify phase gate

**Files:** None (verification only)

- [ ] **Step 1: Run all tests**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && pytest tests/ -v --cov=dev_sync
```
Expected: All tests pass, coverage report shown

- [ ] **Step 2: Verify pip install works**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && uv pip install -e . && dev-sync --version
```
Expected: `dev-sync version 0.1.0`

- [ ] **Step 3: Verify config validate works**

Run:
```bash
dev-sync config validate -c config/orchestrator.yaml
```
Expected: Shows "✓ Config valid"

- [ ] **Step 4: Run ruff linter**

Run:
```bash
cd /Users/ovalenzuela/Projects/dev-sync && ruff check src/ tests/
```
Expected: No errors

- [ ] **Step 5: Commit final state**

```bash
git add -A
git status
# If any uncommitted changes:
git commit -m "chore: phase 0 complete - package skeleton ready"
```

---

## Phase Gate Verification

**Phase 0 is complete when:**

1. ✅ `pip install -e .` succeeds
2. ✅ `dev-sync --version` shows version
3. ✅ `dev-sync config validate -c config/orchestrator.yaml` passes
4. ✅ All tests pass
5. ✅ No linter errors

**Next:** Phase 1 - Checkpoint Protocol + Skill Audit Tool
