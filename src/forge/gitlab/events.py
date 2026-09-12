from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class UserInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    username: str
    email: str | None = None
    avatar_url: str | None = None


class ProjectInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    path_with_namespace: str
    web_url: str
    default_branch: str = "main"


class RepositoryInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    url: str
    homepage: str | None = None


class CommitAuthor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    email: str


class CommitInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    message: str
    title: str | None = None
    timestamp: datetime | None = None
    url: str | None = None
    author: CommitAuthor | None = None
    added: list[str] | None = None
    modified: list[str] | None = None
    removed: list[str] | None = None


class LabelInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    color: str | None = None
    description: str | None = None


class GitLabEvent(BaseModel):
    """Base model for all GitLab webhook events."""

    model_config = ConfigDict(extra="ignore")

    object_kind: str
    event_type: str | None = None
    user: UserInfo | None = None
    project: ProjectInfo | None = None
    repository: RepositoryInfo | None = None


class MRLastCommit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    message: str
    title: str | None = None
    url: str | None = None
    author: CommitAuthor | None = None


class MRSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    name: str | None = None
    path_with_namespace: str | None = None
    web_url: str | None = None


class MRObjectAttributes(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    iid: int
    title: str
    description: str | None = None
    state: str | None = None
    action: str | None = None
    source_branch: str | None = None
    target_branch: str | None = None
    author_id: int | None = None
    url: str | None = None
    source: MRSource | None = None
    target: MRSource | None = None
    last_commit: MRLastCommit | None = None
    draft: bool | None = None
    created_at: str | None = None
    updated_at: str | None = None


class MergeRequestEvent(GitLabEvent):
    """Merge Request Hook event."""

    object_kind: Literal["merge_request"]
    object_attributes: MRObjectAttributes
    labels: list[LabelInfo] = []
    changes: dict | None = None


class NoteObjectAttributes(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    note: str
    noteable_type: str | None = None
    noteable_id: int | None = None
    author_id: int | None = None
    url: str | None = None
    description: str | None = None
    discussion_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class NoteMRInfo(BaseModel):
    """Lightweight MR info embedded in note events."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    iid: int | None = None
    title: str | None = None
    source_branch: str | None = None
    target_branch: str | None = None
    state: str | None = None
    url: str | None = None


class NoteIssueInfo(BaseModel):
    """Lightweight issue info embedded in note events."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    iid: int | None = None
    title: str | None = None
    state: str | None = None


class NoteEvent(GitLabEvent):
    """Note Hook event (comments on MRs, issues, commits, snippets)."""

    object_kind: Literal["note"]
    object_attributes: NoteObjectAttributes
    merge_request: NoteMRInfo | None = None
    issue: NoteIssueInfo | None = None


class PipelineObjectAttributes(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    ref: str | None = None
    status: str | None = None
    stages: list[str] | None = None
    source: str | None = None
    sha: str | None = None
    before_sha: str | None = None
    created_at: str | None = None
    finished_at: str | None = None
    duration: int | None = None


class PipelineMRInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    iid: int | None = None
    title: str | None = None
    source_branch: str | None = None
    target_branch: str | None = None
    url: str | None = None


class BuildInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    stage: str | None = None
    status: str | None = None
    failure_reason: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    when: str | None = None
    manual: bool | None = None
    allow_failure: bool | None = None


class PipelineEvent(GitLabEvent):
    """Pipeline Hook event."""

    object_kind: Literal["pipeline"]
    object_attributes: PipelineObjectAttributes
    merge_request: PipelineMRInfo | None = None
    builds: list[BuildInfo] = []


class PushEvent(GitLabEvent):
    """Push Hook event. Fields are top-level (no object_attributes)."""

    object_kind: Literal["push"]
    before: str | None = None
    after: str | None = None
    ref: str | None = None
    checkout_sha: str | None = None
    user_id: int | None = None
    user_name: str | None = None
    user_username: str | None = None
    user_email: str | None = None
    user_avatar: str | None = None
    project_id: int | None = None
    commits: list[CommitInfo] = []
    total_commits_count: int = 0


class IssueObjectAttributes(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    iid: int
    title: str
    description: str | None = None
    state: str | None = None
    action: str | None = None
    author_id: int | None = None
    url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class IssueEvent(GitLabEvent):
    """Issue Hook event."""

    object_kind: Literal["issue"]
    object_attributes: IssueObjectAttributes
    labels: list[LabelInfo] = []
    changes: dict | None = None


class JobEvent(GitLabEvent):
    """Job Hook event. GitLab uses object_kind='build' for jobs."""

    object_kind: Literal["build"]
    ref: str | None = None
    build_id: int | None = None
    build_name: str | None = None
    build_stage: str | None = None
    build_status: str | None = None
    build_failure_reason: str | None = None
    build_started_at: str | None = None
    build_finished_at: str | None = None
    build_duration: float | None = None
    build_allow_failure: bool | None = None
    pipeline_id: int | None = None
    project_id: int | None = None
    project_name: str | None = None
