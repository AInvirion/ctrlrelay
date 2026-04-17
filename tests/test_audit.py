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


class TestSkillDiscovery:
    def test_discover_skills(self, tmp_path: Path) -> None:
        """Should find all SKILL.md files in directory."""
        from dev_sync.core.audit import discover_skills

        # Create mock skills
        skill1 = tmp_path / "skill-one" / "SKILL.md"
        skill1.parent.mkdir()
        skill1.write_text("---\nname: skill-one\n---\n# Skill One")

        skill2 = tmp_path / "skill-two" / "SKILL.md"
        skill2.parent.mkdir()
        skill2.write_text("---\nname: skill-two\n---\n# Skill Two")

        skills = discover_skills(tmp_path)
        assert len(skills) == 2
        names = {s.name for s in skills}
        assert names == {"skill-one", "skill-two"}

    def test_discover_skills_empty_dir(self, tmp_path: Path) -> None:
        """Should return empty list if no skills found."""
        from dev_sync.core.audit import discover_skills

        skills = discover_skills(tmp_path)
        assert skills == []

    def test_discover_skills_parses_name(self, tmp_path: Path) -> None:
        """Should parse skill name from YAML frontmatter."""
        from dev_sync.core.audit import discover_skills

        skill = tmp_path / "my-skill" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("---\nname: custom-name\ndescription: Test\n---\n# Content")

        skills = discover_skills(tmp_path)
        assert skills[0].name == "custom-name"
