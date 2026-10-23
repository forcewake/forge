"""Immutable plan artifacts and syntactic citations to frozen repository blobs."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from .value import Value
from .verdicts import Sha


def _nonblank(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("text must be nonblank and contain no NUL")
    return value


def _repo_path(value: str) -> str:
    if (
        "\\" in value
        or any(ord(char) < 32 for char in value)
        or ":" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(part.lower() == ".git" for part in value.split("/"))
    ):
        raise ValueError("citation and file paths must be confined canonical repository paths")
    return value


ShortText = Annotated[str, Field(min_length=1, max_length=1024), AfterValidator(_nonblank)]
Text = Annotated[str, Field(min_length=1, max_length=32768), AfterValidator(_nonblank)]
RepoPath = Annotated[str, Field(min_length=1, max_length=4096), AfterValidator(_repo_path)]
StepId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Line = Annotated[int, Field(gt=0, le=1_000_000_000)]


class RepositoryIdentity(Value):
    provider: Literal["github", "gitlab"]
    connection_id: StepId
    repository_id: Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]


class Citation(Value):
    """A location to resolve through RepoPort; construction does not verify its bytes."""

    repository: RepositoryIdentity
    source_sha: Sha
    path: RepoPath
    start_line: Line
    end_line: Line

    @model_validator(mode="after")
    def range_is_ordered(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("citation end line precedes its start")
        return self


class PlanStep(Value):
    step_id: StepId
    title: ShortText
    instructions: Text
    files: Annotated[tuple[RepoPath, ...], Field(max_length=128)] = ()
    citations: Annotated[tuple[Citation, ...], Field(max_length=64)] = ()

    @model_validator(mode="after")
    def files_are_unique(self) -> Self:
        if len(set(self.files)) != len(self.files):
            raise ValueError("plan files must be unique")
        return self


class Plan(Value):
    summary: Text
    steps: Annotated[tuple[PlanStep, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def steps_are_unique(self) -> Self:
        ids = tuple(step.step_id for step in self.steps)
        if len(set(ids)) != len(ids):
            raise ValueError("plan step identifiers must be unique")
        return self

    def render(self) -> str:
        """Deterministic complete brief text; every step and citation remains visible."""
        parts = [self.summary]
        for step in self.steps:
            parts.extend((f"## {step.step_id}: {step.title}", step.instructions))
            if step.files:
                parts.append("Files: " + ", ".join(step.files))
            for cite in step.citations:
                repo = cite.repository
                parts.append(
                    f"Source: {repo.provider}:{repo.connection_id}:{repo.repository_id}"
                    f"@{cite.source_sha}:{cite.path}#L{cite.start_line}-L{cite.end_line}"
                )
        return "\n\n".join(parts)
