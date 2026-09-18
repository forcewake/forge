"""Tests for the ChangeSet contract and trusted validation (ADR-0001)."""

import pytest

from forge.repository.changeset import (
    BUILTIN_WRITE_PROFILES,
    DEFAULT_WRITE_PROFILE,
    DENIED_PATHS,
    MAX_CHANGES,
    Change,
    ChangeSet,
    MaterializationError,
    Operation,
    changeset_from_document,
    changeset_to_document,
    is_lockfile,
    materialize,
    normalize_repo_path,
    resolve_write_policy,
    validate_changeset,
)


def _cs(*changes: Change, message: str = "forge: implement 7 (run abcd1234)") -> ChangeSet:
    return ChangeSet(branch="factory/7/abcd1234", commit_message=message, changes=list(changes))


def _create(path: str = "forge-demo/run-abcd1234.md", content: str = "# hello") -> Change:
    return Change(path=path, operation=Operation.CREATE, content=content)


class TestValidChangesets:
    def test_minimal_valid_changeset(self):
        assert validate_changeset(_cs(_create())) == []

    def test_update_without_content_is_allowed_in_m1(self):
        cs = _cs(Change(path="src/app.py", operation=Operation.UPDATE))
        assert validate_changeset(cs) == []

    def test_update_with_full_content_is_allowed(self):
        cs = _cs(Change(path="src/app.py", operation=Operation.UPDATE, content="x = 1\n"))
        assert validate_changeset(cs) == []

    def test_delete_without_content_is_allowed(self):
        cs = _cs(Change(path="src/old.py", operation=Operation.DELETE))
        assert validate_changeset(cs) == []

    def test_multiple_valid_changes(self):
        cs = _cs(
            _create("forge-demo/a.md"),
            _create("forge-demo/b.md"),
            Change(path="src/old.py", operation=Operation.DELETE),
        )
        assert validate_changeset(cs) == []


class TestDenylist:
    def test_gitlab_ci_file_denied(self):
        violations = validate_changeset(_cs(_create(".gitlab-ci.yml")))
        assert any(".gitlab-ci.yml" in v for v in violations)

    def test_forge_config_denied(self):
        violations = validate_changeset(_cs(_create(".forge.yml")))
        assert violations

    def test_github_prefix_denied(self):
        violations = validate_changeset(_cs(_create(".github/workflows/ci.yml")))
        assert violations

    @pytest.mark.parametrize(
        "path",
        [
            "web/package-lock.json",
            "yarn.lock",
            "Cargo.lock",
            "poetry.lock",
            "uv.lock",
            "sub/dir/Gemfile.lock",
        ],
    )
    def test_well_known_lockfiles_denied(self, path):
        violations = validate_changeset(_cs(_create(path)))
        assert any("lockfile" in v for v in violations)

    def test_any_dotlock_suffix_denied(self):
        violations = validate_changeset(_cs(_create("vendor/thing.lock")))
        assert violations

    def test_is_lockfile_helper(self):
        assert is_lockfile("a/b/uv.lock")
        assert not is_lockfile("src/lock.py")

    def test_denylist_constants_are_module_level(self):
        # Tests (and future policy config) monkeypatch these constants.
        assert ".gitlab-ci.yml" in DENIED_PATHS
        assert MAX_CHANGES == 20

    def test_monkeypatched_limit_is_respected(self, monkeypatch):
        from forge.repository import changeset

        monkeypatch.setattr(changeset, "MAX_CHANGES", 2)
        violations = validate_changeset(_cs(_create("a.md"), _create("b.md")))
        assert violations == []


class TestPathSafety:
    def test_path_traversal_denied(self):
        violations = validate_changeset(_cs(_create("../../etc/passwd")))
        assert any("traversal" in v for v in violations)

    def test_nested_traversal_denied(self):
        violations = validate_changeset(_cs(_create("src/../../escape.txt")))
        assert violations

    def test_absolute_path_denied(self):
        violations = validate_changeset(_cs(_create("/etc/passwd")))
        assert any("absolute" in v for v in violations)

    def test_empty_path_denied(self):
        violations = validate_changeset(_cs(_create("")))
        assert violations


class TestContentRules:
    def test_create_requires_content(self):
        cs = _cs(Change(path="forge-demo/x.md", operation=Operation.CREATE))
        violations = validate_changeset(cs)
        assert any("requires content" in v for v in violations)

    def test_delete_must_not_carry_content(self):
        cs = _cs(Change(path="src/x.py", operation=Operation.DELETE, content="oops"))
        assert violations_of_delete(cs)

    def test_oversized_content_denied(self):
        big = "x" * (256 * 1024 + 1)
        violations = validate_changeset(_cs(_create(content=big)))
        assert any("exceeds" in v for v in violations)

    def test_exactly_256kib_allowed(self):
        big = "x" * (256 * 1024)
        assert validate_changeset(_cs(_create(content=big))) == []


def violations_of_delete(cs: ChangeSet) -> bool:
    return any("must not carry content" in v for v in validate_changeset(cs))


class TestStructuralRules:
    def test_empty_changes_denied(self):
        violations = validate_changeset(_cs())
        assert any("at least one change" in v for v in violations)

    def test_empty_commit_message_denied(self):
        violations = validate_changeset(_cs(_create(), message="   "))
        assert any("commit_message" in v for v in violations)

    def test_more_than_20_changes_denied(self):
        changes = [_create(f"forge-demo/f{i}.md") for i in range(21)]
        violations = validate_changeset(_cs(*changes))
        assert any("21 changes" in v for v in violations)

    def test_exactly_20_changes_allowed(self):
        changes = [_create(f"forge-demo/f{i}.md") for i in range(20)]
        assert validate_changeset(_cs(*changes)) == []

    def test_all_violations_reported_at_once(self):
        cs = ChangeSet(
            branch="factory/7/abcd1234",
            commit_message="",
            changes=[
                Change(path=".gitlab-ci.yml", operation=Operation.CREATE, content="x"),
                Change(path="../escape", operation=Operation.CREATE),
            ],
        )
        violations = validate_changeset(cs)
        assert len(violations) >= 3  # message + denylist + traversal + missing content


class TestPathScope:
    """Monorepo path scoping (v0.7, complex-projects.md §1): allowed_paths."""

    def test_in_scope_change_passes(self):
        violations = validate_changeset(
            _cs(_create("services/api/handler.py")), allowed_paths=["services/**"]
        )
        assert violations == []

    def test_out_of_scope_change_rejected_with_reason(self):
        violations = validate_changeset(
            _cs(_create("webapp/ui/button.tsx")), allowed_paths=["services/**"]
        )
        assert len(violations) == 1
        assert "outside the allowed scope" in violations[0]
        assert "services/**" in violations[0]
        assert "webapp/ui/button.tsx" in violations[0]

    def test_scope_covers_nested_files(self):
        # fnmatch semantics: `*` spans `/`, so a dir glob covers the subtree.
        violations = validate_changeset(
            _cs(_create("services/api/v1/deep/file.py")), allowed_paths=["services/api/*"]
        )
        assert violations == []

    def test_every_change_must_be_in_scope(self):
        cs = _cs(_create("services/a.py"), _create("services/b.py"), _create("other/c.py"))
        violations = validate_changeset(cs, allowed_paths=["services/**"])
        assert len(violations) == 1
        assert "other/c.py" in violations[0]

    def test_unscoped_changesets_are_unchanged(self):
        # None (legacy callers) and [] both mean "the whole repo is in scope".
        cs = _cs(_create("anything/anywhere.py"))
        assert validate_changeset(cs, allowed_paths=None) == []
        assert validate_changeset(cs, allowed_paths=[]) == []

    def test_scope_composes_with_the_denylist(self):
        # In-scope does not mean allowed: the global denylist still wins.
        violations = validate_changeset(
            _cs(_create("services/package-lock.json")), allowed_paths=["services/**"]
        )
        assert any("lockfiles are denylisted" in v for v in violations)


class TestPathNormalization:
    """R18: deny/scope checks run on the canonical spelling — equivalent
    spellings of a path must not bypass (or spuriously fail) the policy."""

    @pytest.mark.parametrize(
        "spelling,canonical",
        [
            ("src/app.py", "src/app.py"),
            ("./src/app.py", "src/app.py"),
            ("src//app.py", "src/app.py"),
            ("src/./app.py", "src/app.py"),
            ("src\\app.py", "src/app.py"),
            ("web\\./package-lock.json", "web/package-lock.json"),
        ],
    )
    def test_equivalent_spellings_compare_equal(self, spelling, canonical):
        assert normalize_repo_path(spelling) == canonical

    @pytest.mark.parametrize(
        "path",
        [
            ".gitlab-ci.yml",
            "./.gitlab-ci.yml",
            ".//.gitlab-ci.yml",
            ".github/./workflows/pwn.yml",
            "./.github/workflows/pwn.yml",
            "web\\package-lock.json",
            "./web/package-lock.json",
            "sub/dir//uv.lock",
        ],
    )
    def test_denylist_bypass_by_spelling_is_blocked(self, path):
        violations = validate_changeset(_cs(_create(path)))
        assert violations, path

    def test_traversal_by_backslash_spelling_is_blocked(self):
        # "..\\..\\escape" has no ".." '/'-segment — only the canonical form
        # exposes the traversal.
        violations = validate_changeset(_cs(_create("..\\..\\escape.txt")))
        assert any("traversal" in v for v in violations)

    def test_traversal_is_never_resolved_into_a_legal_path(self):
        violations = validate_changeset(_cs(_create("src/../escape.txt")))
        assert any("traversal" in v for v in violations)

    def test_scope_check_runs_on_the_canonical_spelling(self):
        # "./services/a.py" IS in scope — a spelling must not fail it either.
        violations = validate_changeset(
            _cs(_create("./services/a.py")), allowed_paths=["services/**"]
        )
        assert violations == []

    def test_scope_escape_by_spelling_is_blocked(self):
        violations = validate_changeset(
            _cs(_create("./webapp/x.ts")), allowed_paths=["services/**"]
        )
        assert any("outside the allowed scope" in v for v in violations)

    def test_canonical_paths_keep_the_exact_historical_verdicts(self):
        # The normalization is invisible for already-canonical paths.
        assert validate_changeset(_cs(_create("src/app.py"))) == []
        assert validate_changeset(_cs(_create("/etc/passwd")))  # absolute
        assert validate_changeset(_cs(_create("docs/./note.md"))) == []


class TestDuplicatePaths:
    """R18: two entries for one path are rejected — in materialization and
    in validation — even when the spellings differ."""

    def test_duplicate_create_paths_violate(self):
        cs = _cs(_create("forge-demo/a.md"), _create("forge-demo/a.md"))
        violations = validate_changeset(cs)
        assert any("duplicate path" in v for v in violations)

    def test_spelling_variant_duplicate_is_still_a_duplicate(self):
        cs = _cs(_create("forge-demo/a.md"), _create("./forge-demo//a.md"))
        violations = validate_changeset(cs)
        assert any("duplicate path" in v for v in violations)
        assert any("'forge-demo/a.md'" in v for v in violations)

    def test_distinct_paths_stay_allowed(self):
        cs = _cs(_create("forge-demo/a.md"), _create("forge-demo/b.md"))
        assert validate_changeset(cs) == []

    def test_materialize_rejects_duplicate_paths(self):
        with pytest.raises(MaterializationError, match="duplicate path"):
            materialize(
                {
                    "branch": "b",
                    "commit_message": "m",
                    "changes": [
                        {"path": "a.md", "operation": "create", "content": "1"},
                        {"path": "./a.md", "operation": "create", "content": "2"},
                    ],
                },
                {},
            )

    def test_materialize_accepts_distinct_paths(self):
        cs = materialize(
            {
                "branch": "b",
                "commit_message": "m",
                "changes": [
                    {"path": "a.md", "operation": "create", "content": "1"},
                    {"path": "b.md", "operation": "create", "content": "2"},
                ],
            },
            {},
        )
        assert len(cs.changes) == 2


class TestWriteProfileMatrix:
    """R18: the four built-in profiles allow/deny exactly per spec. The
    default profile IS today's behavior (zero regression)."""

    #: (path, {profiles where the path is ALLOWED})
    MATRIX = [
        ("src/app.py", set(BUILTIN_WRITE_PROFILES)),
        # Manifests are never denied today (only lockfiles are) — but they
        # are not source either, so code_only excludes them.
        ("pyproject.toml", {"no_dependencies", "dependency_update", "ci_change"}),
        ("docs/readme.md", {"no_dependencies", "dependency_update", "ci_change"}),
        ("package-lock.json", {"dependency_update"}),
        ("web/Cargo.lock", {"dependency_update"}),
        ("sub/dir/uv.lock", {"dependency_update"}),
        (".gitlab-ci.yml", {"ci_change"}),
        (".github/workflows/ci.yml", {"ci_change"}),
        (".forge.yml", set()),  # forge's own config: denied under EVERY builtin
    ]

    def test_default_profile_is_no_dependencies(self):
        assert DEFAULT_WRITE_PROFILE == "no_dependencies"
        policy = resolve_write_policy(None)
        assert policy.name == "no_dependencies"
        assert policy.require_special_approval is False
        assert policy.deny_lockfiles is True

    @pytest.mark.parametrize("profile", BUILTIN_WRITE_PROFILES)
    @pytest.mark.parametrize("path,allowed_in", MATRIX)
    def test_matrix_verdict(self, profile, path, allowed_in):
        violations = validate_changeset(_cs(_create(path)), policy=resolve_write_policy(profile))
        assert (not violations) == (profile in allowed_in), (profile, path, violations)

    def test_dependency_update_allows_the_lockfile_the_default_denies(self):
        lockfile = _cs(_create("package-lock.json", "{}\n"))
        assert validate_changeset(lockfile)  # no_dependencies: denied
        assert validate_changeset(lockfile, policy=resolve_write_policy("dependency_update")) == []

    def test_dependency_update_still_denies_ci_and_forge_config(self):
        policy = resolve_write_policy("dependency_update")
        for path in (".gitlab-ci.yml", ".forge.yml", ".github/workflows/x.yml"):
            assert validate_changeset(_cs(_create(path)), policy=policy), path

    def test_code_only_is_a_source_allowlist(self):
        policy = resolve_write_policy("code_only")
        assert validate_changeset(_cs(_create("deep/nested/module.py")), policy=policy) == []
        violations = validate_changeset(_cs(_create("docs/design.md")), policy=policy)
        assert any("outside the 'code_only' write profile" in v for v in violations)

    def test_ci_change_requires_special_approval_flag(self):
        assert resolve_write_policy("ci_change").require_special_approval is True

    def test_unknown_profile_fails_closed(self):
        with pytest.raises(ValueError, match="unknown write profile"):
            resolve_write_policy("yolo")

    def test_custom_profiles_extend_the_base_tighten_only(self):
        custom = {
            "vendor_locked": {
                "denied_paths": ["vendor/**"],
                "allowed_paths": ["vendor/public.txt"],
                "require_special_approval": True,
            }
        }
        policy = resolve_write_policy("vendor_locked", custom_profiles=custom)
        assert validate_changeset(_cs(_create("src/app.py")), policy=policy) == []
        # allowed_paths exempts from the profile's own deny globs...
        assert validate_changeset(_cs(_create("vendor/public.txt")), policy=policy) == []
        # ...but not from the base denies (forge config) or lockfiles.
        assert validate_changeset(_cs(_create("vendor/uv.lock")), policy=policy)
        assert validate_changeset(_cs(_create("vendor/.forge.yml")), policy=policy)
        # the profile's own glob deny, and the operator gate flag:
        internal = validate_changeset(_cs(_create("vendor/internal.py")), policy=policy)
        assert any("denylisted pattern" in v for v in internal)
        assert policy.require_special_approval is True

    def test_custom_profile_cannot_shadow_a_builtin(self):
        with pytest.raises(ValueError, match="shadows a built-in"):
            resolve_write_policy("no_dependencies", custom_profiles={"no_dependencies": {}})

    def test_extra_denied_paths_apply_under_every_profile(self):
        # Azure-style pipeline entrypoints: sensitive under ALL profiles.
        for profile in BUILTIN_WRITE_PROFILES:
            policy = resolve_write_policy(profile, extra_denied_paths=["ci/build.yml"])
            violations = validate_changeset(_cs(_create("ci/build.yml")), policy=policy)
            assert any("protected pipeline entrypoint" in v for v in violations), profile

    def test_entrypoint_is_denied_even_where_ci_change_permits(self):
        # A project whose GitHub workflow file IS its pipeline entrypoint:
        # ci_change ordinarily permits .github/* — the entrypoint stays
        # protected regardless.
        policy = resolve_write_policy(
            "ci_change", extra_denied_paths=[".github/workflows/deploy.yml"]
        )
        assert validate_changeset(_cs(_create(".github/workflows/ci.yml")), policy=policy) == []
        violations = validate_changeset(_cs(_create(".github/workflows/deploy.yml")), policy=policy)
        assert any("protected pipeline entrypoint" in v for v in violations)

    def test_builtins_read_the_live_module_denylist(self, monkeypatch):
        # The historical monkeypatch contract: module constants shape the
        # default policy.
        from forge.repository import changeset

        monkeypatch.setattr(changeset, "DENIED_PATHS", frozenset({"forbidden.txt"}))
        assert validate_changeset(_cs(_create("forbidden.txt")))


class TestDocumentRoundTrip:
    """The durable attempt manifest: ChangeSet -> JSON document -> ChangeSet."""

    def test_round_trip_preserves_the_changeset(self):
        cs = ChangeSet(
            branch="factory/7/abcd1234",
            commit_message="forge: implement 7 (run abcd1234)",
            changes=[
                _create("forge-demo/new.md", "# created\n"),
                Change(path="src/app.py", operation=Operation.UPDATE, content="x = 2\n"),
                Change(path="src/old.py", operation=Operation.DELETE, content=None),
            ],
            attempt_base_oid="candidate-sha-9",
        )
        assert changeset_from_document(changeset_to_document(cs)) == cs

    def test_delete_content_stays_none_not_missing(self):
        document = changeset_to_document(_cs(Change(path="src/old.py", operation=Operation.DELETE)))
        assert document["changes"][0]["content"] is None

    def test_non_documents_are_not_resumable(self):
        assert changeset_from_document(None) is None
        assert changeset_from_document("changeset") is None
        assert changeset_from_document({"branch": "b"}) is None

    def test_malformed_changes_are_not_resumable(self):
        base = {"branch": "b", "commit_message": "m", "attempt_base_oid": "base"}
        assert changeset_from_document({**base, "changes": []}) is None
        assert changeset_from_document({**base, "changes": "nope"}) is None
        assert changeset_from_document({**base, "changes": [{"path": "p"}]}) is None
        rename = [{"path": "p", "operation": "rename", "content": "x"}]
        assert changeset_from_document({**base, "changes": rename}) is None
