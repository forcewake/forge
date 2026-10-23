"""Extracted candidate rules retain strict bytes, immutable policy and a pure boundary."""

import ast
import random
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from forge.domain.candidate import CandidateBundle, ValidatedCandidate, validate_candidate_bundle
from forge.domain.candidate_parser import parse_unified_diff
from forge.domain.change_types import Change, ChangeSet, Operation
from forge.domain.patch import DiffHunk, HunkLine, apply_unified_hunks
from forge.domain.paths import (
    PathPolicy,
    normalize_repo_path,
    resolve_write_policy,
    validate_changeset,
)
from forge.domain.revival import MAX_BACKOFF_SECONDS, backoff_seconds, classify_terminal_failure


OWNERS = (
    "candidate",
    "candidate_parser",
    "candidate_usage",
    "change_types",
    "changes",
    "patch",
    "paths",
    "revival",
)


def test_extracted_domain_has_no_legacy_or_io_imports():
    root = Path(__file__).resolve().parents[1] / "src/forge/domain"
    forbidden = {
        "asyncio",
        "os",
        "sys",
        "pathlib",
        "socket",
        "subprocess",
        "httpx",
        "sqlalchemy",
        "redis",
    }
    for name in OWNERS:
        tree = ast.parse((root / (name + ".py")).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            for module in modules:
                assert module.split(".")[0] not in forbidden, (name, module)
                assert not module.startswith("forge.") or module.startswith("forge.domain."), (
                    name,
                    module,
                )


def test_path_policy_copies_mutable_constructor_inputs():
    denied, prefixes, permitted = {"private.txt"}, [".github/"], ["docs/*"]
    policy = PathPolicy(
        name="frozen", denied_paths=denied, denied_prefixes=prefixes, permitted_globs=permitted
    )
    denied.clear()
    prefixes.clear()
    permitted.append("*")
    assert policy.denied_paths == frozenset({"private.txt"})
    assert policy.denied_prefixes == (".github/",) and policy.permitted_globs == ("docs/*",)
    with pytest.raises(FrozenInstanceError):
        policy.deny_lockfiles = False
    with pytest.raises(TypeError):
        PathPolicy(name="closed", denied_paths=frozenset(), denied_prefixes=(), unknown=True)


def test_legacy_adapters_use_the_actual_domain_owners():
    from forge.repository.changeset import ChangeSet as LegacyChanges
    from forge.repository.changeset import WritePolicy as LegacyPolicy
    from forge.runs.candidate import CandidateBundle as LegacyBundle
    from forge.runs.candidate import parse_unified_diff as legacy_parse
    from forge.runs.publisher import ValidatedCandidate as LegacyValidated
    from forge.runs.publisher import validate_candidate_bundle as legacy_validate
    from forge.runs.revival import classify_terminal_failure as legacy_classify

    assert LegacyPolicy is PathPolicy and LegacyChanges is ChangeSet
    assert LegacyBundle is CandidateBundle and LegacyValidated is ValidatedCandidate
    assert legacy_parse is parse_unified_diff and legacy_validate is validate_candidate_bundle
    assert legacy_classify is classify_terminal_failure


def test_strict_apply_preserves_unmodified_bytes_across_generated_hunks():
    rng = random.Random(680)
    for case in range(400):
        crlf, trailing = bool(case % 2), bool((case // 2) % 2)
        lines = [f"line-{index}-λ\u2028\x85" for index in range(rng.randint(1, 25))]
        index = rng.randrange(len(lines))
        original, replacement = lines[index], f"new-{case}-é"
        suffix = "\r" if crlf else ""
        old_body = [line + suffix for line in lines]
        no_newline = index == len(lines) - 1 and not trailing
        hunk = DiffHunk(
            index + 1,
            1,
            index + 1,
            1,
            (
                HunkLine("-", original + suffix, no_newline),
                HunkLine("+", replacement + suffix, no_newline),
            ),
        )
        base = "\n".join(old_body) + ("\n" if trailing else "")
        old_body[index] = replacement + suffix
        expected = "\n".join(old_body) + ("\n" if trailing else "")
        assert apply_unified_hunks(base, (hunk,)).encode() == expected.encode()


@pytest.mark.parametrize(
    "profile", ["no_dependencies", "code_only", "dependency_update", "ci_change"]
)
def test_path_canonicalization_cannot_bypass_profile_or_sensitive_denies(profile):
    policy = resolve_write_policy(profile, extra_denied_paths=("ci/lane.yml",))
    paths = (".forge.yml", "ci/lane.yml", ".forge/key", "../escape.py", "src/a.py", "uv.lock")
    for path in paths:
        spellings = (path, "./" + path, path.replace("/", "//"), path.replace("/", "\\"))
        results = []
        for spelling in spellings:
            change = Change(spelling, Operation.CREATE, "new")
            results.append(
                bool(
                    validate_changeset(ChangeSet("factory/x", "approved", [change]), policy=policy)
                )
            )
            assert normalize_repo_path(spelling) == normalize_repo_path(path)
        assert len(set(results)) == 1
    protected = ChangeSet("factory/x", "approved", [Change("ci/lane.yml", Operation.CREATE, "new")])
    assert any(
        "protected pipeline entrypoint" in v for v in validate_changeset(protected, policy=policy)
    )


def test_backoff_is_bounded_and_monotone_even_for_huge_attempt_counts():
    for base in (-100, 0, 1, 30, 60, 901):
        delays = [backoff_seconds(attempt, base) for attempt in range(25)]
        assert delays == sorted(delays)
        assert all(1 <= delay <= MAX_BACKOFF_SECONDS for delay in delays)
        for attempt in range(20):
            assert delays[attempt] == min(max(base, 1) * 2**attempt, MAX_BACKOFF_SECONDS)
        assert backoff_seconds(10**9, base) == MAX_BACKOFF_SECONDS
        assert backoff_seconds(-10, base) == min(max(base, 1), MAX_BACKOFF_SECONDS)


def test_validated_publication_actions_cannot_change_after_validation():
    from forge.domain.candidate import bundle_from_changeset

    actions = [Change("src/safe.py", Operation.CREATE, "approved\n")]
    draft = ChangeSet("factory/approved", "approved change", actions)
    candidate = validate_candidate_bundle(
        bundle_from_changeset(draft, attempt_base_oid="a" * 40),
        base_contents={},
        branch=draft.branch,
        commit_message=draft.commit_message,
        allowed_paths=("src/*",),
        policy=resolve_write_policy(),
    )
    actions.append(Change(".github/workflows/attack.yml", Operation.CREATE, "unapproved"))
    assert draft.changes == (Change("src/safe.py", Operation.CREATE, "approved\n"),)
    assert candidate.changeset.changes == draft.changes
    with pytest.raises(AttributeError):
        candidate.changeset.changes.append(actions[-1])
    with pytest.raises(FrozenInstanceError):
        candidate.changeset.changes += (actions[-1],)
    assert tuple(c.path for c in candidate.changeset.changes) == ("src/safe.py",)


def test_candidate_hunks_and_validated_scope_snapshot_constructor_collections():
    from forge.domain.candidate import ChangeManifestEntry, UnifiedPatch

    lines = [HunkLine("-", "old"), HunkLine("+", "approved")]
    hunk = DiffHunk(1, 1, 1, 1, lines)
    hunks = [hunk]
    entry = UnifiedPatch("src/safe.py", hunks)
    entries = [entry]
    bundle = CandidateBundle("a" * 40, "completed", entries)
    candidate = validate_candidate_bundle(
        bundle,
        base_contents={"src/safe.py": "old\n"},
        branch="factory/approved",
        commit_message="approved change",
        policy=resolve_write_policy(),
    )
    manifests = [ChangeManifestEntry("src/safe.py", "modify", "approved\n", hunks=hunks)]
    scope = ["src/*"]
    wrapped = ValidatedCandidate(bundle, manifests, candidate.changeset, scope)
    lines.clear()
    hunks.clear()
    entries.clear()
    manifests.clear()
    scope.append("*")
    assert wrapped.allowed_paths == ("src/*",)
    assert len(wrapped.manifest) == len(wrapped.bundle.entries) == 1
    assert len(wrapped.manifest[0].hunks) == len(entry.hunks) == 1
    assert hunk.lines == (HunkLine("-", "old"), HunkLine("+", "approved"))
    assert wrapped.bundle.materialize({"src/safe.py": "old\n"})[0].new_content == "approved\n"


def test_usage_raw_json_cannot_drift_after_receipt_construction():
    from forge.domain.candidate_usage import HarnessUsage, usage_receipt_id

    raw = {
        "input_tokens": 10,
        "cost_usd": 0.5,
        "final": False,
        "unknown": {"nested": ["original", {"keep": True}]},
    }
    usage = HarnessUsage.from_meta(raw)
    receipt = usage_receipt_id("run", "attempt", usage)
    raw["cost_usd"] = 999
    raw["unknown"]["nested"].clear()
    decoded = usage.raw_document()
    assert decoded == {
        "input_tokens": 10,
        "cost_usd": 0.5,
        "final": False,
        "unknown": {"nested": ["original", {"keep": True}]},
    }
    decoded["unknown"]["nested"].clear()
    assert usage.raw_document()["unknown"]["nested"] == ["original", {"keep": True}]
    assert usage_receipt_id("run", "attempt", usage) == receipt
    with pytest.raises(FrozenInstanceError):
        usage.raw_json = "{}"
    for value in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError):
            HarnessUsage.from_meta({"input_tokens": 10, "unrecognized": {"value": value}})
    zero = HarnessUsage.from_meta({"input_tokens": 0, "completeness": "exact", "unknown": "keep"})
    assert zero.completeness == "unknown" and zero.total_known_tokens is None
    assert zero.raw_document()["unknown"] == "keep"
