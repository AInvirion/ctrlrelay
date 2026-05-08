"""Path encoding and template resolution for personalization sync.

Two responsibilities:

1. ``encode_project_path`` — replicate Claude Code's directory-name
   encoding for ``~/.claude/projects/<encoded>/``. Verified empirically
   against ~/.claude/projects/ on a real install: every character that
   is not ``[A-Za-z0-9-]`` is replaced by ``-``. No collapsing of runs;
   ``/.ctrlrelay/`` becomes ``--ctrlrelay-`` (the slash + dot each
   produce their own ``-``).

2. ``resolve_template`` — substitute the placeholder set declared in
   ``config.py`` (``${HOME}``, ``${PROJECT}``, ``${PROJECT_ENCODED}``,
   ``${PROJECT_LOCAL}``, ``${PROJECT_PARENT}``) in source/target paths
   from ``Personalization.paths``.

Kept dependency-free so the manager can use it during init/wire without
loading the rest of the package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Anything outside [A-Za-z0-9-] becomes '-'. Hyphens already in the
# original path stay as hyphens (idempotent under this rule).
_NON_SAFE_CHAR_RE = re.compile(r"[^A-Za-z0-9-]")


def encode_project_path(absolute_path: Path | str, *, resolve_symlinks: bool = False) -> str:
    """Encode an absolute filesystem path the way Claude Code does for
    its per-project directory under ``~/.claude/projects/``.

    Args:
        absolute_path: Absolute path or ``~``-prefixed path. Relative
            paths are an error.
        resolve_symlinks: When True, follow symlinks before encoding.
            Default False — match the path as configured in
            ``orchestrator.yaml`` (``RepoConfig.local_path``) which is
            ``expanduser``'d but not resolved. Set True only if you
            observe Claude using the resolved form on this machine.

    Returns:
        The encoded directory name (no leading slash; first character
        is ``-`` because absolute paths start with ``/``).

    Raises:
        ValueError: If ``absolute_path`` is not absolute after
            ``expanduser``.
    """
    p = Path(absolute_path).expanduser()
    if not p.is_absolute():
        raise ValueError(
            f"encode_project_path requires an absolute path, got {absolute_path!r}"
        )
    if resolve_symlinks:
        p = p.resolve()
    return _NON_SAFE_CHAR_RE.sub("-", str(p))


@dataclass(frozen=True)
class TemplateContext:
    """Inputs for ``resolve_template``.

    For non-project-scoped sync entries leave both fields ``None``.
    For project-scoped entries set both: ``project`` is the
    ``<owner>-<repo>`` flat slug (avoids nested-owner-dir surprises and
    name collisions across orgs); ``project_local`` is the
    ``RepoConfig.local_path`` value (already ``expanduser``'d at config
    load time).
    """

    project: str | None = None
    project_local: Path | None = None


def resolve_template(template: str, ctx: TemplateContext) -> Path:
    """Substitute placeholders in a source/target template and return a Path.

    Recognized placeholders:
      - ``${HOME}`` — current user's home directory.
      - ``${PROJECT}`` — ``<owner>-<repo>`` flat slug.
      - ``${PROJECT_ENCODED}`` — Claude's path encoding of
        ``project_local`` (cf. ``encode_project_path``).
      - ``${PROJECT_LOCAL}`` — absolute path to the project working tree.
      - ``${PROJECT_PARENT}`` — parent directory of ``project_local``.

    Project placeholders raise ``ValueError`` when the context lacks
    project information; ``HOME`` always works. Trailing slashes on the
    template are preserved on the returned ``Path`` only by virtue of
    the caller — ``Path`` itself does not retain trailing slashes —
    so callers that care about file-vs-directory distinction must
    inspect the *template string* (e.g., ``template.endswith("/")``)
    not the returned path.
    """
    result = template
    if "${HOME}" in result:
        result = result.replace("${HOME}", str(Path.home()))

    needs_project = any(
        marker in result
        for marker in (
            "${PROJECT}",
            "${PROJECT_ENCODED}",
            "${PROJECT_LOCAL}",
            "${PROJECT_PARENT}",
        )
    )
    if needs_project:
        if ctx.project is None or ctx.project_local is None:
            raise ValueError(
                f"template {template!r} uses project placeholders but no "
                "project context was provided"
            )
        # Replace longest matches first so ``${PROJECT_ENCODED}`` doesn't
        # get partially-eaten by a ``${PROJECT}`` substitution.
        result = result.replace("${PROJECT_ENCODED}", encode_project_path(ctx.project_local))
        result = result.replace("${PROJECT_LOCAL}", str(ctx.project_local))
        result = result.replace("${PROJECT_PARENT}", str(ctx.project_local.parent))
        result = result.replace("${PROJECT}", ctx.project)

    return Path(result).expanduser()


def project_slug(repo_name: str) -> str:
    """Flatten an ``owner/repo`` string to ``owner-repo``.

    Used both for the ``${PROJECT}`` placeholder value and for the
    on-disk source-tree directory name inside the personalization
    repo (e.g. ``claude-memory/AInvirion-ctrlrelay/``). Avoids nested
    owner subdirectories and avoids collisions across orgs that use
    the same repo name.
    """
    return repo_name.replace("/", "-")
