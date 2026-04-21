"""End-to-end install check (issue #87).

Builds the wheel from the current source tree, installs it into a
disposable venv, and exercises the installed `ctrlrelay` binary. Slow
(~20s on a warm machine), so it is gated behind ``CTRLRELAY_E2E=1`` to
keep the default ``pytest`` run fast and hermetic. CI sets the gate
explicitly in the e2e job.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "e2e_install_check.sh"

pytestmark = pytest.mark.skipif(
    os.environ.get("CTRLRELAY_E2E") != "1",
    reason="set CTRLRELAY_E2E=1 to run the build+install end-to-end check",
)


def test_e2e_install_check_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"missing helper script: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} must be executable"


def test_e2e_install_check_runs_clean() -> None:
    """The full build → install → validate flow must succeed end-to-end."""
    assert shutil.which("bash") is not None, "bash is required to run the e2e script"

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert result.returncode == 0, (
        f"e2e install check failed (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert "All e2e checks passed" in result.stdout
