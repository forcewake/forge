"""Candidate, usage and authority contracts on their real product owners.

The standalone checker is retired. The archive collector, publisher and
usage parser enforce these rules on the path that accepts lane output.
"""

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from forge.execution.github_actions import CandidateArchiveError, _extract_candidate
from forge.runs.backends import CITharnessBackend
from forge.runs.candidate import (
    CandidateBundle,
    CandidateError,
    CreateFile,
    HarnessUsage,
    attempt_base_for,
    parse_unified_diff,
)
from forge.runs.publisher import PolicyViolation, validate_candidate_bundle
from forge.repository.changeset import WritePolicy, resolve_write_policy

BASE = "a" * 40
OTHER = "b" * 40
DIFF = "diff --git a/src/new.py b/src/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+VALUE = 1\n"


def archive(meta=None, *, diff=DIFF, extras=()):
    document = {
        "schema_version": 2,
        "manifest_digest": "sha256:" + hashlib.sha256(diff.encode()).hexdigest(),
        "attempt_base": BASE,
        "driver": "claude-code",
        "model": "test-model",
        "exit": "completed",
        "usage": None,
    }
    document.update(meta or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        zipped.writestr("candidate.diff", diff)
        zipped.writestr("candidate.meta.json", json.dumps(document))
        for name, value in extras:
            zipped.writestr(name, value)
    return output.getvalue()


@pytest.mark.parametrize("schema", [1, 2])
def test_conflicting_attempt_base_aliases_are_rejected_by_the_real_collector(schema):
    with pytest.raises(CandidateArchiveError, match="base"):
        _extract_candidate(archive({"schema_version": schema, "attempt_base_oid": OTHER}))


@pytest.mark.parametrize(
    "meta",
    [
        {"attempt_base": BASE, "attempt_base_oid": OTHER},
        {"attempt_base": OTHER},
    ],
)
async def test_the_gitlab_collector_refuses_conflicting_or_untrusted_base_before_fetching_diff(
    meta,
):
    client = SimpleNamespace(
        get_job_artifacts_file=AsyncMock(return_value=json.dumps(meta).encode())
    )
    backend = CITharnessBackend(gitlab=client, writer=None, settings=None)
    run = SimpleNamespace(commit_cycle=1, base_sha=BASE, candidate_shas=[])
    result = await backend._collect_candidate(7, SimpleNamespace(id=9), run)
    assert result.status == "failed" and result.failure_kind == "code"
    assert "attempt_base_mismatch" in result.reason
    client.get_job_artifacts_file.assert_awaited_once()


@pytest.mark.parametrize("completeness", ["exact", "aggregate"])
def test_zeroed_complete_usage_is_unknown_not_measured_zero(completeness):
    raw = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "completeness": completeness,
    }
    usage = HarnessUsage.from_meta(raw)
    assert usage.completeness == "unknown"
    assert usage.total_known_tokens is None
    assert usage.raw == raw


@pytest.mark.parametrize(
    "raw",
    [None, {}, {"completeness": "exact"}, {"input_tokens": -1}, {"input_tokens": True}, "invalid"],
)
def test_unmeasured_or_invalid_usage_does_not_invent_tokens(raw):
    usage = HarnessUsage.from_meta(raw)
    assert usage.completeness == "unknown"
    assert usage.total_known_tokens is None


def test_fully_cached_usage_keeps_its_observed_positive_counters():
    usage = HarnessUsage.from_meta(
        {"input_tokens": 0, "cached_input_tokens": 900, "output_tokens": 50}, driver="claude-code"
    )
    assert usage.cached_input_tokens == 900
    assert usage.total_known_tokens == 950


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        "/absolute.py",
        "C:\\escape.py",
        ".forge/authority.json",
        ".github/workflows/rewrite.yml",
    ],
)
def test_unsafe_candidate_paths_fail_at_the_actual_write_boundary(path):
    bundle = CandidateBundle(
        attempt_base_oid=BASE, driver_exit="completed", entries=(CreateFile(path, "x\n"),)
    )
    with pytest.raises((PolicyViolation, CandidateError)):
        validate_candidate_bundle(
            bundle, base_contents={}, branch="forge/7/test", commit_message="candidate"
        )


def test_duplicate_candidate_paths_are_refused_before_materialization():
    entry = CreateFile("src/new.py", "x\n")
    bundle = CandidateBundle(attempt_base_oid=BASE, driver_exit="completed", entries=(entry, entry))
    with pytest.raises(CandidateError, match="duplicate"):
        validate_candidate_bundle(
            bundle, base_contents={}, branch="forge/7/test", commit_message="candidate"
        )


def test_metadata_cannot_expand_the_frozen_publication_scope():
    diff, meta = _extract_candidate(
        archive({"allowed_paths": ["**"], "publication_authorized": True})
    )
    assert meta["publication_authorized"] is True
    bundle = parse_unified_diff(diff, attempt_base_oid=BASE)
    with pytest.raises(PolicyViolation):
        validate_candidate_bundle(
            bundle,
            base_contents={},
            branch="forge/7/test",
            commit_message="candidate",
            allowed_paths=("docs/**",),
        )


@pytest.mark.parametrize("schema", [0, 3, True, "future"])
def test_foreign_candidate_schema_is_refused(schema):
    with pytest.raises(CandidateArchiveError):
        _extract_candidate(archive({"schema_version": schema}))


def test_substituted_diff_bytes_cannot_reuse_a_manifest_digest():
    with pytest.raises(CandidateArchiveError, match="manifest_digest"):
        _extract_candidate(archive({"manifest_digest": "sha256:" + "0" * 64}))


@pytest.mark.parametrize("entry", ["../outside.txt", "unknown.txt", "nested/candidate.diff"])
def test_archive_traversal_unexpected_and_duplicate_entries_are_refused(entry):
    with pytest.raises(CandidateArchiveError):
        _extract_candidate(archive(extras=((entry, "substituted"),)))


def test_repair_attempt_uses_the_last_published_candidate_instead_of_the_original_base():
    run = SimpleNamespace(commit_cycle=2, base_sha=BASE, candidate_shas=[OTHER])
    assert attempt_base_for(run) == OTHER
    run.commit_cycle = 1
    assert attempt_base_for(run) == BASE


def test_corrupt_hunk_counts_are_not_silently_accepted():
    with pytest.raises(CandidateError):
        parse_unified_diff(DIFF.replace("@@ -0,0 +1 @@", "@@ -0,0 +1,2 @@"), attempt_base_oid=BASE)


@pytest.mark.parametrize(
    "profile", ["no_dependencies", "code_only", "dependency_update", "ci_change", "custom"]
)
def test_runtime_authority_namespace_is_never_lifted_by_a_write_profile(profile):
    policy = (
        WritePolicy(
            name="custom", denied_paths=frozenset(), denied_prefixes=(), permitted_globs=("**",)
        )
        if profile == "custom"
        else resolve_write_policy(profile)
    )
    bundle = CandidateBundle(
        attempt_base_oid=BASE,
        driver_exit="completed",
        entries=(CreateFile(".forge/authority.json", "{}"),),
    )
    with pytest.raises(PolicyViolation, match="reserved forge runtime namespace"):
        validate_candidate_bundle(
            bundle,
            base_contents={},
            branch="forge/7/test",
            commit_message="candidate",
            policy=policy,
        )
