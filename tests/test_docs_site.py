"""Validation tests for the Jekyll documentation site under ``docs/``.

These tests do not invoke Jekyll. They assert the structural invariants the
site relies on so a misconfigured page is caught before GitHub Pages builds.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"


def _split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 4 :].lstrip("\n")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError("front matter must be a mapping")
    return data, body


def _markdown_files() -> list[Path]:
    return sorted(p for p in DOCS.rglob("*.md") if "_site" not in p.parts)


def test_config_is_valid_yaml():
    config_path = DOCS / "_config.yml"
    assert config_path.exists(), "docs/_config.yml must exist"
    data = yaml.safe_load(config_path.read_text())
    assert isinstance(data, dict)
    assert data.get("remote_theme"), "remote_theme must be set for GitHub Pages"
    assert "jekyll-remote-theme" in (data.get("plugins") or []), (
        "jekyll-remote-theme plugin required"
    )


def test_landing_page_exists():
    assert (DOCS / "index.md").exists(), "docs/index.md landing page required"


def test_all_markdown_pages_have_title_front_matter():
    missing: list[str] = []
    for md in _markdown_files():
        front, _ = _split_front_matter(md.read_text())
        if not front.get("title"):
            missing.append(str(md.relative_to(REPO_ROOT)))
    assert not missing, f"pages missing title front matter: {missing}"


def test_parent_pages_declare_has_children():
    """Any page referenced as ``parent`` must itself set ``has_children: true``."""
    titles_with_children: set[str] = set()
    referenced_parents: set[str] = set()
    for md in _markdown_files():
        front, _ = _split_front_matter(md.read_text())
        if front.get("has_children"):
            titles_with_children.add(front["title"])
        parent = front.get("parent")
        if parent:
            referenced_parents.add(parent)
    missing = referenced_parents - titles_with_children
    assert not missing, f"parents referenced but not declared with has_children: {missing}"


def test_nav_order_unique_per_sibling_group():
    """Pages sharing a parent (or all top-level) must have distinct nav_order."""
    groups: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    for md in _markdown_files():
        front, _ = _split_front_matter(md.read_text())
        parent = front.get("parent", "__root__")
        order = front.get("nav_order")
        if order is None:
            continue
        groups[parent][order].append(str(md.relative_to(REPO_ROOT)))
    for parent, by_order in groups.items():
        collisions = {o: files for o, files in by_order.items() if len(files) > 1}
        assert not collisions, (
            f"duplicate nav_order under parent {parent!r}: {collisions}"
        )


def test_no_stray_bare_markdown_outside_structure():
    """Every page must either be the index, a top-level nav page, or have a parent."""
    offenders: list[str] = []
    for md in _markdown_files():
        rel = md.relative_to(DOCS)
        if rel.name == "index.md":
            continue
        front, _ = _split_front_matter(md.read_text())
        if front.get("parent") or front.get("nav_order") is not None:
            continue
        offenders.append(str(md.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"pages without parent or top-level nav_order: {offenders}"
    )


@pytest.mark.parametrize("path", _markdown_files())
def test_front_matter_parses(path: Path):
    front, _ = _split_front_matter(path.read_text())
    assert isinstance(front, dict)


def test_ci_runs_the_whole_suite():
    """No workflow may exclude tests from the run (#179).

    `test.yml` carried `--deselect
    tests/test_docs_site.py::test_nav_order_unique_per_sibling_group` under
    the comment "tracked separately and unchanged by this PR". The test
    passed the whole time - verified by making two pages collide on
    `nav_order`, which it catches and names both files for - so CI had
    been skipping a working guard for months and under-counting itself.

    The transferable part is the justification, not the flag. "Unchanged
    by this PR" is a pull-request-scoped reason frozen into a file that
    runs for every change. It was true the day it was written and stays
    plausible forever, because it is true of almost every pull request -
    so every reader nods and moves on. **Such a reason cannot expire on
    its own.**

    This makes the next exclusion something that has to be defended
    rather than inherited: adding one fails here, and the person adding
    it has to come and say why in this file, where a reviewer will see
    it next to this paragraph.

    If an exclusion is ever genuinely needed, prefer deleting or fixing
    the test. Do not leave the third state - a test that exists, is not
    run, and is believed to be covering something.

    **Reads the parsed `run:` values, not lines of the file.** A review
    round found three faults in the line-grep version, and parsing fixes
    all of them: a step `name:` mentioning a flag was reported as an
    offender (a false positive invites relaxing the guard, and a relaxed
    guard stops guarding); `--ignore tests/x` with a space slipped past a
    pattern that required `--ignore=`; and the guard reported clean when
    it had inspected no pytest command at all.

    **What this does NOT stop, stated so the paragraph above is not the
    false sentence.** It reads text, so an author actively evading it
    wins: `--ig"nore"` reaches pytest as `--ignore` and no pattern here
    sees it. That is not worth chasing - every fix is defeated by the
    next quoting trick, and anyone editing the workflow to dodge this
    guard can edit this file just as easily. The fault in #179 was
    **inherited, not evasive**: a reason that was true once, left in
    place, nodded past by every later reader. That is what this catches.

    One known false positive: a run script that *prints* an example
    containing an exclusion flag - `echo "e.g. pytest --ignore tests/x"` -
    is reported. Deliberately not fixed, because telling the quoted from
    the executed needs a shell parser, which is more machinery than the
    defect. Do not write that line; if you must, name it here.
    """
    import re

    import yaml

    wf_dir = REPO_ROOT / ".github" / "workflows"
    workflows = sorted(
        [*wf_dir.glob("*.yml"), *wf_dir.glob("*.yaml")]
    )
    assert len(workflows) >= 3, f"only found {len(workflows)} workflows"

    # Every `run:` script in every step, with where it came from.
    runs: list[tuple[str, str]] = []
    for wf in workflows:
        doc = yaml.safe_load(wf.read_text()) or {}
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                script = (step or {}).get("run")
                if isinstance(script, str):
                    runs.append((f"{wf.name}:{job_name}", script))

    def _code_lines(script: str) -> list[str]:
        """The lines of a run script that are not comments."""
        return [
            line
            for line in script.splitlines()
            if not line.lstrip().startswith("#")
        ]

    invocations = [
        (where, script)
        for where, script in runs
        if any(
            re.search(r"(?<![\w-])pytest(?![\w-])", line)
            for line in _code_lines(script)
        )
    ]

    # Invariant D, as a POSITIVE assertion. `offenders == []` alone passes
    # when no pytest command exists at all - a workflow replaced with
    # `run: true` would have reported clean.
    assert invocations, (
        "no workflow step runs pytest. Either CI stopped running the "
        "suite, or this guard can no longer see it - both are faults, "
        f"and neither is a clean result. Scanned: {[w for w, _ in runs]}"
    )

    # Flags that remove tests from a run, accepting `=` or a space.
    # `--maxfail` is not one: it stops early on failure, it does not hide
    # a passing test. `-m` selects by marker, which this suite does not
    # use for exclusion.
    excluders = re.compile(
        r"--deselect(?:[=\s]|$)"
        r"|--ignore(?:-glob)?(?:[=\s]|$)"
        r"|(?<!\w)-k(?:[=\s]|$)"
    )

    offenders: list[str] = []
    for where, script in invocations:
        for lineno, line in enumerate(script.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if excluders.search(line):
                offenders.append(f"{where} run line {lineno}: {line.strip()}")

        # Narrowing the target paths excludes tests without any flag at
        # all, which is the bypass a flag list cannot see.
        targets = [
            tok
            for tok in script.split()
            if tok.startswith("tests") and not tok.startswith("-")
        ]
        if targets and not any(t.rstrip("/") == "tests" for t in targets):
            offenders.append(
                f"{where}: runs only {targets}, not the whole tests/ tree"
            )

    assert offenders == [], (
        "a workflow excludes tests from the run. Delete the test, fix it, "
        "or change this guard and say why here - do not leave a test that "
        f"exists, is not run, and is assumed to pass (#179): {offenders}"
    )
