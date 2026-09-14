"""Harness driver prompt builders (Stage E3b scope extension).

The shared implementation brief rendered from the approved RunSpec + issue
+ plan. ONE prompt, BOTH lanes: the Actions entry point
(:mod:`forge.harness_entry`) renders it into ``.forge/brief.md`` — after
fetching the plan (the forge plan comment on the issue) and the issue body
over the read-only runner token — and the GitLab templates' brief carries
the same contract around ``$FORGE_PLAN``. The per-CLI ``-p`` prompt stays
a SHORT pointer (:data:`TASK_PROMPT`) — the quality lives in the brief
file, not in the one-liner. Per-CLI flags stay in the driver scripts
(docs/research/harness-interfaces.md); the PROMPT is shared.

Skills = the CLIs' native conventions: Claude Code reads ``CLAUDE.md`` +
``.claude/skills/``; Grok Build and opencode read ``AGENTS.md``. Those
files are in the checkout already — the brief explicitly directs the agent
to consult them (project conventions win over generic instructions).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: The lane's output contract. ``ci_lane`` is the proposal-only CI lane
#: (ADR-0016): the working tree IS the deliverable. ``dev`` is the local
#: lane: commit, never push.
Lane = str  # "ci_lane" | "dev"

#: Root-level files whose conventions the agent must obey when present.
_CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md")

#: The short per-CLI prompt. The brief file carries the quality; the
#: one-liner only points at it (identical for every driver).
TASK_PROMPT = (
    "Implement the approved task in .forge/brief.md. Read it first, then follow it exactly."
)

#: Paths the CI lane must never create or modify — CI/config files are the
#: execution profile, and the trusted publisher rejects them anyway.
_DENIED_PATHS = (
    ".gitlab-ci.yml",
    ".github/workflows/**",
    "Jenkinsfile",
    "Dockerfile*",
    "*.toml (CI/tool config — pyproject.toml only when the plan says so)",
    ".forge/** (forge's control directory — never a deliverable)",
)

_OUTPUT_CONTRACT_CI = (
    "## Output contract — proposal-only CI lane (binding)\n\n"
    "- Do **NOT** commit and do **NOT** push. You have no write credential "
    "and the lane must stay that way.\n"
    "- Leave ALL your changes in the working tree: the forge publisher "
    "collects `git diff` against the attempt base and publishes it after "
    "validation — its commit, not yours.\n"
    "- Never touch `.forge/` except reading `.forge/brief.md`; control "
    "files are never part of the candidate."
)

_OUTPUT_CONTRACT_DEV = (
    "## Output contract — local lane (binding)\n\n"
    "- Commit your changes with the message the run provides "
    "(`forge: implement <issue> (run <id>)`). Never push to a remote."
)


@dataclass(frozen=True)
class BriefContext:
    """Everything the brief is rendered from (the approved run snapshot)."""

    plan: str
    plan_digest: str = ""
    issue_title: str = ""
    issue_body: str = ""
    issue_number: int = 0
    driver: str = ""
    model: str = ""


@dataclass(frozen=True)
class BriefPolicy:
    """Constraint knobs frozen from policy/RunSpec (ADR-0011/0018)."""

    #: Dependency policy sentence; the default is the safe one for a
    #: proposal-only lane judged by a publisher.
    dependency_policy: str = (
        "Do not add new dependencies. If the approved plan requires one, "
        "note it in a code comment instead of editing manifests."
    )
    #: Language reminder appended to the style constraints.
    language: str = "the repository's language"


def _skill_instruction(references: list[str]) -> str:
    """The skills/conventions sentence for the files actually found."""
    names = " or ".join(f"`{name}`" for name in references)
    return (
        f"- Project conventions win: this repository contains {names} — read "
        "it/them FIRST and follow its instructions (skills, style, commands) "
        "wherever they do not contradict this brief."
    )


def convention_files(repo_root: Path | None) -> list[str]:
    """The convention files present at *repo_root* (AGENTS.md, CLAUDE.md).

    Empty when no root given or none exist — the brief then carries the
    conditional form instead of naming specific files.
    """
    if repo_root is None:
        return []
    return [name for name in _CONVENTION_FILES if (repo_root / name).is_file()]


def render_brief(
    context: BriefContext,
    *,
    lane: Lane = "ci_lane",
    repo_root: Path | None = None,
    policy: BriefPolicy | None = None,
) -> str:
    """The implementation brief written to ``.forge/brief.md``.

    Sections: Role → Task (issue snapshot) → Approved plan (verbatim, with
    its digest) → Constraints (dependency policy, denied paths, style) →
    Quality bar (tests; conventions files; minimal diff) → Output contract
    (lane-specific). The plan goes in VERBATIM: it was approved at the
    human gate and its digest is what ``/go`` consumed — the agent executes
    it, never redesigns it.
    """
    policy = policy or BriefPolicy()
    issue_ref = f" #{context.issue_number}" if context.issue_number else ""
    task_lines = [f"**Issue{issue_ref}:** {context.issue_title or '(untitled)'}"]
    if context.issue_body.strip():
        task_lines.append("")
        task_lines.append(context.issue_body.strip())

    digest_line = (
        f"digest `{context.plan_digest}` — this exact text is what the human approved"
        if context.plan_digest
        else "the approved plan text"
    )
    conventions = convention_files(repo_root)
    skills_line = (
        _skill_instruction(conventions)
        if conventions
        else (
            "- Project conventions win: if `AGENTS.md` or `CLAUDE.md` exists at the "
            "repository root, read it first and follow it."
        )
    )

    output_contract = _OUTPUT_CONTRACT_CI if lane == "ci_lane" else _OUTPUT_CONTRACT_DEV
    if lane not in ("ci_lane", "dev"):
        raise ValueError(f"unknown lane {lane!r} (expected 'ci_lane' | 'dev')")

    return (
        "# forge implementation brief\n\n"
        "## Role\n\n"
        "You are a staff engineer implementing an APPROVED implementation plan "
        "in the repository checked out at the pinned base commit. Work "
        "surgically: the plan was reviewed by a human and its digest is bound "
        "to this run.\n\n"
        "## Task\n\n" + "\n".join(task_lines) + "\n\n"
        "## Approved plan (implement verbatim)\n\n"
        f"The plan below was approved at the human gate ({digest_line}). "
        "Implement it as written; if you must deviate, keep the deviation "
        "minimal and note why in a code comment.\n\n"
        f"{context.plan.strip() or '(no plan text — implement the task above directly)'}\n\n"
        "## Constraints\n\n"
        f"- Dependencies: {policy.dependency_policy}\n"
        "- Denied paths — NEVER create or modify: "
        + "; ".join(f"`{path}`" for path in _DENIED_PATHS)
        + ". CI/config IS the execution profile; changing it from this lane is forbidden.\n"
        f"- Code style: match the surrounding code; new {policy.language} code "
        "carries type hints and docstrings; no commented-out code.\n"
        "- Keep the diff minimal and focused on the approved plan: no drive-by "
        "refactors, no reformatting of untouched code.\n\n"
        "## Quality bar\n\n"
        "- If the repository has a test suite (pytest, npm test, go test, "
        "cargo test, …), run the relevant subset and leave it GREEN before you "
        "finish. A candidate that breaks the existing tests is rejected.\n"
        f"{skills_line}\n"
        "- Read before writing: inspect the files the plan touches before "
        "editing them.\n\n" + output_contract + "\n"
    )
