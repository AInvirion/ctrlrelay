"""Merge fleet-owned keys from a synced JSON file into a real live file.

For a file an app rewrites in place (``~/.claude/settings.json``) a
symlink into the checkout means every app write lands in a tracked file,
dirties the tree and blocks the next pull (#287). In ``mode: merge`` the
live file stays a real file on this machine and a pull writes into it
only the top-level keys the local config names in ``merge_keys``.

The guards, each of which has a test that fails without it:

* The key list is an argument from the local config. Nothing read from
  the synced file can add a key to it.
* Both files must parse as a JSON object, or the live file is left alone.
* Both files are opened with ``O_NOFOLLOW`` and must be regular files
  owned by the current user. A symlink at the live path is refused and
  its target is never opened; the one exception is :func:`migrate_link`,
  which an operator runs explicitly to replace the old link into this
  checkout with a real file.
* Writes go to a temp file in the same directory, are fsynced and then
  renamed over the live path, mode 0600 or the existing mode if stricter.
  Just before the rename the live path is checked to still be the file
  that was read, so a swap in between is refused rather than followed.
* Logs and summaries carry key names, never values: a value can be a
  secret (``env``).
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
import tempfile
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

MIGRATE_HINT = "run `ctrlrelay personalization migrate` once to make it a real file"


class MergeRefusedError(Exception):
    """The merge would be unsafe; nothing was written. The message
    names paths and reasons, never file contents."""


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


def _read_regular(path: Path, *, what: str) -> tuple[bytes, os.stat_result]:
    """Read ``path`` without following a link at its last component.

    Raises ``FileNotFoundError`` if it does not exist, and
    :class:`MergeRefusedError` for a link, a non-regular file, a file owned
    by someone else or an oversized file. ``O_NONBLOCK`` keeps a planted
    FIFO from hanging the open.
    """
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
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
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        # Message and position only: ``e.doc`` holds the whole file and
        # must never reach a log.
        raise MergeRefusedError(
            f"{what} {path} is not valid JSON ({e.msg}, line {e.lineno} "
            f"column {e.colno})"
        ) from None
    if not isinstance(obj, dict):
        raise MergeRefusedError(
            f"{what} {path} is a JSON {type(obj).__name__}, not an object"
        )
    return obj, text


def read_json_object(
    path: Path, *, what: str, allow_empty: bool = False
) -> tuple[dict, os.stat_result]:
    """Read ``path`` (see :func:`_read_regular`) and require a JSON
    object. ``allow_empty`` treats a blank file as ``{}``."""
    raw, st = _read_regular(path, what=what)
    obj, _ = _parse_object(raw, path, what=what, allow_empty=allow_empty)
    return obj, st


def is_link_into(target: Path, source: Path) -> bool:
    """True iff ``target`` is a symlink that resolves to ``source``."""
    try:
        if not stat.S_ISLNK(os.lstat(target).st_mode):
            return False
        return os.path.realpath(target) == os.path.realpath(source)
    except OSError:
        return False


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _check_unchanged(target: Path, expected: os.stat_result | None) -> None:
    """Refuse if the live path is no longer what was read."""
    try:
        now = os.lstat(target)
    except FileNotFoundError:
        if expected is None:
            return
        raise MergeRefusedError(f"live file {target} disappeared during the merge") from None
    if expected is None or not _same_file(now, expected) or (
        stat.S_IFMT(now.st_mode) != stat.S_IFMT(expected.st_mode)
    ):
        raise MergeRefusedError(f"live file {target} changed during the merge")


def atomic_write(
    target: Path, text: str, *, mode: int, expected: os.stat_result | None
) -> None:
    """Write ``text`` to ``target`` via temp + fsync + rename.

    ``expected`` is the ``lstat`` of what was at ``target`` when it was
    read (``None`` if nothing was). If the path changed since, nothing
    is renamed. The rename replaces the directory entry and never
    follows a link at ``target``. On any failure the temp file is
    removed and ``target`` is as it was.
    """
    parent = target.parent
    fd, tmp_name = tempfile.mkstemp(
        dir=parent, prefix=f".{target.name}.", suffix=".ctrlrelay-tmp"
    )
    tmp = Path(tmp_name)
    try:
        try:
            os.fchmod(fd, mode)
            data = text.encode("utf-8")
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        _check_unchanged(target, expected)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    try:
        dfd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _refuse(label: str, reason: str) -> MergeOutcome:
    logger.error("%s: merge refused, live file left untouched: %s", label, reason)
    return MergeOutcome(label=label, action="refused", detail=reason)


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
        upstream, _ = read_json_object(source, what="synced file")
    except FileNotFoundError:
        return MergeOutcome(label=label, action="skipped-source-missing")
    except MergeRefusedError as e:
        return _refuse(label, str(e))

    if is_link_into(target, source):
        return _refuse(
            label,
            f"live file {target} is still a symlink into the personalization "
            f"checkout; {MIGRATE_HINT}",
        )
    try:
        live, live_st = read_json_object(
            target, what="live file", allow_empty=True
        )
    except FileNotFoundError:
        live, live_st = {}, None
    except MergeRefusedError as e:
        return _refuse(label, str(e))

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
    if live_st is None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        atomic_write(target, text, mode=mode, expected=live_st)
    except MergeRefusedError as e:
        return _refuse(label, str(e))
    except OSError as e:
        return _refuse(label, f"write to {target} failed: {e.strerror}")

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
    gets the source's bytes as they are now (so whatever the app wrote
    through the link is kept on this machine), mode 0600 or stricter.
    A link pointing anywhere else is refused and not followed.
    """
    try:
        link_st = os.lstat(target)
    except FileNotFoundError:
        return MergeOutcome(
            label=label, action="nothing-to-migrate", detail="live path missing"
        )
    if not stat.S_ISLNK(link_st.st_mode):
        return MergeOutcome(
            label=label, action="nothing-to-migrate", detail="already a real file"
        )
    if not is_link_into(target, source):
        return _refuse(
            label,
            f"live file {target} is a symlink to somewhere other than "
            f"{source}; not migrating it",
        )
    try:
        raw, src_st = _read_regular(source, what="synced file")
        _, text = _parse_object(raw, source, what="synced file", allow_empty=False)
    except FileNotFoundError:
        return _refuse(label, f"synced file {source} is missing")
    except MergeRefusedError as e:
        return _refuse(label, str(e))
    mode = stat.S_IMODE(src_st.st_mode) & 0o600
    try:
        atomic_write(target, text, mode=mode, expected=link_st)
    except MergeRefusedError as e:
        return _refuse(label, str(e))
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
