"""Harness driver surface (Stage E3b): the shared prompt builder and the
Actions-lane entry point's building blocks.

- :mod:`forge.harnesses.prompt` — the implementation brief rendered from
  the approved RunSpec + issue + plan (shared by both lanes).
"""

from forge.harnesses.prompt import (
    TASK_PROMPT,
    BriefContext,
    BriefPolicy,
    convention_files,
    render_brief,
)

__all__ = [
    "TASK_PROMPT",
    "BriefContext",
    "BriefPolicy",
    "convention_files",
    "render_brief",
]
