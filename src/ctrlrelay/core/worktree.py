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

    async def _run_git(
        self,
        *args: str,
        cwd: Path | None = None,
        timeout: int | None = None,
    ) -> str:
        """Run git command and return stdout. `timeout` overrides self.timeout
        for one call — useful for cheap probes that shouldn't inherit the full
        120s default when network is flaky."""
        cmd = ["git", *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout if timeout is not None else self.timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise

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

    async def get_default_branch(self, repo: str) -> str:
        """Get the default branch for a repo from bare clone."""
        bare_path = self._get_bare_repo_path(repo)
        output = await self._run_git("symbolic-ref", "HEAD", cwd=bare_path)
        return output.strip().replace("refs/heads/", "")

    async def create_worktree(
        self,
        repo: str,
        session_id: str,
        branch: str | None = None,
    ) -> Path:
        """Create a worktree for a session."""
        bare_path = self._get_bare_repo_path(repo)
        worktree_path = self._get_worktree_path(repo, session_id)

        if worktree_path.exists():
            raise WorktreeError(f"Worktree already exists: {worktree_path}")

        if branch is None:
            branch = await self.get_default_branch(repo)

        await self._run_git(
            "worktree", "add",
            str(worktree_path),
            branch,
            cwd=bare_path,
        )

        return worktree_path

    async def create_worktree_with_new_branch(
        self,
        repo: str,
        session_id: str,
        new_branch: str,
        base_branch: str | None = None,
    ) -> Path:
        """Create a worktree with a new branch."""
        bare_path = self._get_bare_repo_path(repo)
        worktree_path = self._get_worktree_path(repo, session_id)

        if worktree_path.exists():
            raise WorktreeError(f"Worktree already exists: {worktree_path}")

        if base_branch is None:
            base_branch = await self.get_default_branch(repo)

        await self._run_git(
            "worktree", "add",
            "-b", new_branch,
            str(worktree_path),
            base_branch,
            cwd=bare_path,
        )
        return worktree_path

    async def push_branch(self, worktree_path: Path, branch: str) -> None:
        """Push a branch to origin."""
        await self._run_git("push", "-u", "origin", branch, cwd=worktree_path)

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

        if bare_path.exists():
            await self._run_git("worktree", "prune", cwd=bare_path)

    async def delete_branch(self, repo: str, branch: str) -> None:
        """Delete a local branch in the bare repo. Best-effort; no-op if absent."""
        bare_path = self._get_bare_repo_path(repo)
        if not bare_path.exists():
            return
        try:
            await self._run_git("branch", "-D", branch, cwd=bare_path)
        except WorktreeError:
            pass

    async def branch_exists_locally(self, repo: str, branch: str) -> bool:
        """Check if `branch` exists as a local ref in the bare repo."""
        bare_path = self._get_bare_repo_path(repo)
        if not bare_path.exists():
            return False
        try:
            await self._run_git(
                "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
                cwd=bare_path,
            )
            return True
        except Exception:
            return False

    async def branch_exists_on_remote(self, repo: str, branch: str) -> bool:
        """Return True if `branch` exists on origin. Fail-closed: on any error
        (WorktreeError, asyncio.TimeoutError from _run_git's wait_for, etc.)
        returns True so callers err on the side of NOT deleting."""
        bare_path = self._get_bare_repo_path(repo)
        if not bare_path.exists():
            return True
        try:
            output = await self._run_git(
                "ls-remote", "--heads", "origin", branch,
                cwd=bare_path,
                timeout=10,
            )
            return bool(output.strip())
        except Exception:
            return True

    def _get_gitdir(self, worktree_path: Path) -> Path:
        """Get the real gitdir for a worktree.

        In linked worktrees, .git is a file pointing to the actual gitdir.
        """
        dot_git = worktree_path / ".git"
        if dot_git.is_file():
            content = dot_git.read_text().strip()
            if content.startswith("gitdir: "):
                return Path(content[8:])
        return dot_git

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

        gitdir = self._get_gitdir(worktree_path)
        exclude_file = gitdir / "info" / "exclude"
        if exclude_file.exists():
            content = exclude_file.read_text()
            if "CLAUDE.md" not in content:
                exclude_file.write_text(content.rstrip() + "\nCLAUDE.md\n")

    def remove_context_symlink(self, worktree_path: Path) -> None:
        """Remove CLAUDE.md symlink before git operations."""
        target = worktree_path / "CLAUDE.md"
        if target.is_symlink():
            target.unlink()
