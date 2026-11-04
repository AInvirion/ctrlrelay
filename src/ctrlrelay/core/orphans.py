"""Runs whose process died without settling them (#312).

A session's ``sessions`` row is moved off ``running`` only by the process
running it. When that process dies first (the kernel's out-of-memory
killer, a ``systemctl stop`` that takes the whole unit, a power cut), the
row says ``running`` for ever: the status page lists it as running, the
issue stays in the poller's ``seen_issues`` so nothing looks at it again,
its claim reads as working, and a repo lock it held can wedge the repo.

:func:`reap_orphaned_sessions` runs once when the poller starts, before
the first poll, and settles each such row: failed, lock dropped, issue
handed back to the poller, claim released, worktree kept and named.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable

from ctrlrelay.core.claims import PROCESS_RUN
from ctrlrelay.core.obs import get_logger, log_event

_logger = get_logger("core.orphans")

# Pipelines whose issue goes back to the poller when their run died. A
# secops sweep has no issue; it runs again on its own schedule.
_ISSUE_PIPELINES = frozenset({"dev", "task"})


def _command_line(pid: int) -> list[str] | None:
    proc = Path("/proc")
    if proc.is_dir():
        try:
            raw = (proc / str(pid) / "cmdline").read_bytes()
        except OSError:
            return None
        return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    import subprocess
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.split()


def owner_is_alive(pid: int) -> bool:
    """Whether the process that owns a session may still be running it.

    Errs towards alive, because the mistake in that direction leaves a
    row as it is today, while the other one fails a live run under its
    own feet. So: a process that exists and runs ctrlrelay, or whose
    command line cannot be read, is alive. Gone, or reused by a program
    that is not ctrlrelay, is dead.

    This process's own pid is dead here. The caller has already kept
    every row this process wrote, by its run id (``owner_run``), so a row
    that names this pid with another run id was written by an earlier
    process whose pid was handed on to this one, as happens to pid 1 in a
    container on every restart.
    """
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by another user: not ours, but not provably
        # dead either.
        return True
    except OSError:
        return True
    args = _command_line(pid)
    if args is None:
        # Exited between the two checks, or unreadable. A vanished
        # process reads as `None` too, so look once more.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    # The console script, or `python -m ctrlrelay...`. Not a substring
    # test: the interpreter of a uv tool install lives under a directory
    # named ctrlrelay, so any Python program run with it would match.
    for i, arg in enumerate(args):
        if os.path.basename(arg) == "ctrlrelay":
            return True
        if (arg == "ctrlrelay" or arg.startswith("ctrlrelay.")) and (
            i > 0 and args[i - 1] == "-m"
        ):
            return True
        if arg == "-mctrlrelay" or arg.startswith("-mctrlrelay."):
            return True
    return False


def worktree_in_use(path: str) -> bool | None:
    """Whether a live process of this user has its working directory
    inside ``path``: ``True``, ``False``, or ``None`` when that cannot be
    told.

    The relay process can die alone (``kill -9``, the OOM killer choosing
    it) while the agent it started keeps running, because the agent is
    in the relay's process group, not under it. Such a run is not dead,
    and settling it would detach the worktree under a live agent and
    start a second run on the same issue. The agent works in its
    worktree, as the relay's own user, so such a process there is the
    sign. Processes of other users are not looked at: an agent never
    runs as another user, and their working directories are not
    readable anyway.
    """
    try:
        root = os.path.realpath(path)
    except OSError:
        return None
    prefix = root.rstrip(os.sep) + os.sep
    uid = os.getuid()
    proc = Path("/proc")
    if proc.is_dir():
        try:
            entries = [e for e in os.listdir(proc) if e.isdigit()]
        except OSError:
            return None
        # This process is of this user and has a working directory, so a
        # scan that read none did not work, whatever it returned.
        read_any = False
        for pid in entries:
            try:
                if os.stat(f"/proc/{pid}").st_uid != uid:
                    continue
                cwd = os.readlink(f"/proc/{pid}/cwd")
                read_any = True
            except OSError:
                # Exited while being looked at, or a process of ours that
                # made itself non-dumpable (systemd --user, ssh-agent).
                # An agent is neither: its working directory reads,
                # inside the bwrap sandbox too.
                continue
            if cwd == root or cwd.startswith(prefix):
                return True
        return False if read_any else None
    import shutil
    import subprocess
    lsof = shutil.which("lsof")
    if lsof is None:
        return None
    try:
        out = subprocess.run(
            [lsof, "-w", "-n", "-P", "-a", "-u", str(uid), "-d", "cwd",
             "-F", "n"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode not in (0, 1):
        return None
    cwds = [line[1:] for line in out.stdout.splitlines() if line.startswith("n")]
    if not cwds:
        # Not even this process's own: lsof did not answer.
        return None
    return any(cwd == root or cwd.startswith(prefix) for cwd in cwds)


def orphan_reason(row: dict[str, Any], branch: str | None) -> str:
    """The ``Reason:`` line of the issue comment for a dead run.

    Says only what was checked: where the files are when the worktree was
    found, and that the branch was freed only when it was.
    """
    where = row.get("worktree_path")
    if where and Path(where).exists():
        kept = (
            f"Its worktree is still on that machine at `{where}`"
            + (f", detached from `{branch}`" if branch else "")
            + ", and the relay deleted nothing in it."
        )
    elif where:
        kept = f"Its worktree `{where}` was not found on that machine."
    else:
        kept = "No worktree was recorded for it."
    return (
        "the relay process running it stopped before the run ended "
        "(for example the machine ran out of memory, or the service was "
        f"restarted). {kept}"
    )


async def _open_pr_for(github: Any, repo: str, branch: str | None) -> int | None:
    """The same-repo open pull request for ``branch``, or ``None`` when
    none was found: there is none, the branch is unknown (no worktree to
    read it from), or the lookup failed."""
    if not branch:
        return None
    try:
        prs = await github.list_prs(repo, state="open", head=branch, timeout=30)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_event(
            _logger, "orphans.pr_lookup_failed", repo=repo, branch=branch,
            reason=type(e).__name__, error=str(e)[:200],
        )
        return None
    owner, _, name = repo.partition("/")
    for pr in prs or []:
        head_owner = ((pr.get("headRepositoryOwner") or {}).get("login") or "")
        head_name = ((pr.get("headRepository") or {}).get("name") or "")
        if head_owner.lower() == owner.lower() and head_name.lower() == name.lower():
            return pr.get("number")
    return None


async def reap_orphaned_sessions(
    state_db: Any,
    *,
    poller: Any,
    github: Any,
    worktree: Any,
    is_alive: Callable[[int], bool] = owner_is_alive,
    in_use: Callable[[str], bool | None] = worktree_in_use,
) -> list[dict[str, Any]]:
    """Settle every ``running`` session whose owning process is gone.

    For each one:

    - the row becomes ``failed`` with a summary saying why, and any repo
      lock it held is dropped (one transaction, see
      :meth:`StateDB.fail_orphaned_session`);
    - its worktree, when there is one, is kept, and its HEAD detached
      from the issue's branch when that can be done safely, so the next
      run for the issue can check that branch out;
    - a dev or task issue is removed from ``seen_issues`` so the next
      poll takes it again, unless an open pull request was found for the
      branch: then the pull request is the outcome and the issue stays
      seen. When the lookup could not be made, the issue is handed back
      anyway; the next run refuses a branch that backs an open pull
      request on its own (``create_worktree_with_new_branch``);
    - a comment on the issue names the worktree, and releases this
      machine's claim by id where the row has one. A task run on a
      single machine claims nothing and gets no comment.

    Left alone: rows this process wrote (``owner_run``; a scheduled
    sweep can start before this runs), rows whose owner is alive, rows
    whose worktree a live process is still working in (the agent outlives
    a relay killed on its own; see :func:`worktree_in_use`), and rows with
    no ``owner_pid``. Those come from a version before #312 and name no
    process, and could as well be a ``ctrlrelay run dev`` started by hand
    and still running.

    The row update comes first, so no later step touches a row something
    else settled, and each step after it is best-effort. The cost is a
    narrow gap: if this process dies between the update and the
    hand-back, that row is no longer running and no later start looks at
    it again. Returns one dict per row settled.
    """
    from ctrlrelay.pipelines.dev import reconcile_claim_on_failure

    try:
        rows = state_db.list_running_sessions()
    except Exception as e:
        log_event(
            _logger, "orphans.list_failed",
            reason=type(e).__name__, error=str(e)[:200],
        )
        return []

    settled: list[dict[str, Any]] = []
    for row in rows:
        session_id = row["id"]
        pid = row.get("owner_pid")
        if row.get("owner_run") == PROCESS_RUN:
            continue
        if pid is None:
            log_event(
                _logger, "orphans.owner_unknown", session_id=session_id,
                repo=row.get("repo"), issue_number=row.get("issue_number"),
                operator_action=(
                    "this row was written before ctrlrelay recorded which "
                    "process runs a session; if no run is live, set its "
                    "status to failed in state.db by hand"
                ),
            )
            continue
        if is_alive(int(pid)):
            continue
        worktree_path = row.get("worktree_path")
        if worktree_path and Path(worktree_path).exists():
            busy = in_use(worktree_path)
            if busy is not False:
                # In use, or could not be told: errs towards alive, as
                # `owner_is_alive` does.
                log_event(
                    _logger,
                    "orphans.worktree_in_use" if busy
                    else "orphans.worktree_use_unknown",
                    session_id=session_id,
                    repo=row.get("repo"), worktree_path=worktree_path,
                    operator_action=(
                        "the relay process that ran this session is gone, "
                        "but a process may still be working in its "
                        "worktree, so it is left as running. Stop that "
                        "process (or leave that directory) and restart the "
                        "poller to settle it."
                    ),
                )
                continue

        repo = row["repo"]
        issue_number = row.get("issue_number")
        pipeline = row.get("pipeline")

        summary = "Run stopped: " + orphan_reason(row, None)
        try:
            changed = state_db.fail_orphaned_session(session_id, summary)
        except Exception as e:
            log_event(
                _logger, "orphans.update_failed", session_id=session_id,
                reason=type(e).__name__, error=str(e)[:200],
            )
            continue
        if not changed:
            continue

        # After the row, not before: a row something else settled in the
        # meantime is not ours to touch, and neither is its worktree.
        branch: str | None = None
        if worktree_path and Path(worktree_path).exists():
            try:
                branch = worktree.detach_orphaned_worktree(repo, worktree_path)
            except Exception as e:
                log_event(
                    _logger, "orphans.detach_failed", session_id=session_id,
                    reason=type(e).__name__, error=str(e)[:200],
                )

        pr_number: int | None = None
        handed_back = False
        if issue_number is not None and pipeline in _ISSUE_PIPELINES:
            pr_number = await _open_pr_for(github, repo, branch)
            if pr_number is None:
                poller.unmark_seen(repo, int(issue_number))
                handed_back = True
            claim_id = row.get("claim_id")
            # A dev run always claims before its row exists. A task run
            # claims only on a multi-machine setup, and posts nothing
            # otherwise, so there is nothing of its to retract then.
            if pipeline == "dev" or claim_id:
                await reconcile_claim_on_failure(
                    github,
                    repo=repo,
                    issue_number=int(issue_number),
                    session_id=session_id,
                    reason=orphan_reason(row, branch),
                    exit_code=row.get("claude_exit_code"),
                    pr_number=pr_number,
                    claim_id=claim_id,
                    will_retry=handed_back,
                    # Only open pull requests were looked for, so "none
                    # was opened" is never known: one may have been
                    # closed or merged.
                    pr_known=False,
                )

        log_event(
            _logger, "orphans.reaped", session_id=session_id, repo=repo,
            issue_number=issue_number, pipeline=pipeline, owner_pid=pid,
            worktree_path=worktree_path, branch_freed=branch,
            pr_number=pr_number, handed_back=handed_back,
        )
        settled.append({
            "session_id": session_id,
            "repo": repo,
            "issue_number": issue_number,
            "worktree_path": worktree_path,
            "branch_freed": branch,
            "pr_number": pr_number,
            "handed_back": handed_back,
        })
    return settled
