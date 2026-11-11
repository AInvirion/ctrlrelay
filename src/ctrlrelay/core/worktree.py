"""Git worktree management for isolated sessions."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import shlex
import shutil
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ctrlrelay.core.obs import get_logger, log_event

if TYPE_CHECKING:
    from ctrlrelay.core.github import GitHubCLI

_logger = get_logger("core.worktree")


# Open-PR probe (issue #52) retries a handful of times before failing
# closed. Transient gh/network errors are common enough that one-shot
# failure would be noisy; but infinite retry would block retries when
# gh is genuinely down. Three quick attempts strike a balance.
_PR_PROBE_RETRY_ATTEMPTS = 3
_PR_PROBE_RETRY_SLEEP_SECONDS = 1.0
# Per-call gh timeout for the open-PR probe. This probe holds the repo
# lock, so inheriting GitHubCLI's 60s default would let a hung gh
# process block other sessions on the same repo for up to ~3 minutes
# (3 attempts × 60s) before failing closed. 10s is plenty for a
# single pr-list call with --head filter against GitHub.
_PR_PROBE_TIMEOUT_SECONDS = 10

# Network-bound git transfers (bare clone, fetch) get their own, much
# larger budget than the 120s that suits local plumbing. A 1.8 GB repo
# cannot clone or fetch its first time inside two minutes on any normal
# link, and the failure mode was silent: `asyncio.wait_for` raises a
# bare `TimeoutError` whose `str()` is empty, so the sweep reported
# "Error processing <repo>" with no cause attached and wrote neither a
# session row nor a log line. 1800s matches claude's own
# `default_timeout_seconds` — a transfer still running past that is
# genuinely stuck, not merely large.
_GIT_TRANSFER_TIMEOUT_SECONDS = 1800

# Branches ctrlrelay never authors, so rewinding one can never destroy
# local work. Dependabot rebases its branches onto a moved base, which
# rewrites their tips — under a non-force refspec every such rebase is
# a non-fast-forward rejection.
_BOT_BRANCH_REFSPEC = "+refs/heads/dependabot/*:refs/heads/dependabot/*"

# ...and the same pattern as a negative refspec, so the fetch that
# carries every other branch leaves them alone. Keeping the two fetches
# disjoint is what lets only one of them prune (#243), and it also
# removes the ordering dependency that made a rebased bot branch a
# tolerated rejection under the plain refspec.
_NOT_BOT_BRANCHES_REFSPEC = "^refs/heads/dependabot/*"


def _rejections_are_only_non_fast_forward(stderr: str) -> list[str]:
    """Return the refs a fetch declined to rewind, or ``[]`` if the
    failure was anything else.

    A non-fast-forward rejection is the non-force refspec doing its job:
    it refused to discard local commits. Git still exits 1, which the
    caller must not read as "the fetch failed" — the rest of the fetch
    (the default branch, prunes, new branches) applied normally.

    Anything git considers a real error — ``fatal:`` on a dead remote,
    ``error:`` on a bad object, or a rejection for some other reason —
    returns ``[]`` so the caller raises as before. Failing open here
    would turn an unreachable remote into a silent no-op and let
    pipelines run against a stale tree, which is the bug this module's
    explicit refspec was added to prevent in the first place.
    """
    refs: list[str] = []
    for raw in stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(("fatal:", "error:")):
            return []
        if not line.startswith("!"):
            continue  # progress line: "From ...", " * [new branch]", " abc..def"
        if "non-fast-forward" not in low and "non-fast forward" not in low:
            return []  # rejected for some other reason — not ours to swallow
        # " ! [rejected]  <src> -> <dst>  (non-fast-forward)"
        body = line.lstrip("! ")
        if "]" in body:
            body = body.split("]", 1)[1]
        target = body.split("->")[-1].split("(")[0].strip()
        refs.append(target or body.strip())
    return refs


# How much of each stream a failing git command contributes to the
# error it raises. #269: `git push --porcelain` writes its per-ref
# status to STDOUT, so for a rejected push the remote's reason is on
# stdout and stderr holds only `error: failed to push some refs to
# '<url>'`. Dropping stdout left every failed push reported by its
# least informative line — and that line names the URL, which sends
# the reader after the transport or the credential when neither was
# at fault.
_GIT_ERROR_TAIL_CHARS = 1000
# The reason is one porcelain line for the one ref we push. The cap is
# for a remote that answers with an essay.
_PUSH_REASON_MAX_CHARS = 500


def _git_failure_detail(
    stdout: str, stderr: str, returncode: int | None
) -> str:
    """Both streams of a failed git command, scrubbed and capped.

    stderr first and whole: it carries git's own ``fatal:``/``error:``
    lines and is what every existing caller's matcher reads. stdout
    follows, labelled and capped, whenever it said anything — which for a
    ``--porcelain`` command is where the answer is.

    Redaction runs before the cap (see :mod:`ctrlrelay.core.redact`):
    clipping first can store half a token, and half a token no longer
    matches the pattern that would have caught it.

    When both streams are empty the exit code is all there is, so it is
    named rather than rendered as a message with nothing after the
    colon.
    """
    from ctrlrelay.core.redact import redact_secrets, tail

    parts: list[str] = []
    # stderr whole, as the message carried it before stdout was added, so
    # a matcher looking for git's words anywhere in it still finds them;
    # stdout capped.
    err = stderr.strip()
    if err:
        parts.append(redact_secrets(err))
    out = stdout.strip()
    if out:
        parts.append("stdout: " + tail(redact_secrets(out), _GIT_ERROR_TAIL_CHARS))
    if not parts:
        return f"exited {returncode} with no output"
    return "; ".join(parts)


def parse_push_rejection(porcelain_stdout: str) -> str | None:
    """The reason ``git push --porcelain`` gave for refusing a ref.

    Porcelain status is one TAB-separated line per ref on **stdout**:
    ``<flag>\\t<from>:<to>\\t<summary>``. A refused ref carries flag
    ``!`` and the remote's reason in the summary — ``[rejected] (stale
    info)``, ``[remote rejected] (pre-receive hook declined)``,
    ``[remote rejected] (push declined due to repository rule
    violations)``. Lines for refs that updated carry a different flag
    and are not failures.

    Returns ``None`` when no ref was rejected: the push died before it
    got a per-ref answer (unreachable remote, bad credential, no such
    repository), and then stderr is the whole story and must not be
    dressed up as a rejection.

    Also reads the non-porcelain form (``! [rejected] <src> -> <dst>
    (reason)``), because the flag and the reason are in it too and a
    caller should not have to know which form it has.
    """
    from ctrlrelay.core.redact import redact_secrets

    reasons: list[str] = []
    for raw in porcelain_stdout.splitlines():
        line = raw.strip()
        if not line.startswith("!"):
            continue
        if "\t" in raw:
            # At most two splits: the summary keeps any tab inside it.
            fields = [f.strip() for f in raw.split("\t", 2)]
            ref = fields[1] if len(fields) > 1 else ""
            summary = fields[2] if len(fields) > 2 else ""
            # Scrubbed before anything is cut (what the remote says can
            # carry a credential, and half a token no longer matches the
            # pattern); then the ref whole and the remote's words capped,
            # so a long ref cannot crowd out the reason.
            ref, summary = redact_secrets(ref), redact_secrets(summary)
            text = f"{ref} {summary[:_PUSH_REASON_MAX_CHARS]}".strip() or line
        else:
            text = redact_secrets(line.lstrip("! ").strip() or line)
        reasons.append(text)
    if not reasons:
        return None
    return "; ".join(reasons)


class WorktreeError(Exception):
    """Raised when worktree operations fail."""


class GitCommandError(WorktreeError):
    """A git command exited non-zero, carrying both of its streams.

    A ``WorktreeError`` like before, so every existing handler is
    unaffected. The two attributes are for a caller that knows the
    command's output format and can read more from it than the message
    does — ``push --porcelain``, whose per-ref reason is on stdout.
    """

    def __init__(
        self, message: str, *, stdout: str = "", stderr: str = ""
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


class PartialSaveError(WorktreeError):
    """The wip branch was pushed, but some files could not be read and
    are not in it (#311). ``sha`` is what was pushed; the caller keeps
    the worktree, which still holds them."""

    def __init__(self, message: str, *, sha: str) -> None:
        super().__init__(message)
        self.sha = sha


class PushRejectedError(WorktreeError):
    """Git rejected the push for a ref, named with the reason it gave.

    Decided by git itself before the remote is asked (a stale lease, a
    non-fast-forward) or by the remote (a protection rule). Distinct from
    :class:`ctrlrelay.core.publish.PushRefusedError`, which is the relay
    declining to push at all. ``reason`` is the ``!`` line from
    ``push --porcelain`` so a caller can log it as its own field
    instead of parsing it back out of the message.
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class DefaultBranchDivergedError(WorktreeError):
    """The bare clone's default branch cannot fast-forward to origin's.

    Usually the repository on GitHub was replaced or its default branch
    force-pushed. ``ensure_bare_repo`` answers it by setting the clone
    aside and cloning again (#234); it reaches a caller only when that
    is refused or fails.
    """


class StaleRecreatePartialFailureError(WorktreeError):
    """Raised when `create_worktree_with_new_branch` had committed to the
    stale-merged delete+recreate path (destroying the old local ref)
    and then the subsequent `git worktree add -b` failed partway. The
    exception signals to the caller that any leftover ref on disk was
    introduced by THIS session, so run_dev_issue's FAILED-path cleanup
    must delete it regardless of the pre-call branch_existed_before
    snapshot — the old ref is gone and anything present now is ours.

    Scoped to the exception path so concurrent sessions can't clobber
    each other's ownership state (instance-level flags on the shared
    WorktreeManager were racy under concurrent run_dev_issue calls)."""


# Command-line config for every git call the relay makes with its own
# credentials (#219). Command-line `-c` outranks every config file, so
# these hold even if a file somewhere sets the opposite. (An empty
# `include.path` cannot be passed here - git refuses relative includes
# from the command line - and is not needed: the only files read are
# the two this module writes.)
_RELAY_GIT_FLAGS: tuple[str, ...] = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "protocol.file.allow=never",
    "-c", "protocol.ext.allow=never",
)


_HEX_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")


def _is_safe_branch(branch: str) -> bool:
    return bool(
        _SAFE_BRANCH.match(branch)
        and ".." not in branch
        and "//" not in branch
        and not any(part.startswith(".") or part.endswith(".lock")
                    for part in branch.split("/"))
    )


def _safe_repo_file(repo_real: str, rel: str) -> str | None:
    """Path of ``rel`` inside ``repo_real`` if it is safe to read.

    Returns None when nothing exists there. Raises
    :class:`PushRefusedError` when something exists but is unsafe: any
    symlink between the repository and the file (the file itself or a
    parent directory), or a path that resolves outside the repository.
    Present-but-unsafe is never reported as absent, so the caller cannot
    fall through to an older value somewhere else.
    """
    from ctrlrelay.core.publish import PushRefusedError

    path = os.path.join(repo_real, rel)
    cur = repo_real
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise PushRefusedError(
                f"push refused: {rel} in the clone goes through a symlink"
            )
        if not os.path.lexists(cur):
            return None
    if (
        os.path.realpath(path) != os.path.abspath(path)
        or not os.path.realpath(path).startswith(repo_real + os.sep)
        or not os.path.isfile(path)
    ):
        raise PushRefusedError(
            f"push refused: {rel} in the clone is not a plain file inside it"
        )
    return path


def _read_nofollow(path: str) -> str:
    # Non-blocking and a plain file only: the callers check for a plain
    # file first, but a FIFO swapped in after that check would otherwise
    # block the read for ever.
    flags = (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    with os.fdopen(fd, "r") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise OSError(f"{path} is not a plain file")
        return f.read()


def _read_branch_head(bare_path: Path, branch: str) -> str:
    """Head of ``refs/heads/<branch>`` read from the ref files as data.

    No git process runs in the bare clone, so none of its config (which
    the agent can write) is read. The agent can also write the ref files
    themselves, so they are read only when no symlink is involved and
    the file is inside the clone: a loose ref first, then
    ``packed-refs``. A loose ref that exists but is unsafe is refused,
    never treated as absent. A missing branch raises
    :class:`WorktreeError`; anything unsafe raises
    :class:`PushRefusedError`.
    """
    from ctrlrelay.core.publish import PushRefusedError

    if not _is_safe_branch(branch):
        raise PushRefusedError(f"push refused: {branch!r} is not a safe ref name")
    repo_real = os.path.realpath(bare_path)
    ref = f"refs/heads/{branch}"
    value: str | None = None
    loose = _safe_repo_file(repo_real, ref)
    if loose is not None:
        value = _read_nofollow(loose).strip()
    else:
        packed = _safe_repo_file(repo_real, "packed-refs")
        if packed is not None:
            for line in _read_nofollow(packed).splitlines():
                if line.startswith(("#", "^")):
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1] == ref:
                    value = parts[0]
                    break
    if value is None:
        raise WorktreeError(f"branch {branch!r} not found in {bare_path}")
    if not _HEX_SHA.match(value):
        raise PushRefusedError(
            f"push refused: {ref} does not hold a plain commit id"
        )
    return value


class _UnsafeExcludeError(Exception):
    """``info/exclude`` is there but is not a plain file reached without
    a link."""


def _safe_context_target(worktree_path: Path, name: str) -> Path:
    """Resolve an adapter's instruction filename inside the worktree."""
    path = Path(name)
    if (
        not name.strip()
        or path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError(f"unsafe context filename {name!r}")
    return worktree_path.joinpath(*path.parts)


_PARENT_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)


@contextlib.contextmanager
def _context_parent(
    worktree_path: Path, name: str, *, create: bool,
) -> Iterator[tuple[int, str]]:
    """The directory holding ``name``, opened without following any link.

    Yields ``(dir_fd, basename)``. Each component below the worktree is
    opened relative to the one before with ``O_NOFOLLOW``, so a directory
    the agent swaps for a symlink, before or during cleanup, makes the
    walk fail instead of redirecting a check or an unlink outside the
    checkout. Everything done to ``basename`` then goes through ``dir_fd``.
    A missing parent raises ``FileNotFoundError`` unless ``create`` is set;
    a parent that is a link or not a directory raises ``ValueError``.
    """
    parts = _safe_context_target(worktree_path, name).relative_to(
        worktree_path
    ).parts
    fd = os.open(worktree_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for part in parts[:-1]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, dir_fd=fd)
            try:
                child = os.open(part, _PARENT_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise
            except OSError as e:
                raise ValueError(
                    f"context directory {part!r} under {worktree_path} is a "
                    "symlink or not a directory"
                ) from e
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _link_points_to(dir_fd: int, name: str, destination: str) -> bool:
    """Whether ``name`` in ``dir_fd`` is a symlink storing ``destination``.

    The relay's context links store the absolute, resolved path of the
    repo's context file, and nothing in a checkout has a reason to point
    there. Matching that exact string is what tells a relay link from a
    repository's own instruction file or link (relative, or absolute to
    somewhere else such as ``/opt/shared/AGENTS.md``): those are tracked,
    and replacing or removing them would put a type change or a deletion
    in front of an agent that stages everything.
    """
    try:
        return os.readlink(name, dir_fd=dir_fd) == destination
    except OSError:
        return False


def _add_exclude_line(admin_fd: int, where: Path, line: str) -> None:
    """Append ``line`` to ``info/exclude`` under the directory open at
    ``admin_fd`` if the file exists and lacks it, without following a link
    at ``info`` or at ``exclude`` (#263).

    A linked worktree's git directory is writable by the agent under the
    sandbox, and this runs outside it on resume. ``git worktree add``
    makes no ``info/`` there, so a file found at that path was put there
    by someone else, and a link would turn this write into one anywhere
    the relay can write. Nothing there is not an error; something there
    that is a link, or not a regular file, raises
    :class:`_UnsafeExcludeError` and nothing is written. ``where`` is the
    directory's path, for messages only.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        info_fd = os.open(
            "info", os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=admin_fd,
        )
    except FileNotFoundError:
        return
    except OSError as e:
        raise _UnsafeExcludeError(
            f"{where / 'info'} is not a directory reached without a link "
            f"({type(e).__name__})"
        ) from e
    try:
        try:
            fd = os.open(
                "exclude", os.O_RDWR | nofollow | os.O_NONBLOCK, dir_fd=info_fd,
            )
        except FileNotFoundError:
            return
        except OSError as e:
            raise _UnsafeExcludeError(
                f"{where / 'info' / 'exclude'} is not a plain file "
                f"({type(e).__name__})"
            ) from e
        with os.fdopen(fd, "r+") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise _UnsafeExcludeError(
                    f"{where / 'info' / 'exclude'} is not a regular file"
                )
            # A hard link would send the append to the other name's file.
            if st.st_nlink != 1:
                raise _UnsafeExcludeError(
                    f"{where / 'info' / 'exclude'} has {st.st_nlink} links"
                )
            content = f.read()
            if line in content:
                return
            f.seek(0)
            f.truncate()
            f.write(content.rstrip() + f"\n{line}\n")
    finally:
        os.close(info_fd)


def _relay_push_gitconfig() -> str:
    """The whole global config a credentialed push sees: one helper.

    ``gh auth git-credential`` hands git the relay's own gh login, the
    same one every other GitHub call the relay makes uses.
    """
    from ctrlrelay.core.github import _find_gh

    gh = _find_gh().replace("\\", "\\\\").replace('"', '\\"')
    return (
        '[credential "https://github.com"]\n'
        f'\thelper = "!\\"{gh}\\" auth git-credential"\n'
    )


def _relay_git_env(global_config: Path) -> dict[str, str]:
    """Environment for a credentialed git call: no inherited GIT_* at
    all (GIT_DIR, GIT_CONFIG_*, GIT_SSH_COMMAND, ...), no system config,
    and the global config replaced by ``global_config``."""
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("GIT_")
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(global_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


# The git subcommands that talk to origin. The relay runs them on the
# operator's own config (their helpers, rewrites and proxies apply), so
# these get only an extra credential helper appended, never a
# replacement config.
_NETWORK_SUBCOMMANDS = frozenset({"clone", "fetch", "ls-remote"})

_GH_CREDENTIAL_KEY = "credential.https://github.com.helper"


def _git_subcommand(args: tuple[str, ...]) -> str | None:
    """The subcommand in ``git <args>``, skipping global options and the
    values of ``-c`` and ``-C``."""
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("-c", "-C"):
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def _with_gh_credential(base: Mapping[str, str]) -> dict[str, str]:
    """``base`` with gh's login appended as a credential helper for
    https://github.com, for the relay's own fetch, clone and ls-remote
    (#398).

    The relay clones over https. On a node where gh is logged in but
    ``gh auth setup-git`` was never run, git has no credential for a
    private repository and the first run failed before the agent
    started. The helper goes in through ``GIT_CONFIG_COUNT``, appended
    after any entries already there (a unit's ``insteadOf`` rewrite
    keeps working). Config from the environment is read after every
    config file, so this helper comes last in git's list: an operator's
    own helper is asked first and wins when it answers. When gh is not
    logged in, the helper answers nothing and git fails as before.

    ``GIT_TERMINAL_PROMPT=0`` is set either way, so a missing credential
    fails at once instead of waiting on a prompt nobody will see. A
    malformed ``GIT_CONFIG_COUNT`` is left alone (git refuses it with
    its own error), and so is a node with no gh binary.
    """
    from ctrlrelay.core.github import _find_gh

    env = dict(base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    gh = shutil.which(_find_gh())
    if gh is None:
        return env
    raw = env.get("GIT_CONFIG_COUNT", "")
    try:
        count = int(raw) if raw else 0
    except ValueError:
        return env
    if count < 0:
        return env
    env[f"GIT_CONFIG_KEY_{count}"] = _GH_CREDENTIAL_KEY
    env[f"GIT_CONFIG_VALUE_{count}"] = f"!{shlex.quote(gh)} auth git-credential"
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


@dataclass
class WorktreeManager:
    """Manages git worktrees for session isolation."""

    worktrees_dir: Path
    bare_repos_dir: Path
    timeout: int = 120
    # Told when a clone is set aside and cloned again (#234), with the
    # repo and where the old clone went. Best-effort: a failure is
    # logged and never fails the run.
    on_reclone: Callable[[str, Path], Awaitable[None]] | None = None
    # The context file each worktree's links point at, recorded by
    # symlink_context so remove_context_symlink deletes only those links.
    # Every pipeline links before it cleans up, in the same process; a
    # worktree this process never linked has nothing it may remove.
    _context_targets: dict[Path, str] = field(
        default_factory=dict, init=False, repr=False,
    )

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
        tolerate_non_fast_forward: bool = False,
        protected_refs: frozenset[str] = frozenset(),
        env: dict[str, str] | None = None,
    ) -> str:
        """Run git command and return stdout. `timeout` overrides self.timeout
        for one call — useful for cheap probes that shouldn't inherit the full
        120s default when network is flaky.

        ``tolerate_non_fast_forward`` is for fetches under a non-force
        refspec: git exits 1 when it declines to rewind a diverged local
        branch, even though every other ref updated. See
        :func:`_rejections_are_only_non_fast_forward`.

        ``protected_refs`` names branches whose rejection is never
        tolerated. A declined rewind of the default branch means local
        and origin have diverged there, and every worktree cut from it
        would run against a tree origin no longer has.
        """
        cmd = ["git", *args]
        if env is None and _git_subcommand(args) in _NETWORK_SUBCOMMANDS:
            env = _with_gh_credential(os.environ)
        effective_timeout = timeout if timeout is not None else self.timeout
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            # Re-raise the SAME type (callers filter on TimeoutError) but
            # with a message attached. A bare `asyncio.TimeoutError()`
            # stringifies to "", so every layer above rendered it as
            # empty: the secops sweep reported "Error processing <repo>"
            # with no cause, and the operator got a Telegram alert that
            # named a repo and nothing else.
            raise asyncio.TimeoutError(
                f"git {' '.join(args)} timed out after "
                f"{effective_timeout}s"
                + (f" (cwd={cwd})" if cwd is not None else "")
            ) from None
        except asyncio.CancelledError:
            # Scheduler/shutdown cancel: kill the child BEFORE re-raising
            # so the git subprocess isn't left mutating the bare repo /
            # worktree after the daemon exits. A restarted daemon would
            # otherwise race with a stray `git worktree add` on the
            # same repo. Shield the reaping so a second cancel between
            # kill() and wait() doesn't leak the zombie.
            if proc.returncode is None:
                try:
                    proc.kill()
                    await asyncio.shield(proc.wait())
                except (asyncio.CancelledError, Exception):
                    pass
            raise

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            out = stdout.decode(errors="replace").strip()
            declined = (
                _rejections_are_only_non_fast_forward(err)
                if tolerate_non_fast_forward
                else []
            )
            if not declined:
                # Both streams, because which one carried the reason
                # depends on the command: a `--porcelain` push puts it
                # on stdout (#269).
                raise GitCommandError(
                    "git failed: "
                    + _git_failure_detail(out, err, proc.returncode),
                    stdout=out,
                    stderr=err,
                )
            stuck = sorted(set(declined) & protected_refs)
            if stuck:
                raise DefaultBranchDivergedError(
                    f"git failed: default branch diverged from origin "
                    f"({', '.join(stuck)}); refusing to use a stale tree: "
                    f"{err}"
                )
            log_event(
                _logger,
                "worktree.fetch.non_fast_forward",
                cwd=str(cwd) if cwd is not None else None,
                refs=declined,
                ref_count=len(declined),
            )

        return stdout.decode()

    async def _worktree_add(self, bare_path: Path, *args: str) -> None:
        """``git worktree add <args>`` in the bare clone, as the relay runs
        every checkout for an agent (#263).

        The checkout runs outside the agent sandbox, on a tree the agent
        can choose: a reused branch holds the previous agent's commits,
        and ``<bare>/info/attributes`` (which the agent can write) applies
        to every checkout from the clone. Either can select a filter
        driver by name, and the operator's own global or system config
        defines the program behind it (``git lfs install`` writes
        ``filter.lfs.*`` there). So this checkout reads no global or
        system config: no driver is defined, and every file is written
        as committed. For a repository using LFS that means the agent
        gets the pointer files. Hooks and fsmonitor are off as for every
        other relay call. The clone's own config still applies; the
        sandbox keeps it read-only.
        """
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_TERMINAL_PROMPT"] = "0"
        await self._run_git(
            *_RELAY_GIT_FLAGS, "worktree", "add", *args,
            cwd=bare_path, env=env,
        )

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

        await self._worktree_add(bare_path, str(worktree_path), branch)

        return worktree_path

    async def create_worktree_with_new_branch(
        self,
        repo: str,
        session_id: str,
        new_branch: str,
        base_branch: str | None = None,
        github: GitHubCLI | None = None,
        *,
        allow_pr: int | None = None,
    ) -> tuple[Path, bool]:
        """Create a worktree for ``new_branch``.

        If ``new_branch`` already exists in the bare repo — e.g. because a
        previous session for the same issue ran out of fix attempts and left
        its PR branch pushed to origin — reuse that branch so the retry can
        iterate on the prior commits instead of hitting
        ``fatal: a branch named 'fix/issue-N' already exists`` from
        ``git worktree add -b``. Without this, any issue that exhausts the
        verify-fix loop once gets permanently wedged until someone manually
        deletes the branch.

        When we have to fall back to a brand-new branch, it's cut from
        ``base_branch`` (default: the repo's default branch).

        Returns ``(worktree_path, created_fresh)`` where ``created_fresh``
        is True when THIS call either created a brand-new branch from
        ``base_branch`` or detected a stale fully-merged local branch,
        deleted it, and recreated it. Callers use this flag to decide
        whether cleanup on session failure should delete the branch
        (issue #51): a pre-snapshotted ``branch_exists_locally`` check
        goes stale the moment we delete+recreate, so the ownership
        signal has to come from the function that performed the
        mutation.

        When ``github`` is provided and the branch already exists locally,
        probe GitHub for an open PR with ``new_branch`` as head. If one
        exists, refuse to reuse the branch — the reviewer's already-
        reviewed work must not be hijacked by a fresh session, and
        ``gh pr create`` would later fail with "A pull request already
        exists" (issue #52). Raises :class:`WorktreeError` with the PR
        number and a concrete operator action. The probe runs BEFORE
        any ref mutations so a concurrent PR is surfaced cleanly.

        ``allow_pr`` is the one open PR the caller already owns: a run
        continuing an interrupted one that had opened it (#231). Only that
        number is let through; any other open PR still refuses.
        """
        # Per-call flag: tracks whether we committed to the stale-merged
        # delete+recreate path. Must stay local to this coroutine call —
        # placing it on self would race under concurrent sessions sharing
        # the WorktreeManager instance (one session's start-of-call reset
        # would clobber another's in-progress signal).
        entered_stale_recreate = False

        bare_path = self._get_bare_repo_path(repo)
        worktree_path = self._get_worktree_path(repo, session_id)

        if worktree_path.exists():
            raise WorktreeError(f"Worktree already exists: {worktree_path}")

        if await self.branch_exists_locally(repo, new_branch):
            # CRITICAL safety: never mutate or delete a branch that's still
            # checked out by another linked worktree (e.g. a BLOCKED session
            # that kept its worktree alive on purpose so it can be resumed
            # when the operator replies). `git update-ref` / `update-ref -d`
            # don't refuse in that case; they'd leave the live worktree's
            # HEAD pointing at a stale or missing ref, corrupting the
            # resumable state. Surface a clean error instead.
            if await self._branch_is_checked_out_elsewhere(bare_path, new_branch):
                raise WorktreeError(
                    f"Branch {new_branch!r} is already checked out in another "
                    f"worktree of {repo!r}. Resolve with `git worktree list` + "
                    "`git worktree remove` in the bare repo before retrying."
                )

            # Issue #52: probe for an open same-repo PR BEFORE any ref
            # mutation. Must run for BOTH on-remote and local-only
            # branches — GitHub can keep a PR open after its head
            # branch is deleted, and our next push would recreate
            # origin/<branch>, re-attaching this session's commits to
            # the reviewer-owned PR (codex round-7). Fail-closed on
            # probe error is the correct trade-off: a flaky gh can
            # delay recovery, but a missed probe silently hijacks a
            # PR. See _refuse_if_branch_backs_open_pr.
            if github is not None:
                await self._refuse_if_branch_backs_open_pr(
                    github, repo, new_branch, allow_pr=allow_pr,
                )

            # Remote presence has to be KNOWN to take either sync-to-origin
            # or stale-merged branches. branch_exists_on_remote is
            # fail-closed (returns True on timeout/auth error) which is
            # good for "should I delete this branch" decisions but wrong
            # here — a transient probe failure would skip the stale-merged
            # recreate path and resurrect already-merged commits. Use the
            # strict variant that raises on probe failure; on error we
            # reuse the local ref unchanged, without mutations.
            try:
                on_remote = await self._branch_exists_on_remote_strict(
                    bare_path, new_branch,
                )
            except Exception:
                await self._worktree_add_with_stale_cleanup(
                    bare_path, worktree_path, new_branch,
                )
                return worktree_path, False

            if on_remote:
                # Remote exists → sync local to remote head (preserving
                # unpushed ahead-of-origin commits) and reuse.
                await self._sync_reused_branch_to_origin(bare_path, new_branch)
                await self._worktree_add_with_stale_cleanup(
                    bare_path, worktree_path, new_branch,
                )
                return worktree_path, False

            # Local-only (confirmed). Distinguish between:
            #   (a) stale-merged: the prior PR was merged (any strategy) and
            #       the remote branch auto-deleted. The local ref's commits
            #       are all patch-equivalent to commits in the default
            #       branch. Reusing it would resurrect already-merged
            #       changes into the next PR — delete + create fresh.
            #   (b) recoverable unpushed work: the prior session made
            #       commits locally and died before push. Local has
            #       content not represented in the default branch — reuse
            #       so the operator's work isn't silently dropped.
            if await self._branch_is_fully_merged(repo, new_branch):
                # Commit to the delete+recreate path. From here on,
                # any ref left on disk — whether the old one we just
                # deleted, a partial one `worktree add -b` wrote
                # before crashing, or the successfully recreated one
                # — belongs to THIS session. Flip the local flag
                # BEFORE the delete so even an exception in
                # update-ref still surfaces StaleRecreatePartialFailureError
                # to the caller.
                entered_stale_recreate = True
                try:
                    await self._run_git(
                        "update-ref", "-d", f"refs/heads/{new_branch}",
                        cwd=bare_path, timeout=10,
                    )
                except Exception:
                    pass
                # Fall through to the fresh-branch creation below. The
                # delete+recreate path is the exact scenario issue #51
                # covers: the branch that now exists was created by THIS
                # session, so cleanup on failure must treat it as ours.
            else:
                await self._worktree_add_with_stale_cleanup(
                    bare_path, worktree_path, new_branch,
                )
                return worktree_path, False

        if base_branch is None:
            base_branch = await self.get_default_branch(repo)

        try:
            await self._worktree_add(
                bare_path, "-b", new_branch, str(worktree_path), base_branch,
            )
        except Exception as e:
            # If we destroyed the old stale-merged ref on the way here,
            # re-raise as StaleRecreatePartialFailureError so the caller
            # knows any leftover ref belongs to this session. Without
            # this signal, a pre-call branch_existed_before=True
            # snapshot would misclassify the leftover as "not ours"
            # and leak it into the next retry (issue #51).
            if entered_stale_recreate:
                raise StaleRecreatePartialFailureError(str(e)) from e
            raise
        return worktree_path, True

    async def rewind_branch(
        self, repo: str, branch: str, sha: str, *, backup_ref: str,
    ) -> str:
        """Move ``branch`` back to ``sha`` in the bare repo, before any
        worktree is made from it, for a run that starts again from a past
        step (#231). Returns the commit the branch was on.

        The old head is kept first under ``backup_ref``, so nothing the
        branch held is lost. Refused when ``sha`` is not a commit here,
        when the branch is checked out in a worktree (moving it would pull
        a live worktree's HEAD out from under it), or when the branch moved
        between the read and the write.
        """
        if not _is_safe_branch(branch):
            raise WorktreeError(f"refusing to rewind unsafe branch {branch!r}")
        if not _HEX_SHA.match(sha):
            raise WorktreeError(f"not a commit id: {sha!r}")
        bare_path = self._get_bare_repo_path(repo)
        if await self._branch_is_checked_out_elsewhere(bare_path, branch):
            raise WorktreeError(
                f"Branch {branch!r} is checked out in a worktree of {repo!r}; "
                "it cannot be rewound while that worktree uses it."
            )
        try:
            await self._run_git(
                "cat-file", "-e", f"{sha}^{{commit}}", cwd=bare_path, timeout=10,
            )
        except WorktreeError as e:
            raise WorktreeError(
                f"commit {sha} is no longer in the clone of {repo!r}"
            ) from e
        try:
            old = _read_branch_head(bare_path, branch)
        except WorktreeError:
            # The branch is gone (deleted after an earlier run). Make it
            # again at the saved commit; the empty old value makes git
            # refuse if something created it meanwhile.
            await self._run_git(
                "update-ref", f"refs/heads/{branch}", sha, "",
                cwd=bare_path, timeout=10,
            )
            return ""
        if old == sha:
            return old
        await self._run_git(
            "update-ref", backup_ref, old, cwd=bare_path, timeout=10,
        )
        await self._run_git(
            "update-ref", f"refs/heads/{branch}", sha, old,
            cwd=bare_path, timeout=10,
        )
        return old

    async def refuse_if_branch_backs_open_pr(
        self, github: GitHubCLI, repo: str, branch: str,
    ) -> None:
        """Raise :class:`WorktreeError` when ``branch`` backs an open PR.
        The same fail-closed probe as branch reuse (#52)."""
        await self._refuse_if_branch_backs_open_pr(github, repo, branch)

    async def _refuse_if_branch_backs_open_pr(
        self, github: GitHubCLI, repo: str, branch: str,
        *, allow_pr: int | None = None,
    ) -> None:
        """Issue #52: raise WorktreeError if ``branch`` is the head of an
        open PR on ``repo``. Probe is fail-closed (on repeated probe
        error, we refuse reuse): the dev workflow pushes to the shared
        branch BEFORE calling ``gh pr create``, so relying on the
        later create to catch a PR-backed branch is unsafe — a
        transient ``gh`` error would let the push hijack the existing
        PR before any failure surfaces.

        ``gh pr list --head`` filters by branch name only, so PRs from
        unrelated repos (external forks, same-owner forks like
        ``acme/repo-fork`` targeting ``acme/repo``) with the same
        branch name would appear in the result and wrongly block
        our reuse. Filter client-side on the FULL head repo identity
        (owner + name) so only PRs whose head actually lives in the
        repo we're about to push to can veto reuse.
        """
        last_exc: Exception | None = None
        prs: list | None = None
        for attempt in range(_PR_PROBE_RETRY_ATTEMPTS):
            try:
                prs = await github.list_prs(
                    repo, state="open", head=branch,
                    timeout=_PR_PROBE_TIMEOUT_SECONDS,
                )
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                if attempt < _PR_PROBE_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_PR_PROBE_RETRY_SLEEP_SECONDS)
        if prs is None:
            raise WorktreeError(
                f"Could not determine whether branch {branch!r} on "
                f"{repo!r} backs an open PR (gh probe failed after "
                f"{_PR_PROBE_RETRY_ATTEMPTS} attempts: "
                f"{type(last_exc).__name__ if last_exc else 'unknown'}). "
                "Refusing reuse to avoid hijacking an open PR. Retry "
                "once gh is reachable, or delete the branch manually "
                "if you're certain no PR is open."
            ) from last_exc
        target_owner, _, target_name = repo.partition("/")
        # GitHub owner/repo names are case-insensitive: config may say
        # "Owner/Repo" while the API returns "owner/repo". Normalize
        # both sides before comparing so a case mismatch doesn't let
        # a colliding PR slip past the veto.
        target_owner_lc = target_owner.lower()
        target_name_lc = target_name.lower()

        def _same_repo(pr: dict) -> bool:
            owner = (pr.get("headRepositoryOwner") or {}).get("login") or ""
            name = (pr.get("headRepository") or {}).get("name") or ""
            return (
                owner.lower() == target_owner_lc
                and name.lower() == target_name_lc
            )

        same_repo_prs = [p for p in prs if _same_repo(p)]
        if allow_pr is not None:
            same_repo_prs = [
                p for p in same_repo_prs if p.get("number") != allow_pr
            ]
        if not same_repo_prs:
            return
        pr_number = same_repo_prs[0].get("number")
        raise WorktreeError(
            f"Branch {branch!r} already has open PR #{pr_number} on "
            f"{repo!r} — close or merge it before retrying this issue."
        )

    async def _worktree_add_with_stale_cleanup(
        self, bare_path: Path, worktree_path: Path, branch: str,
    ) -> None:
        """``git worktree add <path> <branch>`` with targeted recovery
        if the add fails because a stale (prunable) admin entry still
        claims the branch.

        Try the add. If it succeeds, done. If it fails with "already
        checked out" AND we've already established there's no LIVE
        checkout of this branch (the caller checked
        _branch_is_checked_out_elsewhere), we scope a `git worktree
        prune` to this one stale entry and retry.

        We deliberately do NOT prune up front: an unconditional prune
        in a bare repo whose worktrees_dir is on a removable / network
        path would destroy admin state for unrelated worktrees whose
        paths are temporarily unavailable. Scoping to this specific
        branch via a post-failure recovery keeps the blast radius
        minimal.
        """
        try:
            await self._worktree_add(bare_path, str(worktree_path), branch)
            return
        except WorktreeError as e:
            if "already checked out" not in str(e):
                raise
            # Find the stale admin entry for this branch and clear it
            # WITHOUT running repo-wide `git worktree prune` — that would
            # also discard admin state for unrelated prunable worktrees
            # whose paths are temporarily unavailable (removable media,
            # network mounts), making them unrecoverable when the path
            # comes back. Instead, delete just the one admin directory.
            stale_path = await self._find_stale_worktree_path(bare_path, branch)
            if stale_path is None:
                raise
            # Only touch stale entries we OWN — under our managed
            # worktrees_dir — AND only when our worktrees_dir is actually
            # accessible on disk right now. A prunable stanza can also
            # represent a worktree on a temporarily-unavailable path
            # (network mount, removable drive); if the mount is down,
            # every managed worktree under it appears prunable. Deleting
            # their admin state would orphan real checkouts when the
            # mount comes back.
            #
            # Safety gate: require worktrees_dir itself to exist and be
            # a directory. If the whole mount is offline, bail.
            if not self.worktrees_dir.is_dir():
                raise e
            try:
                stale_resolved = Path(stale_path).resolve()
                wt_root = self.worktrees_dir.resolve()
                stale_resolved.relative_to(wt_root)
            except (OSError, ValueError):
                raise e  # not under our dir — unsafe to recover
            # Locate the admin dir via its canonical `gitdir` pointer,
            # NOT by assuming the path basename matches. Git sanitizes
            # names (`foo bar` → `foo-bar`) and disambiguates duplicates
            # (`wt`, `wt1`, …), so Path(stale_path).name can point at the
            # wrong admin dir or a live unrelated worktree's metadata.
            # Each admin dir has a `gitdir` file that points at
            # `<worktree-path>/.git` — that pointer is the source of truth.
            admin_dir = self._resolve_admin_dir(bare_path, stale_path)
            if admin_dir is not None and admin_dir.exists():
                try:
                    shutil.rmtree(admin_dir)
                except OSError:
                    pass
            # Retry the add; if it fails again, let the error surface.
            await self._worktree_add(bare_path, str(worktree_path), branch)

    def detach_orphaned_worktree(
        self, repo: str, worktree_path: Path | str,
    ) -> str | None:
        """Free the branch a dead run's worktree holds, keeping every file
        in it (#312).

        A worktree that still has the issue's branch checked out makes the
        next run for that issue fail with "already checked out". Removing
        the worktree would free the branch and throw away what the agent
        wrote. Detaching its HEAD frees the branch and keeps the files, the
        index and the commit they sit on, so the work can still be read
        and recovered by hand.

        No git process runs here. The worktree's config, and the bare
        clone's that it shares, can be written by the agent, and a git
        command in that tree would run whatever filters or hooks they name,
        outside the agent's sandbox. So the detach rewrites the admin dir's
        ``HEAD`` file directly. The bare clone and every path step below it
        are opened relative to the one before with ``O_NOFOLLOW``, and
        the new file is created and renamed through that directory's
        descriptor, so a link planted inside the clone, before or during
        this, cannot redirect the write. The directories above the clone
        are the relay's own configured paths and are trusted as they are.

        Returns the branch it freed, or ``None`` when there was nothing to
        free (no admin dir, already detached) or anything was unsafe.
        """
        from ctrlrelay.core.publish import PushRefusedError

        bare_path = self._get_bare_repo_path(repo)
        admin_dir = self._resolve_admin_dir(bare_path, str(worktree_path))
        if admin_dir is None:
            return None
        prefix = "ref: refs/heads/"
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
        fds: list[int] = []
        try:
            # The clone itself as well: a link in its place is refused.
            fds.append(os.open(str(bare_path), dir_flags))
            for part in ("worktrees", admin_dir.name):
                fds.append(os.open(part, dir_flags, dir_fd=fds[-1]))
            admin_fd = fds[-1]
            # Non-blocking, then a plain file or nothing: a FIFO planted
            # as HEAD would otherwise hold the poller's startup for ever.
            head_fd = os.open(
                "HEAD", os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
                dir_fd=admin_fd,
            )
            with os.fdopen(head_fd, "r") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    return None
                content = f.read(4096).strip()
            if not content.startswith(prefix):
                return None
            branch = content[len(prefix):]
            # A plain commit id, or it raises: see `_read_branch_head`.
            sha = _read_branch_head(bare_path, branch)
            tmp = f".HEAD.ctrlrelay-{secrets.token_hex(8)}"
            tmp_fd = os.open(
                tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o644,
                dir_fd=admin_fd,
            )
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    f.write(sha + "\n")
                os.replace(tmp, "HEAD", src_dir_fd=admin_fd, dst_dir_fd=admin_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=admin_fd)
                raise
        except (OSError, WorktreeError, PushRefusedError) as e:
            log_event(
                _logger, "worktree.orphan_detach_failed", repo=repo,
                worktree_path=str(worktree_path),
                reason=type(e).__name__, error=str(e)[:200],
            )
            return None
        finally:
            for fd in fds:
                with contextlib.suppress(OSError):
                    os.close(fd)
        return branch

    def _worktree_head_sha(self, repo: str, worktree_path: Path | str) -> str:
        """The commit a worktree sits on, read from its admin dir's
        ``HEAD`` file as data, with the same no-link, plain-file reads as
        :meth:`detach_orphaned_worktree`. Raises :class:`WorktreeError` (or
        ``PushRefusedError`` for an unsafe ref) when it cannot be read."""
        bare_path = self._get_bare_repo_path(repo)
        admin_dir = self._resolve_admin_dir(bare_path, str(worktree_path))
        if admin_dir is None:
            raise WorktreeError(f"no worktree entry for {worktree_path}")
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
        fds: list[int] = []
        try:
            fds.append(os.open(str(bare_path), dir_flags))
            for part in ("worktrees", admin_dir.name):
                fds.append(os.open(part, dir_flags, dir_fd=fds[-1]))
            head_fd = os.open(
                "HEAD", os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
                dir_fd=fds[-1],
            )
            with os.fdopen(head_fd, "r") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    raise WorktreeError("worktree HEAD is not a plain file")
                content = f.read(4096).strip()
        except OSError as e:
            raise WorktreeError(f"cannot read worktree HEAD: {e}") from e
        finally:
            for fd in fds:
                with contextlib.suppress(OSError):
                    os.close(fd)
        prefix = "ref: refs/heads/"
        if content.startswith(prefix):
            return _read_branch_head(bare_path, content[len(prefix):])
        if _HEX_SHA.match(content):
            return content
        raise WorktreeError("worktree HEAD holds neither a branch nor a commit")

    async def save_unfinished_work(
        self,
        repo: str,
        worktree_path: Path | str,
        *,
        wip_branch: str,
        message: str,
        default_branch: str | None,
        relay_pushed_shas: frozenset[str],
        session_id: str | None = None,
    ) -> str | None:
        """Push what a run left in its worktree to ``wip_branch`` (#311).

        For a run that is being given up on, such as one that timed out:
        the edits it had not committed and the commits it had not pushed
        would otherwise go with the worktree. The worktree's files, as they
        are on disk, are committed on top of the commit it sits on, and
        that commit is pushed to ``wip_branch``, which must be ``wip/...``.
        The push only creates it: a branch of that name already on GitHub
        at another commit is refused, never moved. Nothing is pushed to the
        run's own branch, and no pull request is opened.

        What is not saved: files the work tree's ``.gitignore`` files
        ignore (build output, virtualenvs, caches; a list the agent can
        also edit), the contents of a nested git repository (recorded as
        a link to its commit, as git does), and anything staged in the
        agent's index that differs from the file on disk. The files on
        disk are the agent's latest state.

        Raises :class:`PartialSaveError`, after pushing, when some files
        could not be read and so are not on the branch.

        Returns the pushed SHA, or ``None`` when there is nothing to save:
        no uncommitted change, and the commit the worktree sits on is on
        ``default_branch`` already or was pushed by the relay. Raises
        when the work could not be saved; the caller must then keep the
        worktree.

        Same trust boundary as :meth:`push_run_branch`. The worktree's
        files are read by a git that uses a fresh git dir created here,
        an index file created here, and a config written here: no system
        config, a global config holding only the credential helper, no
        inherited ``GIT_*`` variable, hooks and fsmonitor off. So the
        filters, hooks and attributes file the agent could name in the
        clone's or the worktree's config are never read. The work tree's
        ``.gitattributes`` files are read, but this git dir's
        ``info/attributes`` overrides every attribute that could run a
        program or change the bytes.
        The bare clone's objects are borrowed through
        ``objects/info/alternates``, and its refs are read as files.
        """
        from ctrlrelay.core.publish import PushRefusedError, github_push_url

        if not wip_branch.startswith("wip/") or not _is_safe_branch(wip_branch):
            raise PushRefusedError(
                f"push refused: {wip_branch!r} is not a wip/ branch name"
            )
        work_tree = Path(worktree_path)
        if not work_tree.is_dir() or work_tree.is_symlink():
            raise WorktreeError(f"worktree {work_tree} is not a directory")
        head_sha = self._worktree_head_sha(repo, work_tree)
        bare_path = self._get_bare_repo_path(repo)
        base_sha: str | None = None
        if default_branch:
            with contextlib.suppress(WorktreeError, PushRefusedError, OSError):
                base_sha = _read_branch_head(bare_path, default_branch)
        url = github_push_url(repo)
        push_root = self.bare_repos_dir.parent / "push"
        push_root.mkdir(parents=True, exist_ok=True)
        push_root.chmod(0o700)
        workdir = Path(tempfile.mkdtemp(prefix="wip-", dir=push_root))
        try:
            gitdir = workdir / "repo.git"
            global_cfg = workdir / "gitconfig"
            global_cfg.write_text(_relay_push_gitconfig())
            env = _relay_git_env(global_cfg)
            env["GIT_INDEX_FILE"] = str(workdir / "index")
            for who in ("AUTHOR", "COMMITTER"):
                env[f"GIT_{who}_NAME"] = "ctrlrelay"
                env[f"GIT_{who}_EMAIL"] = "ctrlrelay@localhost"

            def git(*args: str, tree: bool = False) -> list[str]:
                where = ["--work-tree", str(work_tree)] if tree else []
                return ["--git-dir", str(gitdir), *where, *_RELAY_GIT_FLAGS,
                        "-c", "core.excludesFile=/dev/null", *args]

            await self._run_git("init", "-q", "--bare", str(gitdir), env=env)
            (gitdir / "objects" / "info" / "alternates").write_text(
                str((bare_path / "objects").resolve()) + "\n"
            )
            # `info/attributes` outranks every `.gitattributes` in the work
            # tree, so the agent's attributes cannot change the bytes saved
            # (end-of-line conversion, `ident`, re-encoding) or name a
            # filter. Works on any git, unlike `--attr-source`.
            (gitdir / "info").mkdir(exist_ok=True)
            (gitdir / "info" / "attributes").write_text(
                "* -text -ident -filter -working-tree-encoding !eol\n"
            )
            await self._run_git(
                *git("read-tree", "--end-of-options", head_sha), env=env,
            )
            # A file it cannot read (a root-owned leftover, a socket) is
            # skipped rather than failing the whole save: with
            # --ignore-errors git adds the rest, writes the index, and
            # exits 1 naming what it skipped.
            add_failed = False
            try:
                await self._run_git(
                    *git("add", "--all", "--ignore-errors", "--", ".",
                         tree=True),
                    cwd=work_tree, env=env,
                )
            except GitCommandError as e:
                add_failed = True
                log_event(
                    _logger, "publish.wip.files_skipped", repo=repo,
                    session_id=session_id, error=str(e)[:400],
                )
            tree = (await self._run_git(*git("write-tree"), env=env)).strip()
            head_tree = (
                await self._run_git(
                    *git("rev-parse", "--verify", "--quiet", "--end-of-options",
                         f"{head_sha}^{{tree}}"),
                    env=env,
                )
            ).strip()
            if tree == head_tree and add_failed:
                # An add that failed can leave the index as HEAD's, which
                # reads as "nothing changed". Not knowing is not nothing:
                # the caller keeps the worktree.
                raise WorktreeError(
                    "could not read the worktree's files to save them"
                )
            if tree == head_tree:
                if head_sha in relay_pushed_shas:
                    return None
                if base_sha is not None:
                    try:
                        await self._run_git(
                            *git("merge-base", "--is-ancestor", head_sha,
                                 base_sha),
                            env=env,
                        )
                        return None
                    except GitCommandError:
                        pass
                sha = head_sha
            else:
                sha = (
                    await self._run_git(
                        *git("commit-tree", tree, "-p", head_sha, "-m", message),
                        env=env,
                    )
                ).strip()
            # Create-only: an empty lease refuses a branch that exists.
            await self._run_git(
                *git(
                    "push", "--no-verify", "--porcelain",
                    f"--force-with-lease=refs/heads/{wip_branch}:",
                    url, f"{sha}:refs/heads/{wip_branch}",
                ),
                env=env,
                timeout=_GIT_TRANSFER_TIMEOUT_SECONDS,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        log_event(
            _logger, "publish.wip.saved", repo=repo, session_id=session_id,
            branch=wip_branch, sha=sha, partial=add_failed,
        )
        if add_failed:
            raise PartialSaveError(
                f"saved to {wip_branch}, but some files could not be read",
                sha=sha,
            )
        return sha

    def _resolve_admin_dir(
        self, bare_path: Path, worktree_dir: str,
    ) -> Path | None:
        """Find the bare repo's admin dir whose ``gitdir`` pointer file
        resolves to ``<worktree_dir>/.git``. Returns None if no
        matching entry exists.

        Git may name admin dirs differently from the worktree path
        basename (sanitization, collision-suffix), so the only robust
        way to pair them is to inspect each admin's gitdir pointer.
        """
        worktrees_root = bare_path / "worktrees"
        if not worktrees_root.is_dir():
            return None
        target = str(Path(worktree_dir) / ".git")
        try:
            entries = list(worktrees_root.iterdir())
        except OSError:
            return None
        for admin_dir in entries:
            gitdir_file = admin_dir / "gitdir"
            if not gitdir_file.exists():
                continue
            try:
                pointer = gitdir_file.read_text().strip()
            except OSError:
                continue
            if pointer == target:
                return admin_dir
        return None

    async def _find_stale_worktree_path(
        self, bare_path: Path, branch: str,
    ) -> str | None:
        """If ``refs/heads/<branch>`` is claimed by a worktree entry marked
        ``prunable``, return the path of that entry. Otherwise None.
        Used to confirm that a "already checked out" failure is safe to
        recover from via prune (i.e. the stale entry IS for our branch
        and not a real live checkout).
        """
        try:
            output = await self._run_git(
                "worktree", "list", "--porcelain",
                cwd=bare_path, timeout=10,
            )
        except Exception:
            return None
        target = f"refs/heads/{branch}"
        current_path: str | None = None
        current_branch: str | None = None
        current_prunable = False
        for raw in output.splitlines() + [""]:
            line = raw.strip()
            if not line:
                if (
                    current_branch == target
                    and current_prunable
                    and current_path is not None
                ):
                    return current_path
                current_path = None
                current_branch = None
                current_prunable = False
                continue
            if line.startswith("worktree "):
                current_path = line[9:].strip()
            elif line.startswith("branch "):
                current_branch = line[7:].strip()
            elif line.startswith("prunable"):
                current_prunable = True
        return None

    async def _sync_reused_branch_to_origin(
        self, bare_path: Path, branch: str,
    ) -> None:
        """Fast-forward the local ``branch`` to origin's head using a
        scratch ref so it never touches ``refs/heads/<branch>`` except
        via a safe `update-ref` that's gated by a merge-base ancestor
        check.

        Bare clones from `git clone --bare` have fetch refspec
        `+refs/heads/*:refs/heads/*`, so there are NO
        `refs/remotes/origin/*` tracking refs AND an unscoped
        `git fetch origin <branch>` would overwrite the live local ref
        directly — destroying any unpushed local commits before we get
        to compare. Routing through a scratch ref in
        `refs/ctrlrelay/sync/*` avoids both problems.

        Rules:
          - If local is ancestor of the fetched remote tip → fast-forward.
          - If local is ahead or diverged → leave local alone (ahead
            contains unpushed work; forcing would destroy it).

        All steps are best-effort: any failure (timeout, network,
        permissions, bad ref) falls back to reusing the local ref
        unchanged, so a flaky origin never aborts the retry.
        """
        scratch_ref = f"refs/ctrlrelay/sync/{branch}"
        fetched = False
        try:
            await self._run_git(
                "fetch", "origin",
                f"+refs/heads/{branch}:{scratch_ref}",
                cwd=bare_path, timeout=30,
            )
            fetched = True
        except Exception:
            return
        try:
            # Distinguish three cases cleanly:
            #   a) local is ancestor of remote → local behind → fast-forward
            #   b) remote is ancestor of local → local ahead (unpushed work) → preserve
            #   c) neither → diverged → raise; caller surfaces as session failure
            #      so we don't silently reuse a branch that will fail on push.
            local_behind = False
            local_ahead = False
            try:
                await self._run_git(
                    "merge-base", "--is-ancestor", branch, scratch_ref,
                    cwd=bare_path, timeout=10,
                )
                local_behind = True
            except WorktreeError:
                pass
            except Exception:
                # Timeout or other — play safe and preserve local.
                return
            if not local_behind:
                try:
                    await self._run_git(
                        "merge-base", "--is-ancestor", scratch_ref, branch,
                        cwd=bare_path, timeout=10,
                    )
                    local_ahead = True
                except WorktreeError:
                    pass
                except Exception:
                    return

            if local_behind:
                try:
                    await self._run_git(
                        "update-ref", f"refs/heads/{branch}", scratch_ref,
                        cwd=bare_path, timeout=10,
                    )
                except Exception:
                    pass
            elif local_ahead:
                # Preserve local — ahead means unpushed operator work.
                pass
            else:
                # Diverged: local has commits origin doesn't, and origin has
                # commits local doesn't. Reusing either side silently loses
                # work or produces non-ff pushes. Surface the conflict so
                # the session fails cleanly.
                raise WorktreeError(
                    f"Local branch {branch!r} has diverged from "
                    f"origin/{branch}. Rebase or reset manually in the bare "
                    "repo before retrying."
                )
        finally:
            if fetched:
                try:
                    await self._run_git(
                        "update-ref", "-d", scratch_ref,
                        cwd=bare_path, timeout=10,
                    )
                except Exception:
                    pass

    async def _branch_exists_on_remote_strict(
        self, bare_path: Path, branch: str,
    ) -> bool:
        """Strict variant of branch_exists_on_remote: True if origin
        has the branch, False if it does not, RAISES on probe failure.

        The public branch_exists_on_remote is fail-closed (returns True
        on error) so callers making "safe to delete" decisions err
        on preservation. That semantic is wrong for the reuse path,
        where a transient error must NOT be interpreted as "remote
        exists" — that would skip the stale-merged recreate branch and
        resurrect already-merged commits into a new PR.
        """
        output = await self._run_git(
            "ls-remote", "--heads", "origin", branch,
            cwd=bare_path, timeout=10,
        )
        return bool(output.strip())

    async def _branch_is_checked_out_elsewhere(
        self, bare_path: Path, branch: str,
    ) -> bool:
        """Return True if ``refs/heads/<branch>`` is currently checked out
        by any LIVE linked worktree of this bare repo.

        `git worktree list --porcelain` emits a blank-line-separated
        stanza per worktree. A stanza with a ``prunable <reason>`` line
        is a stale metadata entry — the worktree directory is gone, we
        just haven't run ``git worktree prune`` yet. Such stanzas don't
        represent a real checkout and MUST be ignored here, otherwise a
        crash between ``shutil.rmtree()`` and ``worktree prune`` wedges
        the branch for all future retries.

        Conservative (fails closed): if the probe itself errors, returns
        True — better to refuse mutation than to risk corrupting a live
        worktree.
        """
        try:
            output = await self._run_git(
                "worktree", "list", "--porcelain",
                cwd=bare_path, timeout=10,
            )
        except Exception:
            return True

        target = f"refs/heads/{branch}"
        current_branch: str | None = None
        current_prunable = False
        for raw in output.splitlines() + [""]:  # trailing "" to flush last stanza
            line = raw.strip()
            if not line:
                # End of stanza — check if this one matches and is live.
                if (
                    current_branch == target
                    and not current_prunable
                ):
                    return True
                current_branch = None
                current_prunable = False
                continue
            if line.startswith("branch "):
                current_branch = line[7:].strip()
            elif line.startswith("prunable"):
                current_prunable = True
        return False

    async def _branch_is_fully_merged(self, repo: str, branch: str) -> bool:
        """Return True if every commit reachable from ``branch`` is
        patch-equivalent to something already in the default branch.

        Uses ``git cherry <default> <branch>`` which compares commits
        by patch-id (content), not by SHA — so this catches squash and
        rebase merges too, not just regular merge commits.

        Conservative: on any error (unknown default branch, cherry
        failure) returns False so we preserve the local ref.
        """
        bare_path = self._get_bare_repo_path(repo)
        try:
            default = await self.get_default_branch(repo)
        except Exception:
            return False
        try:
            out = await self._run_git(
                "cherry", default, branch,
                cwd=bare_path, timeout=15,
            )
        except Exception:
            return False
        lines = [line for line in out.splitlines() if line.strip()]
        if not lines:
            # No commits in branch not in default — fully merged / stale.
            return True
        # Each line starts with "+" (unique) or "-" (patch-equivalent in upstream).
        return all(line.startswith("-") for line in lines)

    async def push_run_branch(
        self,
        repo: str,
        branch: str,
        *,
        run_branch: str,
        default_branch: str,
        expected_remote_sha: str | None,
        relay_pushed_shas: frozenset[str],
        session_id: str | None = None,
    ) -> str:
        """Push this run's branch to GitHub with the relay's credentials.

        The only push the relay makes on an agent's behalf (#219):

        - :func:`check_push_target` refuses ``main``, ``master``, the
          default branch and anything that is not ``run_branch``.
        - The branch must carry at least one commit the default branch
          does not, or there is nothing to open a pull request for.
        - The pushed object is the SHA read once here, not a ref name.
        - The remote is an explicit URL built from the validated repo
          name, never ``origin``, and the push runs from a fresh git dir
          created for this one push in a relay directory. Its config is
          written here, system config is off, the global config is a
          file written here holding only the credential helper, no
          ``GIT_*`` variable is inherited, and hooks and fsmonitor are
          off on the command line.
        - The bare clone's config is never read: its config is shared
          with every worktree cut from it, so the agent can write it.
          The two branch heads are read from its ref files as data, and
          its objects are borrowed through ``objects/info/alternates``.
        - Lease: ``expected_remote_sha`` is what this session pushed
          last. When it is None, a branch that does not exist on GitHub
          is created; one that does is pushed over only if its head is
          a SHA in ``relay_pushed_shas`` (ctrlrelay put it there), and
          refused otherwise rather than overwritten.

        What this does NOT guarantee: the agent runs as the same OS user,
        so a deliberately hostile agent can read credential files, and
        can write to the relay's push directory, the bare clone's object
        store and any alternates chain it adds there, racing this push.
        That boundary needs a separate user or a read-only home (#236).

        Returns the SHA pushed.
        """
        from ctrlrelay.core.publish import (
            PushRefusedError,
            check_push_target,
            github_push_url,
        )

        check_push_target(
            branch,
            run_branch=run_branch,
            default_branch=default_branch,
            repo=repo,
            session_id=session_id,
        )
        url = github_push_url(repo)
        bare_path = self._get_bare_repo_path(repo)
        sha = _read_branch_head(bare_path, branch)
        base_sha = _read_branch_head(bare_path, default_branch)
        push_root = self.bare_repos_dir.parent / "push"
        push_root.mkdir(parents=True, exist_ok=True)
        push_root.chmod(0o700)
        workdir = Path(tempfile.mkdtemp(prefix="push-", dir=push_root))
        try:
            gitdir = workdir / "repo.git"
            global_cfg = workdir / "gitconfig"
            global_cfg.write_text(_relay_push_gitconfig())
            env = _relay_git_env(global_cfg)

            def git(*args: str) -> list[str]:
                return ["--git-dir", str(gitdir), *_RELAY_GIT_FLAGS, *args]

            await self._run_git("init", "-q", "--bare", str(gitdir), env=env)
            (gitdir / "objects" / "info" / "alternates").write_text(
                str((bare_path / "objects").resolve()) + "\n"
            )
            for value in (sha, base_sha):
                await self._run_git(
                    *git("rev-parse", "--verify", "--quiet",
                         "--end-of-options", f"{value}^{{commit}}"),
                    env=env,
                )
            ahead = (
                await self._run_git(
                    *git("rev-list", "--count", f"{base_sha}..{sha}"), env=env,
                )
            ).strip()
            if ahead == "0":
                log_event(
                    _logger,
                    "publish.push.nothing_to_push",
                    repo=repo,
                    session_id=session_id,
                    branch=branch,
                )
                from ctrlrelay.core.publish import NothingToPushError

                raise NothingToPushError(
                    f"push refused: {branch!r} has no commits that "
                    f"{default_branch!r} does not already have; the agent "
                    f"must commit its work on {branch!r}"
                )
            if expected_remote_sha is None:
                out = await self._run_git(
                    *git("ls-remote", "--heads", url, f"refs/heads/{branch}"),
                    env=env,
                    timeout=_GIT_TRANSFER_TIMEOUT_SECONDS,
                )
                remote = out.split()[0] if out.strip() else ""
                if remote and remote not in relay_pushed_shas:
                    _logger.error(
                        "publish.push.refused",
                        extra={
                            "repo": repo,
                            "session_id": session_id,
                            "branch": branch,
                            "remote_sha": remote,
                            "reason": "remote branch not pushed by ctrlrelay",
                        },
                    )
                    raise PushRefusedError(
                        f"push refused: {branch!r} already exists on GitHub "
                        f"at {remote[:12]}, which ctrlrelay did not push; "
                        "not overwriting it"
                    )
                expected_remote_sha = remote
            try:
                await self._run_git(
                    *git(
                        "push", "--no-verify", "--porcelain",
                        f"--force-with-lease=refs/heads/{branch}:"
                        f"{expected_remote_sha}",
                        url, f"{sha}:refs/heads/{branch}",
                    ),
                    env=env,
                    timeout=_GIT_TRANSFER_TIMEOUT_SECONDS,
                )
            except GitCommandError as exc:
                # The remote's reason for refusing the ref is on
                # stdout, which the generic message now carries but
                # buries after git's summary line. Pull it out so the
                # reason leads, and hand it up as its own field (#269).
                reason = parse_push_rejection(exc.stdout)
                if reason is None:
                    raise
                from ctrlrelay.core.redact import redact_secrets, tail

                said = tail(redact_secrets("; ".join(
                    line.strip()
                    for line in exc.stderr.splitlines()
                    if line.strip().startswith("remote:")
                )), _GIT_ERROR_TAIL_CHARS)
                _logger.error(
                    "publish.push.rejected",
                    extra={
                        "repo": repo,
                        "session_id": session_id,
                        "branch": branch,
                        "sha": sha,
                        "reason": reason,
                    },
                )
                # The reason leads; git's own failure text follows, as the
                # message carried it before, for anything matching on it.
                raise PushRejectedError(
                    f"git failed: push rejected by {url}: {reason}"
                    + (f"; {said}" if said else "")
                    # Already scrubbed where it was built; again here, so
                    # this line does not depend on that.
                    + f"; {redact_secrets(str(exc))}",
                    reason=reason,
                ) from exc
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        log_event(
            _logger,
            "publish.push.done",
            repo=repo,
            session_id=session_id,
            branch=branch,
            sha=sha,
            commits_ahead=ahead,
        )
        return sha

    async def ensure_bare_repo(self, repo: str) -> Path:
        """Ensure bare repo exists, cloning if needed.

        Uses an explicit refspec on fetch instead of relying on
        ``remote.origin.fetch`` config. Some bare clones in the wild
        have the refspec missing or empty; ``git fetch --all`` is a
        silent no-op in that case, so repeated calls never update
        ``refs/heads/*``. Worktrees created from that stale bare
        then check out commits days or weeks behind origin —
        exactly what we saw today when a task-pipeline run reported
        test counts from a commit that was two weeks old.

        Refspec: ``refs/heads/*:refs/heads/*`` (no leading ``+``).
        Fast-forward-only on purpose: a force update would clobber
        the dev pipeline's ``create_worktree_with_new_branch`` reuse
        path, which keeps local-ahead-of-origin commits when a prior
        session pushed partial work. Non-force semantics give us the
        right behavior in all three cases:

        - Local branch BEHIND origin (default-branch case, the bug
          we're fixing): fast-forward applies, local catches up.
        - Local branch AHEAD of origin (unpushed dev work): fetch
          refuses to rewind, local stays as-is, dev's reuse logic
          gets to inspect divergence and decide.
        - Local and origin equal: no-op.

        Two fetches, not one, and only the first prunes (#243).
        ``--prune`` is per command and scoped to that command's
        refspecs, so ``--prune`` paired with ``refs/heads/*`` deletes
        every local branch origin does not have — measured on 31 Oct,
        a branch made in the clone went with
        ``- [deleted]  (none)  -> held``. A blocked session whose
        branch was never pushed lost it the next time any run touched
        that repo, and its commits were then reachable only from the
        worktree's HEAD, one ``git gc`` from gone. So:

        - ``dependabot/*`` with a force refspec **and** ``--prune``.
          Dependabot rebases its branches, which rewrites their tips,
          so under a plain refspec every rebase is a rejection — a
          secops sweep died on ``AInvirion/zzsites`` that way with
          ``main`` already updated, and twelve bare repos were holding
          such a ref. These branches are bot-authored, never carry
          local work, and are the ones that actually pile up, so this
          is where pruning belongs.
        - Everything else with the plain refspec and **no** ``--prune``,
          with ``dependabot/*`` excluded by a negative refspec so the
          two never overlap. A branch origin deleted therefore lingers
          here; that is the trade, and the branch-reuse path in
          :meth:`create_worktree_with_new_branch` already has to
          recognise a local-only ref and decide whether its commits
          are already merged.

        Any rejection that survives either fetch is tolerated rather
        than raised. Git exits 1 on a declined rewind even when every
        other ref applied, and that exit code was aborting the whole
        session for a branch it had correctly protected.

        The default branch is the exception: tolerating a rejection
        there would swap a loud failure for work done quietly on a stale
        tree, since sessions are cut from it. So the clone is set aside
        and cloned again instead (#234): the usual cause is the repo on
        GitHub being replaced or its default branch force-pushed, and
        before this every run on the repo failed until somebody moved
        the clone by hand. See :meth:`_set_aside_and_reclone` for when
        that is refused.
        """
        bare_path = self._get_bare_repo_path(repo)

        if bare_path.exists():
            try:
                default_branch = await self.get_default_branch(repo)
            except WorktreeError:
                # A bare repo with no symbolic HEAD cannot name a default
                # branch to protect. create_worktree fails on the same
                # lookup, so the stale-tree risk never reaches a session.
                default_branch = ""
            # Branches a worktree has checked out are left out of the
            # fetch (#253). Git refuses to fetch into a checked-out
            # branch whenever the remote has a branch of that name, even
            # one that would not change (measured on git 2.43), and it
            # refuses the WHOLE fetch with it, default branch included.
            # Once a dev run's branch is published, every fetch during
            # that session failed: a run whose pull request went BEHIND
            # died in its own fix round. The session's worktree is the
            # authority for its branch until it ends.
            #
            # The default branch is never left out: sessions are cut from
            # it, so a stale one is worse than a loud failure. With no
            # default branch known nothing is left out, for the same
            # reason: it could be one of the checked-out branches.
            skip = [
                f"^refs/heads/{branch}"
                for branch in await self._checked_out_branches(bare_path)
                if branch != default_branch
            ] if default_branch else []
            # The bot branches: force refspec, and the only --prune.
            # `skip` applies here too — git refuses the WHOLE fetch over
            # a checked-out branch, and a secops run can have a
            # dependabot branch checked out. Excluding one also spares
            # it the prune, which is the answer we want anyway.
            await self._run_git(
                "fetch", "--prune", "origin",
                _BOT_BRANCH_REFSPEC,
                *skip,
                cwd=bare_path,
                timeout=_GIT_TRANSFER_TIMEOUT_SECONDS,
                tolerate_non_fast_forward=True,
            )
            # Every other branch: no --prune, so a local-only branch
            # survives (#243).
            try:
                await self._run_git(
                    "fetch", "origin",
                    "refs/heads/*:refs/heads/*",
                    _NOT_BOT_BRANCHES_REFSPEC,
                    *skip,
                    cwd=bare_path,
                    timeout=_GIT_TRANSFER_TIMEOUT_SECONDS,
                    tolerate_non_fast_forward=True,
                    protected_refs=(
                        frozenset({default_branch}) if default_branch
                        else frozenset()
                    ),
                )
            except DefaultBranchDivergedError as e:
                await self._set_aside_and_reclone(repo, bare_path, e)
            else:
                await self._follow_origin_default(repo, bare_path, default_branch)
        else:
            await self._clone_bare(repo, bare_path)

        return bare_path

    async def fetch_pr_head(self, repo: str, pr_number: int) -> str:
        """Fetch a pull request's current head into the bare clone and
        return its commit id (#222).

        Into ``refs/ctrlrelay/review/pr-<n>``, never into the branch: a
        run's branch is checked out in its worktree for the whole session,
        and :meth:`ensure_bare_repo` leaves such branches out of its fetch
        (#253), so a commit pushed by someone else never reached the clone
        and the review copy of it failed.
        """
        if not isinstance(pr_number, int) or pr_number <= 0:
            raise WorktreeError(f"not a pull request number: {pr_number!r}")
        bare_path = self._get_bare_repo_path(repo)
        ref = f"refs/ctrlrelay/review/pr-{pr_number}"
        await self._run_git(
            "fetch", "--no-tags", "origin",
            f"+refs/pull/{pr_number}/head:{ref}",
            cwd=bare_path,
            timeout=_GIT_TRANSFER_TIMEOUT_SECONDS,
        )
        sha = (
            await self._run_git(
                "rev-parse", "--verify", "--quiet", "--end-of-options",
                f"{ref}^{{commit}}",
                cwd=bare_path,
            )
        ).strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise WorktreeError(f"{ref} is not a commit: {sha[:80]!r}")
        return sha

    async def _follow_origin_default(
        self, repo: str, bare_path: Path, default_branch: str,
    ) -> None:
        """Point the clone's HEAD at origin's default branch when origin
        changed it. The fetch no longer prunes branches (#243), so a
        renamed default's old branch stays here, and without this the
        clone would keep cutting sessions from it. HEAD moves only when
        the new default is here at the commit origin reports, so a stale
        or diverged local branch is never adopted; anything else is left
        for the next update and logged, never silent."""
        try:
            out = await self._run_git(
                "ls-remote", "--symref", "origin", "HEAD",
                cwd=bare_path, timeout=_GIT_TRANSFER_TIMEOUT_SECONDS,
            )
        except WorktreeError as exc:
            log_event(
                _logger, "worktree.default_branch_unchecked", repo=repo,
                reason=str(exc)[:200],
            )
            return
        prefix, suffix = "ref: refs/heads/", "\tHEAD"
        new = next(
            (line[len(prefix):-len(suffix)] for line in out.splitlines()
             if line.startswith(prefix) and line.endswith(suffix)),
            "",
        )
        remote_sha = next(
            (line.split("\t", 1)[0] for line in out.splitlines()
             if line.endswith("\tHEAD") and not line.startswith("ref: ")),
            "",
        )
        if not new or new == default_branch:
            return
        try:
            local_sha = (await self._run_git(
                "rev-parse", "--verify", "--quiet", f"refs/heads/{new}",
                cwd=bare_path,
            )).strip()
        except WorktreeError:
            local_sha = ""
        if not remote_sha or local_sha != remote_sha:
            log_event(
                _logger, "worktree.default_branch_not_followed", repo=repo,
                old=default_branch or None, new=new,
                reason="not here yet" if not local_sha else "not current",
            )
            return
        try:
            await self._run_git(
                "symbolic-ref", "HEAD", f"refs/heads/{new}", cwd=bare_path,
            )
        except WorktreeError:
            return
        log_event(
            _logger, "worktree.default_branch_changed", repo=repo,
            old=default_branch or None, new=new,
        )

    async def _checked_out_branches(self, bare_path: Path) -> list[str]:
        """Branches checked out in this clone's linked worktrees."""
        listing = await self._run_git(
            "worktree", "list", "--porcelain", cwd=bare_path,
        )
        prefix = "branch refs/heads/"
        return [
            line[len(prefix):]
            for line in listing.splitlines()
            if line.startswith(prefix)
        ]

    def _clone_url(self, repo: str) -> str:
        """Where a bare clone comes from. Never the old clone's
        ``remote.origin.url``: that config is shared with every worktree
        cut from the clone, so an agent can rewrite it."""
        return f"https://github.com/{repo}.git"

    async def _clone_bare(self, repo: str, bare_path: Path) -> None:
        await self._run_git(
            "clone", "--bare", self._clone_url(repo), str(bare_path),
            timeout=_GIT_TRANSFER_TIMEOUT_SECONDS,
        )

    async def _set_aside_and_reclone(
        self, repo: str, bare_path: Path, cause: DefaultBranchDivergedError,
    ) -> None:
        """Move a clone whose default branch diverged from origin to
        ``<name>.git.replaced-<UTC time>`` and clone fresh (#234).

        The old clone is never deleted here, and the operator is told
        where it went so they can delete it. It holds what the failed
        fetch left: the bot-branch fetch that ran before it prunes
        ``dependabot/*`` refs origin no longer has. Branches outside
        that namespace are not pruned by any fetch here (#243).

        Refused, re-raising ``cause``, while any worktree is registered
        in the clone: a BLOCKED session keeps its worktree for the
        resume, and moving the clone would cut that worktree off from
        its repository. No prune first, because a prune also drops the
        registration of a worktree whose directory is only briefly
        missing. If the fresh clone fails, the partial one is removed
        and the old clone put back, so the next run sees the same state
        and tries again.

        The check and the move are not atomic on their own. They rely on
        the per-repo lock: every caller of ``ensure_bare_repo`` (the dev,
        task and secops runs, their resumes, and the dev fix round)
        holds it, and so does everything that adds a worktree.
        """
        listing = await self._run_git(
            "worktree", "list", "--porcelain", cwd=bare_path,
        )
        linked = [
            line[len("worktree "):]
            for line in listing.splitlines()[1:]
            if line.startswith("worktree ")
        ]
        if linked:
            log_event(
                _logger, "worktree.bare.reclone_refused",
                repo=repo, worktrees=len(linked),
            )
            raise DefaultBranchDivergedError(
                f"{cause}; not re-cloning while {len(linked)} worktree(s) "
                f"still use it: {', '.join(linked)}"
            ) from cause

        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        aside = bare_path.with_name(f"{bare_path.name}.replaced-{stamp}")
        bare_path.rename(aside)
        try:
            await self._clone_bare(repo, bare_path)
        except BaseException:
            shutil.rmtree(bare_path, ignore_errors=True)
            aside.rename(bare_path)
            raise
        log_event(
            _logger, "worktree.bare.recloned",
            repo=repo, set_aside=str(aside),
        )
        if self.on_reclone is not None:
            try:
                await self.on_reclone(repo, aside)
            except Exception as e:
                log_event(
                    _logger, "worktree.bare.reclone_notice_failed",
                    repo=repo, reason=type(e).__name__,
                )

    async def make_review_copy(
        self, repo: str, sha: str, *, base_branch: str, base_sha: str,
    ) -> Path:
        """A fresh clone of the bare repo checked out at ``sha``, for the
        reviewer (#263).

        The reviewer used to run in the agent's own worktree, after the
        agent had exited. Anything the agent left there that git follows
        was then followed by a process outside the agent sandbox. The
        measured case: a nested repository (a gitlink plus its own
        ``.git``) whose config sets ``core.fsmonitor`` or a clean filter.
        A plain ``git status`` or ``git diff`` in the worktree recurses
        into it, reads that config and runs the program it names.

        A clone carries only committed content. A commit cannot hold a
        ``.git`` path, so no nested repository or planted config can
        come across, and a gitlink checks out as an empty directory.

        The clone is made in a new directory from ``mkdtemp`` (0700,
        created by this call) under :meth:`_review_root`, beside the
        bare clones and not in the system temp dir (which can be a small
        tmpfs), so nothing can be planted there first. Its ``origin`` is
        set to the bare clone's own ``origin`` URL, so a reviewer that
        asks the forge about the branch finds the same remote it did in
        the worktree.
        It reads the bare repo and writes nothing to it, so it needs no
        repo lock: the review can run while the lock is released for the
        CI wait.

        Filter and diff drivers from the operator's own config are
        switched off in the copy for the same reason (see below).

        The base the reviewer diffs against is pinned too.
        ``base_branch`` and ``base_sha`` come from GitHub, not from the
        bare clone, whose ``HEAD`` and branch refs the agent can write:
        moving ``main`` onto its own earlier commit, or ``HEAD`` onto its
        own branch, would narrow the diff to what it chose. In the copy,
        ``refs/heads/<base_branch>`` and ``refs/remotes/origin/<base_branch>``
        are both set to ``base_sha``, which is fetched from GitHub if the
        clone does not have it.

        This covers what the reviewer finds on disk. It does not stop
        the reviewer running the committed code itself (see
        ``CodeReviewConfig.cli_command``), and it is not on the path of
        the agent's own git calls, which run inside its sandbox.
        """
        if not _HEX_SHA.match(sha):
            raise WorktreeError(f"review copy refused: {sha!r} is not a commit id")
        if not _HEX_SHA.match(base_sha):
            raise WorktreeError(
                f"review copy refused: base {base_sha!r} is not a commit id"
            )
        if not _is_safe_branch(base_branch):
            raise WorktreeError(
                f"review copy refused: {base_branch!r} is not a safe branch name"
            )
        bare_path = self._get_bare_repo_path(repo)
        if not bare_path.exists():
            raise WorktreeError(f"review copy: no clone of {repo} at {bare_path}")
        root = self._review_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        holder = Path(tempfile.mkdtemp(prefix="ctrlrelay-review-", dir=root))
        dest = holder / "repo"
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_TERMINAL_PROMPT"] = "0"
        flags = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
        try:
            await self._run_git(
                *flags, "clone", "--quiet", "--no-checkout", "--shared",
                "--", str(bare_path), str(dest), env=env,
            )
            # Before the checkout: a committed `.gitattributes` can point
            # a file at any filter or diff driver the operator's own
            # config defines (git-lfs, a textconv), which would then run
            # on agent-written content here, outside the sandbox. This
            # file outranks every `.gitattributes` in the tree, and `!`
            # puts both back to unspecified: no driver, and git's own
            # binary detection. The reviewer's diffs read it too.
            info = dest / ".git" / "info"
            info.mkdir(parents=True, exist_ok=True)
            (info / "attributes").write_text("* !filter !diff\n")
            await self._run_git(
                *flags, "-C", str(dest), "checkout", "--quiet", "--detach",
                sha, env=env,
            )
            head = (await self._run_git(
                "-C", str(dest), "rev-parse", "HEAD", env=env,
            )).strip()
            try:
                origin = (await self._run_git(
                    "config", "--get", "remote.origin.url",
                    cwd=bare_path, env=env,
                )).strip()
            except WorktreeError:
                origin = ""
            if origin:
                await self._run_git(
                    "-C", str(dest), "remote", "set-url", "origin", origin,
                    env=env,
                )
            await self._pin_review_base(
                repo, dest, base_branch, base_sha, env=env, flags=flags,
            )
            if head != sha:
                raise WorktreeError(
                    f"review copy is at {head[:12]}, expected {sha[:12]}"
                )
        except BaseException:
            shutil.rmtree(holder, ignore_errors=True)
            raise
        return dest

    async def _pin_review_base(
        self,
        repo: str,
        dest: Path,
        base_branch: str,
        base_sha: str,
        *,
        env: dict[str, str],
        flags: tuple[str, ...],
    ) -> None:
        """Point the copy's ``base_branch`` (local and ``origin/``) at
        ``base_sha``, whatever the bare clone said (#263)."""
        commit = f"{base_sha}^{{commit}}"

        async def have() -> bool:
            try:
                await self._run_git(
                    "-C", str(dest), "cat-file", "-e", commit, env=env,
                )
                return True
            except WorktreeError:
                return False

        if not await have():
            # Into the copy's own object store, from GitHub by URL: the
            # clone's `remote.origin.url` is shared with the agent.
            await self._run_git(
                *flags, "-C", str(dest), "fetch", "--quiet", "--no-tags",
                "--no-write-fetch-head", self._clone_url(repo), base_sha,
                env=env, timeout=_GIT_TRANSFER_TIMEOUT_SECONDS,
            )
            if not await have():
                raise WorktreeError(
                    f"review copy: base {base_sha[:12]} is not a commit here"
                )
        for ref in (
            f"refs/heads/{base_branch}",
            f"refs/remotes/origin/{base_branch}",
        ):
            await self._run_git(
                "-C", str(dest), "update-ref", "--no-deref", ref, base_sha,
                env=env,
            )
        await self._run_git(
            "-C", str(dest), "symbolic-ref", "refs/remotes/origin/HEAD",
            f"refs/remotes/origin/{base_branch}", env=env,
        )
        for key, value in (
            ("remote", "origin"), ("merge", f"refs/heads/{base_branch}"),
        ):
            await self._run_git(
                "-C", str(dest), "config", f"branch.{base_branch}.{key}",
                value, env=env,
            )

    def _review_root(self) -> Path:
        """Where review copies are made: beside the bare clones."""
        return self.bare_repos_dir.parent / "review"

    async def remove_review_copy(self, path: Path) -> None:
        """Delete a copy made by :meth:`make_review_copy`.

        Raises rather than returning quietly, so the caller's log line
        fires and a copy that is left behind is visible: a path that is
        not one of these copies is refused (a wrong argument cannot
        delete something else), and a failed delete propagates.
        """
        holder = Path(path).parent
        if not (
            Path(path).name == "repo"
            and holder.name.startswith("ctrlrelay-review-")
            and holder.parent == self._review_root()
        ):
            raise WorktreeError(f"not a review copy: {path}")
        shutil.rmtree(holder)

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
        filenames: Sequence[str] = ("CLAUDE.md",),
    ) -> list[str]:
        """Link the relay's repo context under every instruction filename.

        Agents disagree on the name: Claude reads ``CLAUDE.md``, Codex reads
        ``AGENTS.md``, and Copilot can read ``.github/copilot-instructions.md``.
        The selected adapter supplies the names it actually reads.

        A name the repository already has is left exactly as it is: its own
        instructions win over the relay's under that name, and the checkout
        stays clean so nothing of the relay's can reach a commit. The relay
        context still goes under every name that is free. Returns the names
        that were linked.
        """
        destination = str(context_path.resolve())
        self._context_targets[Path(worktree_path)] = destination
        linked: list[str] = []
        for name in filenames:
            with _context_parent(worktree_path, name, create=True) as (fd, base):
                if _link_points_to(fd, base, destination):
                    # Ours, from an earlier round on a reused worktree.
                    os.unlink(base, dir_fd=fd)
                else:
                    try:
                        os.stat(base, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        log_event(
                            _logger, "worktree.context.kept_repo_file",
                            worktree=str(worktree_path), context_name=name,
                        )
                        continue
                os.symlink(destination, base, dir_fd=fd)
            linked.append(name)

        if not linked:
            return linked
        try:
            self._add_context_exclude(worktree_path, linked)
        except (_UnsafeExcludeError, OSError, ValueError) as e:
            # Refused, not followed, and never raised out of a resume: the
            # line only keeps the link out of `git status`, so the run
            # carries on without it. ValueError covers a back-pointer
            # that is not UTF-8.
            log_event(
                _logger, "worktree.context.exclude_refused",
                worktree=str(worktree_path),
                reason=f"{type(e).__name__}: {e}"[:300],
            )
        return linked

    def _add_context_exclude(
        self, worktree_path: Path, filenames: Sequence[str] = ("CLAUDE.md",),
    ) -> None:
        """Add the context links to the worktree's ``info/exclude``, found
        from the relay's own data only (#263).

        The worktree's ``.git`` file is never read: the agent can rewrite
        it when there is no sandbox. Git names a worktree's directory
        after the basename of the path the relay gave ``worktree add``,
        with a number appended on a collision. So the candidates are
        ``<bare_repos_dir>/<clone>.git/worktrees/<basename>[N]``, each
        opened from ``bare_repos_dir`` down without following a link, and
        the one used is the single candidate whose own ``gitdir``
        back-pointer resolves to this worktree's ``.git``. None, or more
        than one, raises :class:`_UnsafeExcludeError`.
        """
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        dflags = os.O_RDONLY | os.O_DIRECTORY | nofollow
        base = worktree_path.name
        want = os.path.realpath(worktree_path / ".git")

        def is_candidate(name: str) -> bool:
            return name == base or (
                name.startswith(base) and name[len(base):].isdigit()
            )

        def points_here(admin_fd: int, admin: Path) -> bool:
            try:
                pfd = os.open("gitdir", os.O_RDONLY | nofollow, dir_fd=admin_fd)
            except OSError:
                return False
            with os.fdopen(pfd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    return False
                # Strict: a pointer that is not UTF-8 raises, and the
                # caller logs it as a refusal.
                pointer = f.read().decode("utf-8").strip()
            if not os.path.isabs(pointer):
                pointer = os.path.join(admin, pointer)
            return os.path.realpath(pointer) == want

        root_fd = os.open(self.bare_repos_dir, os.O_RDONLY | os.O_DIRECTORY)
        matches: list[tuple[int, Path]] = []
        try:
            for clone in sorted(os.listdir(root_fd)):
                if not clone.endswith(".git"):
                    continue
                try:
                    clone_fd = os.open(clone, dflags, dir_fd=root_fd)
                except OSError:
                    continue
                try:
                    try:
                        wts_fd = os.open("worktrees", dflags, dir_fd=clone_fd)
                    except OSError:
                        continue
                    try:
                        for name in sorted(os.listdir(wts_fd)):
                            if not is_candidate(name):
                                continue
                            try:
                                admin_fd = os.open(name, dflags, dir_fd=wts_fd)
                            except OSError:
                                continue
                            admin = self.bare_repos_dir / clone / "worktrees" / name
                            if points_here(admin_fd, admin):
                                matches.append((admin_fd, admin))
                            else:
                                os.close(admin_fd)
                    finally:
                        os.close(wts_fd)
                finally:
                    os.close(clone_fd)
            if len(matches) != 1:
                raise _UnsafeExcludeError(
                    f"{len(matches)} git directories in {self.bare_repos_dir} "
                    f"point back at {worktree_path}"
                )
            admin_fd, admin = matches[0]
            for name in filenames:
                _add_exclude_line(admin_fd, admin, Path(name).as_posix())
        finally:
            os.close(root_fd)
            for fd, _ in matches:
                os.close(fd)

    def remove_context_symlink(
        self,
        worktree_path: Path,
        filenames: Sequence[str] = ("CLAUDE.md",),
    ) -> None:
        """Remove the context links before git operations.

        Only links pointing at the context file this manager linked into
        ``worktree_path`` are removed; a repository's own file or link under
        the same name is part of the checkout and stays.
        """
        key = Path(worktree_path)
        destination = self._context_targets.get(key)
        if destination is None:
            return
        complete = True
        for name in filenames:
            try:
                with _context_parent(
                    worktree_path, name, create=False,
                ) as (fd, base):
                    if _link_points_to(fd, base, destination):
                        os.unlink(base, dir_fd=fd)
            except FileNotFoundError:
                # No parent directory: nothing of ours can be there.
                continue
            except (OSError, ValueError):
                # A parent the agent replaced or locked. Nothing outside
                # the checkout is touched, and the target is kept so a
                # later cleanup of this worktree can still finish.
                complete = False
        if complete:
            del self._context_targets[key]
