"""Helpers for seeding candidate-bundle artifacts in the GitLab fake.

Stage D (ADR-0016): the harness job uploads ``.forge/candidate.diff`` +
``.forge/candidate.meta.json``; forge adopts the diff through the trusted
publisher. These helpers let tests seed a well-formed candidate (or a
malformed one) without hand-building JSON each time.
"""

from __future__ import annotations
import json

from tests.fixtures.fake_gitlab import FakeGitLab

DIFF_PATH = ".forge/candidate.diff"
META_PATH = ".forge/candidate.meta.json"


def create_diff(path: str, content: str) -> str:
    """A ``git diff --binary --full-index`` fragment creating one file."""
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    body = "".join(f"+{line}\n" for line in lines)
    count = len(lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{count} @@\n"
        f"{body}"
    )


def modify_diff(path: str, old_text: str, new_text: str) -> str:
    """A fragment modifying one line of an existing file (1-line context)."""
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    old_body = "".join(f" {line}\n" for line in old_lines[:-1])
    old_body += f"-{old_lines[-1]}\n"
    new_body = "".join(f"+{line}\n" for line in new_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{len(old_lines)} +1,{len(new_lines)} @@\n"
        f"{old_body}{new_body}"
    )


def delete_diff(path: str, old_text: str) -> str:
    """A fragment deleting an existing file (full old content shown)."""
    lines = old_text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    body = "".join(f"-{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        f"@@ -1,{len(lines)} +0,0 @@\n"
        f"{body}"
    )


def seed_candidate(
    fake_gitlab: FakeGitLab,
    job_id: int,
    *,
    attempt_base: str,
    diff: str = "",
    exit: str = "completed",
    driver: str = "claude-code",
    model: str = "glm-5.3-flash[1m]",
    usage: dict | None = None,
    summary: str = "done",
    meta_override: dict | None = None,
    omit_diff: bool = False,
    omit_meta: bool = False,
) -> None:
    """Seed the candidate artifacts a proposal-only job uploads."""
    meta = {
        "attempt_base": attempt_base,
        "driver": driver,
        "model": model,
        "exit": exit,
        "summary": summary,
    }
    if usage is not None:
        meta["usage"] = usage
    if meta_override is not None:
        meta.update(meta_override)
    if not omit_meta:
        fake_gitlab.seed_job_artifact(job_id, META_PATH, json.dumps(meta))
    if not omit_diff:
        fake_gitlab.seed_job_artifact(job_id, DIFF_PATH, diff)
