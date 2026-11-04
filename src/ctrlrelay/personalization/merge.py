"""Merge fleet-owned keys from a synced JSON file into a real live file.

For a file an app rewrites in place (``~/.claude/settings.json``) a
symlink into the checkout means every app write lands in a tracked file,
dirties the tree and blocks the next pull (#287). In ``mode: merge`` the
live file stays a real file on this machine and a pull writes into it
only the top-level keys the local config names in ``merge_keys``.

The guards, each of which has a test that fails without it:

* The key list is an argument from the local config. Nothing read from
  the synced file can add a key to it.
* Both files must parse as a JSON object with no duplicate keys, or the
  live file is left alone.
* Directories are walked one component at a time with
  ``O_DIRECTORY | O_NOFOLLOW`` from ``$HOME`` (or ``/`` for a path
  outside it), and the file is read, the temp file created and the
  rename done relative to that directory fd. So a symlink at the file
  or at any directory below the anchor is refused, and a directory
  swapped after the walk cannot redirect the write. The files must be
  regular and owned by the current user. The one exception is
  :func:`migrate_link`, which an operator runs explicitly to replace the
  old link into this checkout with a real file.
* Writes go to a temp file in the same directory, are fsynced and then
  renamed over the live path, mode 0600 or the existing mode if stricter.
  Just before the rename the live path is checked to still be the file
  that was read (device, inode, size, mtime). If it changed, the merge
  is redone once from the new content, then refused.
* Logs and summaries carry key names, never values: a value can be a
  secret (``env``).
"""

from __future__ import annotations

import errno
import json
import logging
import os
import secrets
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("ctrlrelay.personalization")

# Keys that can run code or loosen permissions. A pull that changes one
# of them logs at warning, so a surprise change upstream is visible.
SENSITIVE_KEYS = frozenset({
    "hooks",
    "env",
    "permissions",
    "apiKeyHelper",
    "skipDangerousModePermissionPrompt",
})

# Bound on what is read: a settings file is a few KiB. Anything bigger
# is not one, and reading it unbounded would let a planted file eat RAM.
MAX_BYTES = 1 << 20

# Bound on nesting. A settings file is a few levels deep. The C parser
# accepts far deeper input (about 10,000 levels on 3.12 and, with its
# stack-based limit, about 65,000 on 3.14), and on 3.14 writing that out
# with ``json.dumps(indent=2)`` grew past 9 GB instead of raising
# RecursionError. So depth is checked right after parsing, before
# anything compares or serialises the value.
MAX_DEPTH = 64

MIGRATE_HINT = "run `ctrlrelay personalization migrate` once to make it a real file"

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC


class MergeRefusedError(Exception):
    """The merge would be unsafe; nothing was written. The message
    names paths, keys and reasons, never file contents."""


class _LiveChangedError(Exception):
    """The live path is no longer what was read; nothing was renamed."""


@dataclass(frozen=True)
class MergeOutcome:
    """What one merge-mode entry did, for summaries and status."""

    label: str
    # "wrote" | "in-sync" | "skipped-source-missing" | "refused"
    # | "migrated" | "nothing-to-migrate"
    action: str
    keys: tuple[str, ...] = ()
    detail: str = ""

    @property
    def refused(self) -> bool:
        return self.action == "refused"

    def describe(self) -> str:
        if self.action == "wrote":
            text = f"{self.label}: wrote {', '.join(self.keys)}"
        elif self.action == "refused":
            text = f"{self.label}: REFUSED"
        else:
            text = f"{self.label}: {self.action}"
        return f"{text} ({self.detail})" if self.detail else text


def _canonical(value: object) -> str:
    # ``1 == True`` and ``1 == 1.0`` in Python; JSON tells them apart,
    # so compare serialised forms.
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


# ----- directories ------------------------------------------------------------


def open_dir_nofollow(path: Path, *, create: bool = False) -> int:
    """Return an fd for directory ``path`` such that no component below
    ``$HOME`` is a symlink.

    The walk starts at the RESOLVED ``$HOME`` (so a home reached through
    an alias, such as macOS's ``/var`` -> ``/private/var``, works) and
    ``path`` may be spelled under either form of it. Each later
    component is opened relative to the previous fd with
    ``O_NOFOLLOW``. A path outside ``$HOME`` is refused. ``create``
    makes missing components (mode 0700). Raises ``FileNotFoundError``
    for a missing component when not creating, :class:`MergeRefusedError`
    otherwise.
    """
    path = Path(os.path.abspath(path))
    home_real = Path(os.path.realpath(Path.home()))
    home_abs = Path(os.path.abspath(Path.home()))
    for base in (home_real, home_abs):
        if path == base or base in path.parents:
            parts = path.relative_to(base).parts
            break
    else:
        raise MergeRefusedError(
            f"{path} is not under $HOME ({home_real}); mode: merge only reads "
            "and writes inside the home directory, so the live file and the "
            "personalization checkout must both be under it"
        )
    fd = os.open(home_real, _DIR_FLAGS)
    try:
        for part in parts:
            flags = _DIR_FLAGS | os.O_NOFOLLOW
            try:
                nfd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                nfd = os.open(part, flags, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise MergeRefusedError(
                        f"{path}: component {part!r} is a symbolic link or "
                        "not a directory"
                    ) from None
                raise MergeRefusedError(
                    f"cannot open directory {path}: {e.strerror}"
                ) from None
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def resolved_parent(path: Path) -> Path:
    """``path`` with its directory resolved and its last component kept
    as is (the last component is guarded by ``O_NOFOLLOW`` instead)."""
    return Path(os.path.realpath(path.parent)) / path.name


# ----- reading ----------------------------------------------------------------


def _read_at(
    dfd: int, name: str, path: Path, *, what: str
) -> tuple[bytes, os.stat_result]:
    """Read ``name`` in directory ``dfd`` without following a link.

    Raises ``FileNotFoundError`` if it does not exist, and
    :class:`MergeRefusedError` for a link, a non-regular file, a file
    owned by someone else or an oversized file. ``O_NONBLOCK`` keeps a
    planted FIFO from hanging the open.
    """
    flags = (
        os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(name, flags, dir_fd=dfd)
    except FileNotFoundError:
        raise
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise MergeRefusedError(f"{what} {path} is a symbolic link") from None
        raise MergeRefusedError(
            f"cannot open {what} {path}: {e.strerror}"
        ) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise MergeRefusedError(f"{what} {path} is not a regular file")
        if st.st_uid != os.getuid():
            raise MergeRefusedError(
                f"{what} {path} is owned by uid {st.st_uid}, not {os.getuid()}"
            )
        if st.st_size > MAX_BYTES:
            raise MergeRefusedError(
                f"{what} {path} is {st.st_size} bytes, over the {MAX_BYTES} limit"
            )
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, 65536):
            total += len(chunk)
            if total > MAX_BYTES:
                raise MergeRefusedError(f"{what} {path} grew past {MAX_BYTES} bytes")
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks), st


def _no_duplicates(path: Path, what: str):
    def hook(pairs: list[tuple[str, object]]) -> dict:
        obj: dict = {}
        for key, value in pairs:
            if key in obj:
                raise MergeRefusedError(
                    f"{what} {path} has duplicate key {key!r}"
                )
            obj[key] = value
        return obj

    return hook


def _parse_object(
    raw: bytes, path: Path, *, what: str, allow_empty: bool
) -> tuple[dict, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MergeRefusedError(f"{what} {path} is not UTF-8") from None
    if allow_empty and not text.strip():
        return {}, text
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicates(path, what))
    except json.JSONDecodeError as e:
        # Message and position only: ``e.doc`` holds the whole file and
        # must never reach a log.
        raise MergeRefusedError(
            f"{what} {path} is not valid JSON ({e.msg}, line {e.lineno} "
            f"column {e.colno})"
        ) from None
    except RecursionError:
        raise MergeRefusedError(f"{what} {path} is nested too deeply") from None
    if not isinstance(obj, dict):
        raise MergeRefusedError(
            f"{what} {path} is a JSON {type(obj).__name__}, not an object"
        )
    if _deeper_than(obj, MAX_DEPTH):
        raise MergeRefusedError(
            f"{what} {path} is nested too deeply (over {MAX_DEPTH} levels)"
        )
    return obj, text


def _deeper_than(value: object, limit: int) -> bool:
    """Whether ``value`` nests containers more than ``limit`` levels deep.

    Iterative, so it cannot itself run out of stack on the input it is
    there to refuse. The top-level object is level 1.
    """
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, level = stack.pop()
        if isinstance(item, dict):
            children: Iterable[object] = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if level > limit:
            return True
        stack.extend((child, level + 1) for child in children)
    return False


def _load(
    path: Path, *, what: str, allow_empty: bool = False
) -> tuple[dict, str, os.stat_result]:
    dfd = open_dir_nofollow(path.parent)
    try:
        raw, st = _read_at(dfd, path.name, path, what=what)
    finally:
        os.close(dfd)
    obj, text = _parse_object(raw, path, what=what, allow_empty=allow_empty)
    return obj, text, st


def read_json_object(
    path: Path, *, what: str, allow_empty: bool = False
) -> tuple[dict, os.stat_result]:
    """Read ``path`` with every guard above and require a JSON object.
    ``allow_empty`` treats a blank file as ``{}``."""
    obj, _, st = _load(path, what=what, allow_empty=allow_empty)
    return obj, st


def is_link_into(target: Path, source: Path) -> bool:
    """True iff ``target`` is a symlink that resolves to ``source``."""
    try:
        if not stat.S_ISLNK(os.lstat(target).st_mode):
            return False
        return os.path.realpath(target) == os.path.realpath(source)
    except OSError:
        return False


# ----- writing ----------------------------------------------------------------


def _fingerprint(st: os.stat_result) -> tuple:
    return (
        st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode), st.st_size,
        st.st_mtime_ns,
    )


def _check_unchanged_at(
    dfd: int, name: str, expected: os.stat_result | None
) -> None:
    try:
        now = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        if expected is None:
            return
        raise _LiveChangedError from None
    if expected is None or _fingerprint(now) != _fingerprint(expected):
        raise _LiveChangedError


def _atomic_write_at(
    dfd: int, name: str, text: str, *, mode: int,
    expected: os.stat_result | None,
) -> None:
    """Write ``text`` to ``name`` in ``dfd`` via temp + fsync + rename.

    ``expected`` is the ``lstat`` of what was at ``name`` when it was
    read (``None`` if nothing was). If it changed since, raises
    :class:`_LiveChangedError` and nothing is renamed. On any failure
    the temp file is removed and ``name`` is as it was.
    """
    tmp = f".{name}.{secrets.token_hex(6)}.ctrlrelay-tmp"
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC,
        mode,
        dir_fd=dfd,
    )
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(text.encode("utf-8"))
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        _check_unchanged_at(dfd, name, expected)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        # Never let a failing cleanup replace the error that matters.
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    try:
        os.fsync(dfd)
    except OSError:
        pass


def _refuse(label: str, reason: str) -> MergeOutcome:
    logger.error("%s: merge refused, live file left untouched: %s", label, reason)
    return MergeOutcome(label=label, action="refused", detail=reason)


# ----- merge ------------------------------------------------------------------


def merge_into_live(
    source: Path, target: Path, keys: Iterable[str], *, label: str
) -> MergeOutcome:
    """Write the ``keys`` present in ``source`` into the live ``target``.

    Only listed keys are written. A listed key absent from ``source`` is
    left as it is on this machine (absent is not a deletion). Keys not
    listed are never read from ``source`` and never touched in
    ``target``.
    """
    owned = list(keys)
    try:
        upstream, _, _ = _load(source, what="synced file")
    except FileNotFoundError:
        return MergeOutcome(label=label, action="skipped-source-missing")
    except MergeRefusedError as e:
        return _refuse(label, str(e))
    except OSError as e:
        return _refuse(label, f"cannot read synced file {source}: {e.strerror}")

    if is_link_into(target, source):
        return _refuse(
            label,
            f"live file {target} is still a symlink into the personalization "
            f"checkout; {MIGRATE_HINT}",
        )
    try:
        dfd = open_dir_nofollow(target.parent, create=True)
    except MergeRefusedError as e:
        return _refuse(label, str(e))
    except OSError as e:
        return _refuse(label, f"cannot open {target.parent}: {e.strerror}")

    changed: list[str] = []
    try:
        for attempt in (1, 2):
            try:
                raw, live_st = _read_at(dfd, target.name, target, what="live file")
                live, _ = _parse_object(
                    raw, target, what="live file", allow_empty=True
                )
            except FileNotFoundError:
                live, live_st = {}, None
            changed = [
                k for k in owned
                if k in upstream
                and (k not in live or _canonical(live[k]) != _canonical(upstream[k]))
            ]
            if not changed:
                return MergeOutcome(label=label, action="in-sync")
            merged = dict(live)
            for k in changed:
                merged[k] = upstream[k]
            text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
            mode = 0o600 if live_st is None else stat.S_IMODE(live_st.st_mode) & 0o600
            try:
                _atomic_write_at(
                    dfd, target.name, text, mode=mode, expected=live_st
                )
                break
            except _LiveChangedError:
                if attempt == 2:
                    return _refuse(
                        label,
                        f"live file {target} changed during the merge twice; "
                        "try again",
                    )
                logger.info(
                    "%s: live file changed during the merge; redoing it once",
                    label,
                )
    except MergeRefusedError as e:
        return _refuse(label, str(e))
    except RecursionError:
        # Parsed, but too deep to compare or serialise (the pure-Python
        # ``json.dumps(indent=...)`` recurses far less deep than the C
        # parser).
        return _refuse(label, "a value is nested too deeply to merge")
    except OSError as e:
        return _refuse(label, f"write to {target} failed: {e.strerror}")
    finally:
        os.close(dfd)

    logger.info("%s: wrote %s", label, ", ".join(changed))
    for k in changed:
        if k in SENSITIVE_KEYS:
            logger.warning(
                "%s: pull changed %r, a key that can run code or loosen "
                "permissions; if this was not expected, check the synced "
                "file's history",
                label, k,
            )
    return MergeOutcome(label=label, action="wrote", keys=tuple(changed))


def migrate_link(source: Path, target: Path, *, label: str) -> MergeOutcome:
    """Replace the old symlink ``target -> source`` with a real file.

    Explicit, operator-run only; no pull path calls this. The new file
    gets the source's bytes as they are now (so whatever the app or the
    operator wrote through the link is kept on this machine), mode 0600
    or stricter. A link pointing anywhere else is refused and not
    followed.
    """
    try:
        dfd = open_dir_nofollow(target.parent)
    except FileNotFoundError:
        return MergeOutcome(
            label=label, action="nothing-to-migrate", detail="live path missing"
        )
    except MergeRefusedError as e:
        return _refuse(label, str(e))
    except OSError as e:
        return _refuse(label, f"cannot open {target.parent}: {e.strerror}")
    try:
        return _migrate_at(dfd, source, target, label=label)
    except OSError as e:
        return _refuse(label, f"migration of {target} failed: {e.strerror}")
    finally:
        os.close(dfd)


def _migrate_at(
    dfd: int, source: Path, target: Path, *, label: str
) -> MergeOutcome:
    try:
        link_st = os.stat(target.name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return MergeOutcome(
            label=label, action="nothing-to-migrate",
            detail="live path missing",
        )
    if not stat.S_ISLNK(link_st.st_mode):
        return MergeOutcome(
            label=label, action="nothing-to-migrate",
            detail="already a real file",
        )
    if not is_link_into(target, source):
        return _refuse(
            label,
            f"live file {target} is a symlink to somewhere other than "
            f"{source}; not migrating it",
        )
    try:
        _, text, src_st = _load(source, what="synced file")
    except FileNotFoundError:
        return _refuse(label, f"synced file {source} is missing")
    except MergeRefusedError as e:
        return _refuse(label, str(e))
    except OSError as e:
        return _refuse(label, f"cannot read synced file {source}: {e.strerror}")
    mode = stat.S_IMODE(src_st.st_mode) & 0o600
    try:
        _atomic_write_at(dfd, target.name, text, mode=mode, expected=link_st)
    except _LiveChangedError:
        return _refuse(label, f"live path {target} changed during the migration")
    except OSError as e:
        return _refuse(label, f"write to {target} failed: {e.strerror}")
    logger.info("%s: migrated %s from a symlink to a real file", label, target)
    return MergeOutcome(label=label, action="migrated")


def differing_keys(a: dict, b: dict) -> list[str]:
    """Top-level keys whose presence or value differs. Names only."""
    return sorted(
        k for k in set(a) | set(b)
        if k not in a or k not in b or _canonical(a[k]) != _canonical(b[k])
    )
