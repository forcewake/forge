from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from forge.gitlab.events import UserInfo


#: R42-02 (#375): GitLab documents exactly 15 canonical ``Pipeline.source``
#: values (the REST field and ``CI_PIPELINE_SOURCE``; see
#: docs/research/2026-09-27-gitlab-pipeline-sources/): push,
#: merge_request_event, api, chat, external,
#: external_pull_request_event, ondemand_dast_scan,
#: ondemand_dast_validation, parent_pipeline, pipeline, schedule,
#: security_orchestration_policy, trigger, web, webide. The MR
#: verification pipelines carry ``merge_request_event`` — there is no
#: ``merge_request`` source value. Very old instances presented the
#: legacy ``merge_request`` spelling for the same event, so the adapter
#: folds it onto the canonical value; unknown values pass through
#: unchanged (a new GitLab spelling must surface, not vanish).
_PIPELINE_SOURCE_ALIASES = {"merge_request": "merge_request_event"}


def normalize_pipeline_source(raw: str | None) -> str | None:
    """Canonicalize one provider pipeline ``source`` (R42-02, #375).

    The ADAPTER BOUNDARY normalization — stripped, lowercased, alias-folded
    onto the canonical spelling — so everything downstream compares
    canonical constants, never presentation labels. ``None``/blank stay
    ``None`` (an absent source degrades correlation, it never invents
    one).
    """
    if raw is None:
        return None
    text = raw.strip().lower()
    if not text:
        return None
    return _PIPELINE_SOURCE_ALIASES.get(text, text)


class DiffRefs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_sha: str | None = None
    head_sha: str | None = None
    start_sha: str | None = None


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    iid: int
    title: str
    description: str | None = None
    state: str
    source_branch: str
    target_branch: str
    author: UserInfo | None = None
    web_url: str | None = None
    draft: bool = False
    labels: list[str] = []
    sha: str | None = None
    #: R24 acceptance: ISO-8601 timestamp the MR was merged at — None while
    #: the MR is open/closed-unmerged; carried so the acceptance evidence can
    #: time the human-wait decomposition from the provider's own record.
    merged_at: str | None = None
    diff_refs: DiffRefs | None = None
    has_conflicts: bool = False


class Diff(BaseModel):
    model_config = ConfigDict(extra="ignore")

    old_path: str
    new_path: str
    a_mode: str | None = None
    b_mode: str | None = None
    new_file: bool = False
    renamed_file: bool = False
    deleted_file: bool = False
    diff: str = ""


class MRVersion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    head_commit_sha: str | None = None
    base_commit_sha: str | None = None
    start_commit_sha: str | None = None


class NotePosition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_sha: str | None = None
    start_sha: str | None = None
    head_sha: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    position_type: str | None = None
    old_line: int | None = None
    new_line: int | None = None


class Note(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    body: str
    author: UserInfo | None = None
    created_at: str | None = None
    updated_at: str | None = None
    system: bool = False
    resolvable: bool = False
    resolved: bool | None = None
    type: str | None = None
    position: NotePosition | None = None


class Discussion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    individual_note: bool = False
    notes: list[Note] = []


class Pipeline(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    status: str
    ref: str | None = None
    sha: str | None = None
    web_url: str | None = None
    source: str | None = None
    #: R41-05 (#360): the provider-side creation stamp — the time-ref the
    #: occupancy probe correlates a branch listing's rows against the
    #: attempt's dispatch window. Optional because listings from older
    #: fakes/tests may omit it; absence degrades correlation, never breaks it.
    created_at: str | None = None

    #: R42-02 (#375): the source arrives canonicalized (see
    #: :func:`normalize_pipeline_source`) — the parse boundary is the ONLY
    #: place a presentation spelling is folded onto the canonical value, so
    #: occupancy policy compares what GitLab actually emits.
    @field_validator("source", mode="before")
    @classmethod
    def _canonical_source(cls, value: str | None) -> str | None:
        return normalize_pipeline_source(value)


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    stage: str | None = None
    status: str
    web_url: str | None = None
    failure_reason: str | None = None
    duration: float | None = None


class TreeEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str
    type: str
    path: str
    mode: str | None = None


class RepositoryFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_name: str
    file_path: str
    size: int | None = None
    encoding: str | None = None
    content: str = ""
    content_sha256: str | None = None
    ref: str | None = None


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    path_with_namespace: str
    description: str | None = None
    web_url: str | None = None
    default_branch: str | None = None


class Issue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    iid: int
    title: str
    description: str | None = None
    state: str
    labels: list[str] = []
    web_url: str | None = None
    author: UserInfo | None = None
    created_at: str | None = None
    updated_at: str | None = None


class Label(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    color: str | None = None
    description: str | None = None
