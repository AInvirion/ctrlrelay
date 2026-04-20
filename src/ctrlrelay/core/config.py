"""Configuration loading and validation for ctrlrelay."""

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

    bot_token_env: str = "CTRLRELAY_TELEGRAM_TOKEN"
    chat_id: int = 0
    socket_path: Path = Field(
        default_factory=lambda: Path("~/.ctrlrelay/ctrlrelay.sock").expanduser()
    )

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
    auth_token_env: str = "CTRLRELAY_DASHBOARD_TOKEN"
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


class SchedulesConfig(BaseModel):
    """Cron schedules for background jobs run by the poller daemon.

    Values are standard 5-field cron expressions (minute hour dom month dow),
    evaluated in the top-level ``timezone``. Each schedule is validated at
    config load time so an unparseable expression fails fast rather than
    silently disabling the job.
    """

    secops_cron: str = "0 6 * * *"

    @field_validator("secops_cron")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        from apscheduler.triggers.cron import CronTrigger

        try:
            CronTrigger.from_crontab(v)
        except Exception as e:
            raise ValueError(
                f"invalid cron expression {v!r}: {e}"
            ) from e
        return v


class Config(BaseModel):
    """Root configuration model for ctrlrelay orchestrator."""

    version: str = "1"
    node_id: str
    timezone: str = "UTC"
    paths: PathsConfig
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    transport: TransportConfig
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    schedules: SchedulesConfig = Field(default_factory=SchedulesConfig)
    repos: list[RepoConfig] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        """Reject unparseable IANA zones at load time.

        Since the scheduler feeds ``timezone`` directly into APScheduler's
        CronTrigger, a typo like ``America/Santiagoo`` would only surface
        as a ``ZoneInfoNotFoundError`` when the poller daemon starts —
        much worse than a synchronous config error at load.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown timezone {v!r}: {e}") from e
        return v


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
    except OSError as e:
        raise ConfigError(f"Failed to read config file: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse YAML: {e}") from e

    if data is None:
        raise ConfigError("Config file is empty")

    try:
        return Config.model_validate(data)
    except Exception as e:
        raise ConfigError(f"Config validation failed: {e}") from e
