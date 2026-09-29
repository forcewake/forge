"""I1 — the bot never merges (ADR-0003).

Two mechanical checks: no source file contains a merge API call, and every
lane template disables push and holds no write credential.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

# Each merge API, with one sample of how code would call it.
MERGE_APIS: dict[str, tuple[str, str]] = {
    "GitLab: accept a merge request": (
        r"/merge_requests/[^\"'\s]*/merge\b",
        'await self._put(f"/projects/{pid}/merge_requests/{iid}/merge")',
    ),
    "any REST path ending in /merge": (r"/merge[\"'?]", 'await self._put(f"{mr_path}/merge")'),
    "GitLab: merge when the pipeline succeeds": (
        r"\bmerge_when_pipeline_succeeds\b",
        '{"merge_when_pipeline_succeeds": True}',
    ),
    "GitLab: merge trains": (
        r"/merge_trains\b",
        'f"/projects/{pid}/merge_trains/merge_requests/1"',
    ),
    "GitLab GraphQL: accept a merge request": (
        r"\bmergeRequestAccept\b",
        "mutation { mergeRequestAccept(input: $input) { errors } }",
    ),
    "auto-merge flags": (r"\bauto_merge\b|\bautoMerge\b", '{"auto_merge": True}'),
    "GitHub REST: merge a pull request": (
        r"/pulls/[^\"'\s]*/merge\b",
        'await self._put(f"/repos/{repo}/pulls/{number}/merge")',
    ),
    "GitHub REST: merge a branch": (r"/merges\b", 'await self._post(f"/repos/{repo}/merges")'),
    "GitHub GraphQL: merge a pull request": (
        r"\bmergePullRequest\b",
        "mutation { mergePullRequest(input: $input) { clientMutationId } }",
    ),
    "GitHub GraphQL: enable auto-merge": (
        r"\benablePullRequestAutoMerge\b",
        "mutation { enablePullRequestAutoMerge(input: $input) { clientMutationId } }",
    ),
    "GitHub GraphQL: merge a branch": (
        r"\bmergeBranch\b",
        "mutation { mergeBranch(input: $input) { clientMutationId } }",
    ),
    "GitHub GraphQL: merge queue": (
        r"\benqueuePullRequest\b",
        "mutation { enqueuePullRequest(input: $input) { clientMutationId } }",
    ),
    "Azure DevOps: auto-complete": (r"\bautoCompleteSetBy\b", '{"autoCompleteSetBy": {"id": me}}'),
    "Azure DevOps: completion options": (
        r"\bcompletionOptions\b",
        '{"completionOptions": {"deleteSourceBranch": True}}',
    ),
    "Azure DevOps: complete a pull request": (
        r"[\"']status[\"']\s*:\s*[\"']completed[\"']",
        'await self._patch(pr_url, json={"status": "completed"})',
    ),
}
PATTERNS = {name: re.compile(pattern) for name, (pattern, _) in MERGE_APIS.items()}
NOT_MERGES = (
    'f"/projects/{pid}/merge_requests/{iid}/notes"',
    "await gitlab.list_merge_requests(project_id)",
    'f"/projects/{pid}/merge_requests/{iid}/merge_ref"',
    '{"merge_request_iid": iid, "status": "in_progress"}',
)
LANE_TEMPLATES = sorted(
    [
        *REPO.glob("ci/templates/*.yml"),
        REPO / ".github/workflows/forge-harness.yml",
        REPO / "forge-lane.azure-pipelines.yml",
    ]
)
GITHUB_TEMPLATES = [p for p in LANE_TEMPLATES if "github" in p.name or p.parent.name == "workflows"]
AZURE_TEMPLATES = [p for p in LANE_TEMPLATES if p.name.endswith(".azure-pipelines.yml")]
READ_ONLY = {"read", "none"}


def _source_files() -> list[Path]:
    return sorted(p for p in (REPO / "src").rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


@pytest.mark.parametrize("name", sorted(MERGE_APIS))
def test_each_pattern_recognises_its_merge_call(name: str):
    assert PATTERNS[name].search(MERGE_APIS[name][1])


@pytest.mark.parametrize("sample", NOT_MERGES)
def test_ordinary_merge_request_calls_are_not_flagged(sample: str):
    assert [name for name, p in PATTERNS.items() if p.search(sample)] == []


@pytest.mark.parametrize("path", _source_files(), ids=_rel)
def test_source_contains_no_merge_call(path: Path):
    text = path.read_text(encoding="utf-8")
    hits = [name for name, pattern in PATTERNS.items() if pattern.search(text)]
    assert hits == [], f"{_rel(path)} uses a merge API: {hits}"


@pytest.mark.parametrize("path", LANE_TEMPLATES, ids=_rel)
def test_lane_template_disables_push(path: Path):
    assert "git remote set-url --push origin FORBIDDEN" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", GITHUB_TEMPLATES, ids=_rel)
def test_github_lane_token_is_read_only(path: Path):
    text = path.read_text(encoding="utf-8")
    assert "persist-credentials: false" in text
    doc = yaml.safe_load(text)
    for name, job in doc["jobs"].items():
        permissions = job.get("permissions", doc.get("permissions"))
        assert isinstance(permissions, dict), f"job {name} does not pin its token permissions"
        assert set(permissions.values()) <= READ_ONLY, f"job {name}: {permissions}"


@pytest.mark.parametrize("path", AZURE_TEMPLATES, ids=_rel)
def test_azure_lane_holds_no_access_token(path: Path):
    text = path.read_text(encoding="utf-8")
    assert "persistCredentials: false" in text
    assert "System.AccessToken" not in text
