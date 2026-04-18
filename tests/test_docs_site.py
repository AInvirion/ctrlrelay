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
