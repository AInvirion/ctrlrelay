"""Skill audit functionality for orchestrator readiness checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml


class AuditCheck(str, Enum):
    """Types of orchestrator readiness checks."""

    CHECKPOINT = "checkpoint"
    HEADLESS = "headless"
    CONTEXT_PATH = "context_path"
    ATTRIBUTION = "attribution"


@dataclass
class AuditResult:
    """Result of a single audit check."""

    passed: bool
    reason: str = ""
    auto_fixable: bool = False


@dataclass
class SkillAudit:
    """Audit results for a single skill."""

    name: str
    path: Path
    results: dict[AuditCheck, AuditResult] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True if all checks passed."""
        return all(r.passed for r in self.results.values())

    @property
    def status(self) -> str:
        """Human-readable status."""
        return "READY" if self.passed else "NOT READY"


@dataclass
class SkillInfo:
    """Basic skill information from SKILL.md."""

    name: str
    path: Path
    content: str
    frontmatter: dict


def discover_skills(skills_dir: Path) -> list[SkillInfo]:
    """Discover all skills in a directory.

    Args:
        skills_dir: Path to skills directory.

    Returns:
        List of SkillInfo for each skill found.
    """
    skills = []

    if not skills_dir.exists():
        return skills

    for skill_md in skills_dir.glob("*/SKILL.md"):
        content = skill_md.read_text()

        # Parse YAML frontmatter
        frontmatter = {}
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if match:
            try:
                frontmatter = yaml.safe_load(match.group(1)) or {}
            except yaml.YAMLError:
                pass

        name = frontmatter.get("name", skill_md.parent.name)
        skills.append(
            SkillInfo(
                name=name,
                path=skill_md.parent,
                content=content,
                frontmatter=frontmatter,
            )
        )

    return sorted(skills, key=lambda s: s.name)
