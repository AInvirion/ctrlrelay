"""ctrlrelay: Local-first orchestrator for headless coding agents."""

from importlib.metadata import PackageNotFoundError, version

from ctrlrelay.core import checkpoint


def _resolve_version() -> str:
    try:
        return version("ctrlrelay")
    except PackageNotFoundError:
        # Source checkout with no installed dist-info — e.g. a bare
        # `pytest tests/` with pyproject's `pythonpath = ["src"]` as the
        # only mechanism putting the package on sys.path. Fall back to
        # parsing pyproject.toml so the drift-catcher test still sees a
        # real version instead of a placeholder.
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if pyproject.is_file():
            try:
                with pyproject.open("rb") as f:
                    return tomllib.load(f)["project"]["version"]
            except (OSError, KeyError, tomllib.TOMLDecodeError):
                pass
        return "0.0.0+unknown"


__version__ = _resolve_version()

# Public API
__all__ = ["__version__", "checkpoint"]
