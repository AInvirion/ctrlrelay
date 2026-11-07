"""Core functionality for ctrlrelay orchestrator."""

from ctrlrelay.core.agent_plugins import (
    AgentCapabilities,
    AgentIdentity,
    AgentSelection,
    AgentSelectionError,
)
from ctrlrelay.core.checkpoint import (
    CheckpointError,
    CheckpointState,
    CheckpointStatus,
    blocked,
    done,
    failed,
    read_checkpoint,
)
from ctrlrelay.core.config import (
    Config,
    ConfigError,
    RepoConfig,
    load_config,
)
from ctrlrelay.core.dispatcher import (
    AgentAdapter,
    ClaudeDispatcher,
    CliAgentAdapter,
    OpenCodeDispatcher,
    RepoAgentRouter,
    SessionResult,
    make_agent_dispatcher,
    make_repo_agent_dispatcher,
    select_agent,
)
from ctrlrelay.core.generic_cli import GenericCliDispatcher
from ctrlrelay.core.github import GitHubCLI
from ctrlrelay.core.poller import IssuePoller
from ctrlrelay.core.pr_verifier import PRVerifier, VerificationResult
from ctrlrelay.core.pr_watcher import PRWatcher
from ctrlrelay.core.state import StateDB
from ctrlrelay.core.worktree import WorktreeManager

__all__ = [
    "CheckpointError",
    "CheckpointState",
    "CheckpointStatus",
    "AgentAdapter",
    "AgentCapabilities",
    "AgentIdentity",
    "AgentSelection",
    "AgentSelectionError",
    "ClaudeDispatcher",
    "CliAgentAdapter",
    "GenericCliDispatcher",
    "OpenCodeDispatcher",
    "RepoAgentRouter",
    "make_agent_dispatcher",
    "make_repo_agent_dispatcher",
    "select_agent",
    "Config",
    "ConfigError",
    "GitHubCLI",
    "IssuePoller",
    "PRVerifier",
    "PRWatcher",
    "RepoConfig",
    "SessionResult",
    "StateDB",
    "VerificationResult",
    "WorktreeManager",
    "blocked",
    "done",
    "failed",
    "load_config",
    "read_checkpoint",
]
