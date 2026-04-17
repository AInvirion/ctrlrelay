"""Tests for skill audit functionality."""

from pathlib import Path

import pytest

from dev_sync.core.audit import AuditCheck, AuditResult, SkillAudit


class TestAuditModels:
    def test_audit_check_values(self) -> None:
        """AuditCheck should have all required check types."""
        assert AuditCheck.CHECKPOINT.value == "checkpoint"
        assert AuditCheck.HEADLESS.value == "headless"
        assert AuditCheck.CONTEXT_PATH.value == "context_path"
        assert AuditCheck.ATTRIBUTION.value == "attribution"

    def test_skill_audit_passed(self) -> None:
        """SkillAudit should calculate passed status."""
        audit = SkillAudit(
            name="test-skill",
            path=Path("/skills/test"),
            results={
                AuditCheck.CHECKPOINT: AuditResult(passed=True),
                AuditCheck.HEADLESS: AuditResult(passed=True),
                AuditCheck.CONTEXT_PATH: AuditResult(passed=True),
                AuditCheck.ATTRIBUTION: AuditResult(passed=True),
            },
        )
        assert audit.passed is True
        assert audit.status == "READY"

    def test_skill_audit_failed(self) -> None:
        """SkillAudit should report NOT READY if any check fails."""
        audit = SkillAudit(
            name="test-skill",
            path=Path("/skills/test"),
            results={
                AuditCheck.CHECKPOINT: AuditResult(passed=False, reason="No checkpoint calls"),
                AuditCheck.HEADLESS: AuditResult(passed=True),
                AuditCheck.CONTEXT_PATH: AuditResult(passed=True),
                AuditCheck.ATTRIBUTION: AuditResult(passed=True),
            },
        )
        assert audit.passed is False
        assert audit.status == "NOT READY"
