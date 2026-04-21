"""ctrlrelay: Local-first orchestrator for headless coding agents."""

from importlib.metadata import PackageNotFoundError, version

from ctrlrelay.core import checkpoint

try:
    __version__ = version("ctrlrelay")
except PackageNotFoundError:
    # Source checkout without install (uv dev / test / CI pre-install).
    __version__ = "0.0.0+unknown"

# Public API
__all__ = ["__version__", "checkpoint"]
