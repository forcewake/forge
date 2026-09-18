from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from forge.gitlab.events import UserInfo


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
