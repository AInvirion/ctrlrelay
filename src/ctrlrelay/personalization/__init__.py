"""Personalization sync — operator state shared across machines.

A separate (typically private) GitHub repo holds context that survives
across sessions and computers but doesn't belong inside any project's
own source tree: global Claude config, per-project memory, spec docs,
workspace planning notes. ctrlrelay clones this repo once per machine
and wires symlinks per the ``personalization.paths`` config so that
agent and operator writes flow straight into the working tree of the
checkout, ready to commit.

See ``manager.PersonalizationManager`` for the lifecycle.
"""

from ctrlrelay.personalization.manager import PersonalizationManager
from ctrlrelay.personalization.paths import (
    TemplateContext,
    encode_project_path,
    resolve_template,
)

__all__ = [
    "PersonalizationManager",
    "TemplateContext",
    "encode_project_path",
    "resolve_template",
]
