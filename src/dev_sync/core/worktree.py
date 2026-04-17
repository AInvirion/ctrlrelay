"""Git worktree management for isolated sessions."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(Exception):
    """Raised when worktree operations fail."""


@dataclass
class WorktreeManager:
    """Manages git worktrees for session isolation."""

    worktrees_dir: Path
    bare_repos_dir: Path
    timeout: int = 120

    def __post_init__(self) -> None:
        self.worktrees_dir = Path(self.worktrees_dir)
        self.bare_repos_dir = Path(self.bare_repos_dir)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.bare_repos_dir.mkdir(parents=True, exist_ok=True)

    async def _run_git(self, *args: str, cwd: Path | None = None) -> str:
        """Run git command and return stdout."""
        cmd = ["git", *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self.timeout
        )

        if proc.returncode != 0:
            raise WorktreeError(f"git failed: {stderr.decode().strip()}")

        return stdout.decode()

    def _get_bare_repo_path(self, repo: str) -> Path:
        """Get path to bare repo clone."""
        repo_name = repo.replace("/", "-")
        return self.bare_repos_dir / f"{repo_name}.git"

    def _get_worktree_path(self, repo: str, session_id: str) -> Path:
        """Get path for a worktree."""
        repo_name = repo.replace("/", "-")
        return self.worktrees_dir / f"{repo_name}-{session_id}"

    async def create_worktree(
        self,
        repo: str,
        session_id: str,
        branch: str = "main",
    ) -> Path:
        """Create a worktree for a session."""
        bare_path = self._get_bare_repo_path(repo)
        worktree_path = self._get_worktree_path(repo, session_id)

        if worktree_path.exists():
            raise WorktreeError(f"Worktree already exists: {worktree_path}")

        await self._run_git(
            "worktree", "add",
            str(worktree_path),
            branch,
            cwd=bare_path,
        )

        return worktree_path

    async def ensure_bare_repo(self, repo: str) -> Path:
        """Ensure bare repo exists, cloning if needed."""
        bare_path = self._get_bare_repo_path(repo)

        if bare_path.exists():
            await self._run_git("fetch", "--all", cwd=bare_path)
        else:
            await self._run_git(
                "clone", "--bare",
                f"https://github.com/{repo}.git",
                str(bare_path),
            )

        return bare_path

    async def remove_worktree(self, repo: str, session_id: str) -> None:
        """Remove a worktree and clean up."""
        bare_path = self._get_bare_repo_path(repo)
        worktree_path = self._get_worktree_path(repo, session_id)

        if worktree_path.exists():
            shutil.rmtree(worktree_path)

        await self._run_git("worktree", "prune", cwd=bare_path)

    def symlink_context(
        self,
        worktree_path: Path,
        context_path: Path,
    ) -> None:
        """Symlink CLAUDE.md into worktree."""
        target = worktree_path / "CLAUDE.md"

        if target.exists() or target.is_symlink():
            target.unlink()

        target.symlink_to(context_path.resolve())

        exclude_file = worktree_path / ".git" / "info" / "exclude"
        if exclude_file.exists():
            content = exclude_file.read_text()
            if "CLAUDE.md" not in content:
                exclude_file.write_text(content.rstrip() + "\nCLAUDE.md\n")

    def remove_context_symlink(self, worktree_path: Path) -> None:
        """Remove CLAUDE.md symlink before git operations."""
        target = worktree_path / "CLAUDE.md"
        if target.is_symlink():
            target.unlink()
