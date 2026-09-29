"""I1 — the bot never merges (ADR-0003).

Two mechanical checks: no provider client or writer contains a merge API
call, and every lane template disables push and grants no write access.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROVIDER_SOURCES = (
    "src/forge/gitlab",
    "src/forge/integrations",
    "src/forge/repository",
    "src/forge/providers",
)
MERGE_PATTERNS = (
    re.compile(r"/merge_requests/[^\"'\s]*/merge\b"),  # GitLab: accept a merge request
    re.compile(r"\bmerge_when_pipeline_succeeds\b"),
    re.compile(r"\bauto_merge\b"),
    re.compile(r"/pulls/[^\"'\s]*/merge\b"),  # GitHub REST: merge a pull request
    re.compile(r"\bmergePullRequest\b"),
    re.compile(r"\benablePullRequestAutoMerge\b"),
    re.compile(r"\bautoCompleteSetBy\b"),  # Azure DevOps auto-complete
    re.compile(r"\bcompletionOptions\b"),
)
LANE_TEMPLATES = sorted(
    [
        *REPO.glob("ci/templates/*.yml"),
        REPO / ".github/workflows/forge-harness.yml",
        REPO / "forge-lane.azure-pipelines.yml",
    ]
)
GITHUB_TEMPLATES = [p for p in LANE_TEMPLATES if "github" in p.name or p.parent.name == "workflows"]


def _provider_files() -> list[Path]:
    return sorted(
        path
        for base in PROVIDER_SOURCES
        if (REPO / base).is_dir()
        for path in (REPO / base).rglob("*.py")
    )


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def test_the_scanner_recognises_merge_calls():
    samples = (
        'await self._put(f"/projects/{pid}/merge_requests/{iid}/merge")',
        'await self._put(f"/repos/{repo}/pulls/{number}/merge")',
        "mutation { mergePullRequest(input: $input) { clientMutationId } }",
    )
    assert all(any(p.search(s) for p in MERGE_PATTERNS) for s in samples)


@pytest.mark.parametrize("path", _provider_files(), ids=_rel)
def test_provider_code_contains_no_merge_call(path: Path):
    text = path.read_text(encoding="utf-8")
    hits = [p.pattern for p in MERGE_PATTERNS if p.search(text)]
    assert hits == [], f"{_rel(path)} uses a merge API: {hits}"


@pytest.mark.parametrize("path", LANE_TEMPLATES, ids=_rel)
def test_lane_template_disables_push_and_grants_no_write(path: Path):
    text = path.read_text(encoding="utf-8")
    assert "git remote set-url --push origin FORBIDDEN" in text
    assert not re.search(r"contents:\s*write", text)


@pytest.mark.parametrize("path", GITHUB_TEMPLATES, ids=_rel)
def test_github_lane_does_not_persist_checkout_credentials(path: Path):
    assert "persist-credentials: false" in path.read_text(encoding="utf-8")
