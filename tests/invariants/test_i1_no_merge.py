"""I1 — the bot never merges (ADR-0003).

Two mechanical checks: no source file contains a merge API call, and every
lane template disables push and holds no write credential.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

# Each merge API, with one sample of how code would call it.
MERGE_APIS: dict[str, tuple[str, str]] = {
    "GitLab: accept a merge request": (
        r"/merge_requests/[^\"'\s]*/merge\b",
        'await self._put(f"/projects/{pid}/merge_requests/{iid}/merge")',
    ),
    "any REST path ending in /merge": (
        r"(?m)^(?!.*\brefs/).*?/merge[\"'?]",
        'await self._put(f"{mr_path}/merge")',
    ),
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
    "REST auto-merge flag": (r"\bauto_merge\b", '{"auto_merge": True}'),
    "auto-merge flag (DESIGN §4 spelling)": (r"\bautoMerge\b", '{"autoMerge": True}'),
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
        'await self._patch(f"{repo}/pullRequests/{pr}", json={"status": "completed"})',
    ),
    "CLI: merge a pull or merge request": (
        r"\b(?:gh\s+pr|glab\s+mr)\s+merge\b|[\"'](?:pr|mr)[\"']\s*,\s*[\"']merge[\"']",
        'subprocess.run(["gh", "pr", "merge", str(number)])',
    ),
    "CLI: complete an Azure DevOps pull request": (
        r"--auto-complete\b|--status[\"']?(?:\s*,\s*[\"']|\s+)completed\b",
        'run(["az", "repos", "pr", "update", "--status", "completed"])',
    ),
}
PATTERNS = {name: re.compile(pattern) for name, (pattern, _) in MERGE_APIS.items()}
# A pattern listed here only counts in files that also match the context:
# "status": "completed" is also the normal state of check runs and builds.
CONTEXT = {"Azure DevOps: complete a pull request": re.compile(r"(?i)pullrequests")}
# Reviewed exceptions: (path, API name) -> why it is not a merge. Never reword
# code to dodge a pattern; add an entry here instead.
ALLOWED: dict[tuple[str, str], str] = {}
NOT_MERGES = (
    'f"/projects/{pid}/merge_requests/{iid}/notes"',
    "await gitlab.list_merge_requests(project_id)",
    'f"/projects/{pid}/merge_requests/{iid}/merge_ref"',
    '{"merge_request_iid": iid, "status": "in_progress"}',
    '{"name": "ci", "status": "completed", "conclusion": "success"}',
    'source_branch = f"refs/pull/{number}/merge"',
    're.fullmatch(r"refs/merge-requests/(\\d+)/merge", ref)',
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


def merge_apis_in(text: str) -> list[str]:
    return [
        name
        for name, pattern in PATTERNS.items()
        if pattern.search(text) and (name not in CONTEXT or CONTEXT[name].search(text))
    ]


def _mappings(node: Any) -> Iterator[dict[str, Any]]:
    """Every mapping in a parsed YAML document, depth first."""
    if isinstance(node, dict):
        yield node
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            yield from _mappings(child)


def _source_files() -> list[Path]:
    return sorted(
        p
        for root in [REPO / "src", *REPO.glob("packages/*/src")]
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}
    )


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


@pytest.mark.parametrize("name", sorted(MERGE_APIS))
def test_each_pattern_recognises_its_merge_call(name: str):
    assert name in merge_apis_in(MERGE_APIS[name][1])


@pytest.mark.parametrize("sample", NOT_MERGES)
def test_ordinary_merge_request_calls_are_not_flagged(sample: str):
    assert merge_apis_in(sample) == []


@pytest.mark.parametrize("path", _source_files(), ids=_rel)
def test_source_contains_no_merge_call(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    hits = [name for name in merge_apis_in(text) if (_rel(path), name) not in ALLOWED]
    assert hits == [], f"{_rel(path)} uses a merge API: {hits}"


@pytest.mark.parametrize("path", LANE_TEMPLATES, ids=_rel)
def test_lane_template_disables_push(path: Path):
    assert "git remote set-url --push origin FORBIDDEN" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", GITHUB_TEMPLATES, ids=_rel)
def test_github_lane_token_is_read_only(path: Path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    checkouts = [
        m for m in _mappings(doc) if str(m.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts, "no actions/checkout step found"
    assert all((m.get("with") or {}).get("persist-credentials") is False for m in checkouts)
    for name, job in doc["jobs"].items():
        permissions = job.get("permissions", doc.get("permissions"))
        if permissions == "read-all":
            continue
        assert isinstance(permissions, dict), f"job {name} does not pin its token permissions"
        assert set(permissions.values()) <= READ_ONLY, f"job {name}: {permissions}"


@pytest.mark.parametrize("path", AZURE_TEMPLATES, ids=_rel)
def test_azure_lane_holds_no_access_token(path: Path):
    text = path.read_text(encoding="utf-8")
    checkouts = [m for m in _mappings(yaml.safe_load(text)) if "checkout" in m]
    assert checkouts, "no checkout step found"
    assert all(m.get("persistCredentials") is False for m in checkouts)
    assert not re.search(r"system[._]accesstoken", text, re.IGNORECASE)


@pytest.mark.parametrize(
    "relative",
    [".github/workflows/lane.yml", "templates/github/forge-lane.yml", "ci/lane.gitlab-ci.yml"],
)
def test_v1_lanes_hold_no_repository_write_credential(relative):
    path = REPO / relative
    doc = yaml.safe_load(path.read_text())
    text = path.read_text()
    assert "GITLAB_TOKEN" not in text and "GH_TOKEN" not in text
    if relative.endswith("gitlab-ci.yml"):
        job = doc["forge-lane-v1"]
        assert job["id_tokens"]["FORGE_OIDC_TOKEN"]["aud"] == "$FORGE_PUBLIC_URL"
        assert "git remote set-url --push origin FORBIDDEN" in text
        assert "unset CI_JOB_TOKEN CI_REPOSITORY_URL" in text
        return
    assert doc["permissions"] == {"contents": "read", "id-token": "write"}
    job = doc["jobs"]["lane"]
    assert "permissions" not in job
    if "uses" in job:
        assert job["uses"] == "forcewake/forge/.github/workflows/lane.yml@v1"
        assert "secrets: inherit" not in text
        return
    checkouts = [
        m for m in _mappings(job) if str(m.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts and all(
        (m.get("with") or {}).get("persist-credentials") is False for m in checkouts
    )
    assert "git remote set-url --push origin FORBIDDEN" in text
