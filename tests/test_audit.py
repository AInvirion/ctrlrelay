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


class TestAuditChecks:
    def test_check_checkpoint_passes_with_import(self, tmp_path: Path) -> None:
        """Should pass if skill imports checkpoint module."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\n```python\nfrom dev_sync import checkpoint\ncheckpoint.done()\n```",
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.CHECKPOINT)
        assert result.passed is True

    def test_check_checkpoint_fails_without(self, tmp_path: Path) -> None:
        """Should fail if skill has no checkpoint references."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\nDo some work.",
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.CHECKPOINT)
        assert result.passed is False

    def test_check_headless_passes_without_input(self, tmp_path: Path) -> None:
        """Should pass if skill has no interactive prompts."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\nRun commands.",
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.HEADLESS)
        assert result.passed is True

    def test_check_headless_fails_with_input(self, tmp_path: Path) -> None:
        """Should fail if skill uses input()."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\n```python\nuser_input = input('Enter value:')\n```",
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.HEADLESS)
        assert result.passed is False

    def test_check_headless_fails_with_playwright(self, tmp_path: Path) -> None:
        """Should fail if skill uses playwright MCP without fallback."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\nUse mcp__playwright__navigate to browse.",
            frontmatter={"tools": "mcp__playwright__navigate"},
        )
        result = run_check(skill, AuditCheck.HEADLESS)
        assert result.passed is False

    def test_check_attribution_passes_clean(self, tmp_path: Path) -> None:
        """Should pass if no Claude/Anthropic in output."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\nDo work and report results.",
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.ATTRIBUTION)
        assert result.passed is True

    def test_check_attribution_fails_with_claude(self, tmp_path: Path) -> None:
        """Should fail if output mentions Claude."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, run_check

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content='# Skill\n\nPrint "Generated by Claude"',
            frontmatter={},
        )
        result = run_check(skill, AuditCheck.ATTRIBUTION)
        assert result.passed is False


class TestAuditFunctions:
    def test_audit_skill(self, tmp_path: Path) -> None:
        """audit_skill should run all checks on a skill."""
        from dev_sync.core.audit import AuditCheck, SkillInfo, audit_skill

        skill = SkillInfo(
            name="test",
            path=tmp_path,
            content="# Skill\n\n```python\nfrom dev_sync import checkpoint\ncheckpoint.done()\n```",
            frontmatter={},
        )

        result = audit_skill(skill)
        assert result.name == "test"
        assert AuditCheck.CHECKPOINT in result.results
        assert AuditCheck.HEADLESS in result.results

    def test_audit_all(self, tmp_path: Path) -> None:
        """audit_all should audit all skills in directory."""
        from dev_sync.core.audit import AuditCheck, audit_all

        # Create skills
        skill1 = tmp_path / "skill-one" / "SKILL.md"
        skill1.parent.mkdir()
        skill1.write_text("---\nname: skill-one\n---\n# Ready\n\nfrom dev_sync import checkpoint")

        skill2 = tmp_path / "skill-two" / "SKILL.md"
        skill2.parent.mkdir()
        skill2.write_text("---\nname: skill-two\n---\n# Not ready")

        results = audit_all(tmp_path)
        assert len(results) == 2

        by_name = {r.name: r for r in results}
        assert by_name["skill-one"].results[AuditCheck.CHECKPOINT].passed is True
        assert by_name["skill-two"].results[AuditCheck.CHECKPOINT].passed is False
