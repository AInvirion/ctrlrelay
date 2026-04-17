"""Skill audit functionality for orchestrator readiness checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


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
