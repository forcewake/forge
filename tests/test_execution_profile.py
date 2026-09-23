"""The versioned execution profile (review finding A18).

- ``derive_from_repo`` over fixture repos (with and without a lock) — the
  toolchain pins are the target's OWN locked versions (the R15 lane
  extraction, generalized) and the honesty axes degrade to ``unknown``,
  never fabricate.
- Digest stability: the same repo state always yields the same
  ``profile_digest``; any relevant byte change moves it.
- Spec embedding: the digest rides the executable RunSpec's additive
  ``execution_profile`` section; pre-A18 documents parse unchanged.
- Bootstrap classification: a failed environment bootstrap is classified
  infrastructure (blocked, never code repair) — in the lane log, the
  candidate meta and the control plane's log patterns.
"""

import hashlib
import re
from pathlib import Path

import pytest

from forge.gitlab.blob_reads import BlobReadResult
from forge.runs.backends import _HARNESS_INFRASTRUCTURE_PATTERNS
from forge.runs.execution_profile import (
    BOOTSTRAP_STATUS_FAILED,
    COMPILED_COMBINATIONS,
    DRIVER_PROFILE_ALIASES,
    FORGE_BOOTSTRAP_FAILED_MARKER,
    HARNESS_PROFILES,
    INSTALL_PIP_MINIMAL,
    INSTALL_UV_FROZEN,
    LANE_CREDENTIAL_CAPABILITIES,
    LANE_PROFILE_VALUES,
    LANE_PROFILE_V2,
    LANE_PYTHON_VERSION,
    LANE_TEST_COMMANDS,
    LOCK_ABSENT,
    LOCK_LOCKED,
    LOCK_UNKNOWN,
    RUNTIME_RECIPES,
    ExecutionProfile,
    FileRead,
    LocalRepoSource,
    MaterializedFiles,
    RuntimeRecipe,
    bootstrap_failed,
    classify_bootstrap_failure,
    compile_driver_profile,
    credentials_at,
    derive_from_reader,
    derive_from_repo,
    lane_profile,
    resolve_driver_profile,
    runtime_recipe,
    validate_lane_profile_declaration,
    validate_runtime,
)
from forge.runs.spec import ExecutableRunSpec, SpecInvalid

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Fixture repos
# ---------------------------------------------------------------------------

_LOCK_TEXT = """\
version = 1
requires-python = ">=3.13"

[[package]]
name = "ruff"
version = "0.15.7"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pytest"
version = "9.0.2"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "mypy"
version = "1.19.1"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "httpx"
version = "0.28.1"
source = { registry = "https://pypi.org/simple" }
"""

_PYPROJECT_TEXT = """\
[project]
name = "target"
version = "1.0.0"
requires-python = ">=3.13"
"""

_CI_MATCHING_TEXT = """\
jobs:
  test:
    steps:
      - run: uv sync --frozen
      - run: uv run pytest
      - run: uv run ruff check .
      - run: uv run mypy .
"""

_CI_SUBSET_TEXT = """\
jobs:
  build:
    steps:
      - run: npm test
      - run: npm run lint
      - run: uv run pytest
"""


def write_repo(
    tmp_path: Path,
    *,
    lock: str | None = _LOCK_TEXT,
    pyproject: str | None = _PYPROJECT_TEXT,
    ci_path: str = ".github/workflows/ci.yml",
    ci_text: str | None = _CI_MATCHING_TEXT,
) -> Path:
    """A fixture target repo shaped like the ones the lane runs against."""
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject)
    if lock is not None:
        (tmp_path / "uv.lock").write_text(lock)
    if ci_text is not None:
        target = tmp_path / ci_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(ci_text)
    return tmp_path


# ---------------------------------------------------------------------------
# derive_from_repo over fixture repos
# ---------------------------------------------------------------------------


class TestDeriveFromLocalRepo:
    def test_locked_repo_carries_its_own_toolchain_pins(self, tmp_path: Path):
        """The R15 seed, generalized: the pins come from the TARGET's lock —
        the exact versions its own CI enforces, not forge's, not latest."""
        repo = write_repo(tmp_path)

        profile = derive_from_repo(LocalRepoSource(repo))

        assert profile.install_strategy == INSTALL_UV_FROZEN
        assert profile.lock_status == LOCK_LOCKED
        assert profile.lock_sha256 == hashlib.sha256(_LOCK_TEXT.encode()).hexdigest()
        assert profile.toolchain_pins == (
            ("mypy", "1.19.1"),
            ("pytest", "9.0.2"),
            ("ruff", "0.15.7"),
        )
        assert profile.requires_python == ">=3.13"
        assert profile.python_version == LANE_PYTHON_VERSION

    def test_lane_capability_axes_are_recorded_not_implied(self, tmp_path: Path):
        """The approval names the capabilities the lane could see: the gated
        credential set, the MCP sourcing policy and the network posture."""
        profile = derive_from_repo(LocalRepoSource(write_repo(tmp_path)))

        assert profile.lane_commands == LANE_TEST_COMMANDS
        assert "ANTHROPIC_API_KEY" in LANE_CREDENTIAL_CAPABILITIES
        assert "FORGE_GROK_AUTH" in LANE_CREDENTIAL_CAPABILITIES
        assert "COPILOT_GITHUB_TOKEN" in LANE_CREDENTIAL_CAPABILITIES
        assert profile.credential_capabilities == LANE_CREDENTIAL_CAPABILITIES
        assert "FORGE_HARNESS_MCP" in profile.mcp_policy
        assert "pinned" in profile.network_policy

    def test_lock_less_repo_degrades_to_the_documented_pip_fallback(self, tmp_path: Path):
        repo = write_repo(tmp_path, lock=None)

        profile = derive_from_repo(LocalRepoSource(repo))

        assert profile.install_strategy == INSTALL_PIP_MINIMAL
        assert profile.lock_status == LOCK_ABSENT
        assert profile.lock_sha256 == ""
        assert profile.toolchain_pins == ()

    def test_unreadable_lock_is_unknown_not_absent(self, tmp_path: Path):
        """A lock that exists but cannot be read is NOT a confirmed absence
        (R14 honesty): the install strategy honestly stops claiming."""
        repo = write_repo(tmp_path)
        (tmp_path / "uv.lock").unlink()
        (tmp_path / "uv.lock").mkdir()  # a directory at the lock's path

        profile = derive_from_repo(LocalRepoSource(repo))

        assert profile.lock_status == LOCK_UNKNOWN
        assert profile.install_strategy == "unknown"

    def test_unparseable_lock_still_pins_the_bytes(self, tmp_path: Path):
        """The lane's `uv sync --frozen` consumes the lock whether or not
        forge can parse it — the digest pins the bytes, the pins stay
        best-effort empty."""
        repo = write_repo(tmp_path, lock="not toml {{{")

        profile = derive_from_repo(LocalRepoSource(repo))

        assert profile.lock_status == LOCK_LOCKED
        assert profile.lock_sha256 == hashlib.sha256(b"not toml {{{").hexdigest()
        assert profile.toolchain_pins == ()

    def test_absent_manifest_degrades_requires_python_to_unknown(self, tmp_path: Path):
        repo = write_repo(tmp_path, pyproject=None)

        profile = derive_from_repo(LocalRepoSource(repo))

        assert profile.requires_python == ""
        assert profile.install_strategy == INSTALL_UV_FROZEN


# ---------------------------------------------------------------------------
# Digest stability
# ---------------------------------------------------------------------------


class TestDigestStability:
    def test_the_same_repo_state_yields_the_same_digest(self, tmp_path: Path):
        first = derive_from_repo(LocalRepoSource(write_repo(tmp_path)))
        second = derive_from_repo(LocalRepoSource(write_repo(tmp_path)))

        assert first.profile_digest == second.profile_digest
        assert re.fullmatch(r"[0-9a-f]{64}", first.profile_digest)

    @pytest.mark.parametrize(
        ("mutate", "label"),
        [
            (lambda p: (p / "uv.lock").write_text(_LOCK_TEXT.replace("0.15.7", "0.16.0")), "pin"),
            (
                lambda p: (p / "uv.lock").write_text(
                    _LOCK_TEXT + '\n[[package]]\nname = "extra"\nversion = "1.0.0"\n'
                ),
                "unrelated-lock-entry",
            ),
            (
                lambda p: (p / "pyproject.toml").write_text(
                    _PYPROJECT_TEXT.replace(">=3.13", ">=3.12")
                ),
                "requires-python",
            ),
            (lambda p: (p / "uv.lock").unlink(), "lock-removed"),
            (
                lambda p: (p / ".github/workflows/ci.yml").write_text("- run: go test ./..."),
                "ci-config",
            ),
        ],
    )
    def test_any_relevant_byte_change_moves_the_digest(self, tmp_path: Path, mutate, label: str):
        baseline = derive_from_repo(LocalRepoSource(write_repo(tmp_path)))
        repo = write_repo(tmp_path)
        mutate(repo)

        moved = derive_from_repo(LocalRepoSource(repo))

        assert moved.profile_digest != baseline.profile_digest, label

    def test_digest_is_the_canonical_json_of_the_record(self, tmp_path: Path):
        """The digest is reproducible from the document alone — the A16
        manifest can recompute it without the live object."""
        profile = derive_from_repo(LocalRepoSource(write_repo(tmp_path)))

        from forge.runs.spec import canonical_json_digest

        assert profile.profile_digest == canonical_json_digest(profile.to_document())

    def test_record_validates_its_own_invariants(self):
        with pytest.raises(ValueError, match="must carry the lock digest"):
            ExecutionProfile(
                schema_version=1,
                python_version="3.13",
                requires_python="",
                install_strategy=INSTALL_UV_FROZEN,
                lock_status=LOCK_LOCKED,
                lock_sha256="",
                toolchain_pins=(("ruff", "1"),),
                lane_commands=LANE_TEST_COMMANDS,
                ci_commands=(),
                ci_contract="unknown",
                network_policy="p",
                mcp_policy="p",
                credential_capabilities=(),
            )
        with pytest.raises(ValueError, match="ci_contract"):
            ExecutionProfile(
                schema_version=1,
                python_version="3.13",
                requires_python="",
                install_strategy=INSTALL_PIP_MINIMAL,
                lock_status=LOCK_ABSENT,
                lock_sha256="",
                toolchain_pins=(),
                lane_commands=LANE_TEST_COMMANDS,
                ci_commands=(),
                ci_contract="matching-ish",
                network_policy="p",
                mcp_policy="p",
                credential_capabilities=(),
            )


# ---------------------------------------------------------------------------
# ci_contract honesty
# ---------------------------------------------------------------------------


class TestCiContract:
    def test_ci_within_the_lane_surface_is_matching(self, tmp_path: Path):
        profile = derive_from_repo(LocalRepoSource(write_repo(tmp_path)))

        assert profile.ci_commands == ("mypy", "pytest", "ruff")
        assert profile.ci_contract == "matching"

    def test_ci_commands_the_lane_cannot_run_make_it_an_honest_subset(self, tmp_path: Path):
        """npm test + uv run pytest: the lane runs PART of target CI's
        contract — recorded, so repair budget is never burned believing
        the lane reproduced CI (the A16 manifest consumes this)."""
        profile = derive_from_repo(LocalRepoSource(write_repo(tmp_path, ci_text=_CI_SUBSET_TEXT)))

        assert profile.ci_commands == ("npm", "pytest")
        assert profile.ci_contract == "subset"

    def test_no_detectable_ci_config_is_unknown_never_matching(self, tmp_path: Path):
        profile = derive_from_repo(LocalRepoSource(write_repo(tmp_path, ci_text=None)))

        assert profile.ci_commands == ()
        assert profile.ci_contract == "unknown"

    def test_unrelated_ci_tools_still_detect_the_lane_surface(self, tmp_path: Path):
        profile = derive_from_repo(
            LocalRepoSource(write_repo(tmp_path, ci_text="- run: cargo test\n- run: make lint"))
        )

        assert profile.ci_commands == ("cargo", "make")
        assert profile.ci_contract == "subset"

    def test_materialized_files_view_is_equivalent(self, tmp_path: Path):
        """The freeze-time shape (pre-fetched reads) derives the same
        record as the local view over the same inputs."""
        repo = write_repo(tmp_path)
        local = derive_from_repo(LocalRepoSource(repo))
        materialized = derive_from_repo(
            MaterializedFiles(
                files={
                    "uv.lock": FileRead.found(_LOCK_TEXT),
                    "pyproject.toml": FileRead.found(_PYPROJECT_TEXT),
                },
                workflows=(_CI_MATCHING_TEXT,),
            )
        )

        assert materialized.to_document() == local.to_document()


# ---------------------------------------------------------------------------
# The freeze-time reader path
# ---------------------------------------------------------------------------


class FakeReader:
    """Duck-typed provider reader: path → BlobReadResult (or a raised error)."""

    def __init__(self, results: dict[str, BlobReadResult], *, fail: bool = False):
        self._results = results
        self._fail = fail
        self.read_paths: list[str] = []

    async def read_blob(self, project_id: int, file_path: str, ref: str = "HEAD"):
        self.read_paths.append(file_path)
        if self._fail:
            raise RuntimeError("provider down")
        return self._results.get(file_path, BlobReadResult.not_found())


class TestReaderDerivation:
    async def test_lock_and_ci_config_are_read_over_the_blob_surface(self):
        reader = FakeReader(
            {
                "uv.lock": BlobReadResult.found(_LOCK_TEXT),
                "pyproject.toml": BlobReadResult.found(_PYPROJECT_TEXT),
                ".gitlab-ci.yml": BlobReadResult.found("script:\n  - uv run pytest"),
            }
        )

        profile = await derive_from_reader(reader, project_id=42, ref="main")

        assert set(reader.read_paths) == {
            "uv.lock",
            "pyproject.toml",
            ".gitlab-ci.yml",
            ".github/workflows/ci.yml",
        }
        assert profile.lock_status == LOCK_LOCKED
        assert profile.lock_sha256 == BlobReadResult.found(_LOCK_TEXT).content_sha256
        assert profile.ci_commands == ("pytest",)
        assert profile.ci_contract == "matching"

    async def test_confirmed_absence_is_absent_not_unknown(self):
        reader = FakeReader({})

        profile = await derive_from_reader(reader, project_id=42, ref="main")

        assert profile.lock_status == LOCK_ABSENT
        assert profile.install_strategy == INSTALL_PIP_MINIMAL
        assert profile.ci_contract == "unknown"

    async def test_read_failure_degrades_to_unknown_never_raises(self):
        reader = FakeReader({}, fail=True)

        profile = await derive_from_reader(reader, project_id=42, ref="main")

        assert profile.lock_status == LOCK_UNKNOWN
        assert profile.install_strategy == "unknown"
        assert profile.profile_digest  # the unknown-honest record still digests

    async def test_reader_without_the_blob_surface_is_all_unknown(self):
        class Legacy:
            pass

        profile = await derive_from_reader(Legacy(), project_id=42, ref="main")

        assert profile.lock_status == LOCK_UNKNOWN
        assert profile.install_strategy == "unknown"


# ---------------------------------------------------------------------------
# Bootstrap classification (deterministic install + honest failure class)
# ---------------------------------------------------------------------------


class TestBootstrapClassification:
    def test_the_marker_in_a_lane_log_classifies_infrastructure(self):
        log = (
            "some noise\n"
            "FORGE_BOOTSTRAP_FAILED: uv sync --frozen could not materialize the "
            "locked environment\n"
        )

        assert classify_bootstrap_failure(log) is True
        assert classify_bootstrap_failure("clean log") is False
        assert classify_bootstrap_failure("") is False

    def test_meta_bootstrap_field_carries_the_lane_classification(self):
        assert bootstrap_failed({"bootstrap": "failed"}) is True
        assert bootstrap_failed({"bootstrap": "ok"}) is False
        # Absent/unknown stays unknown — never retroactively a bootstrap
        # failure (pre-A18 lanes carry no marker).
        assert bootstrap_failed({}) is False
        assert bootstrap_failed({"bootstrap": ""}) is False

    def test_the_log_pattern_classifier_carries_the_marker(self):
        """A red lane whose log carries the marker classifies infrastructure
        on the control plane's log path too (both lanes' executors)."""
        assert FORGE_BOOTSTRAP_FAILED_MARKER.lower() in _HARNESS_INFRASTRUCTURE_PATTERNS

    def test_status_values_are_the_two_the_template_writes(self):
        from forge.runs.execution_profile import BOOTSTRAP_STATUS_OK, BOOTSTRAP_STATUS_VALUES

        assert BOOTSTRAP_STATUS_VALUES == ("ok", BOOTSTRAP_STATUS_FAILED)
        assert BOOTSTRAP_STATUS_OK == "ok"


# ---------------------------------------------------------------------------
# Spec embedding (the additive execution_profile section)
# ---------------------------------------------------------------------------


def make_spec(**overrides) -> ExecutableRunSpec:
    values = dict(
        provider="gitlab",
        project_id=42,
        issue_iid=7,
        source_base_oid="base-sha-1",
        task_title="Add a widget",
        task_description="Widgets make the app better.",
        plan_summary="Plan summary.",
        plan_files_hint=(),
        plan_digest="a" * 64,
        model_route="code",
        policy_digest="b" * 64,
        required_jobs=(),
        backend="builtin",
        harness_model="glm-5.3-flash[1m]",
        target_branch="main",
        harness_driver="claude-code",
        commit_cycles=3,
        harness_timeout=1800,
    )
    values.update(overrides)
    return ExecutableRunSpec.freeze(**values)


class TestSpecEmbedding:
    def test_the_profile_digest_rides_the_frozen_spec(self, tmp_path: Path):
        profile = derive_from_repo(LocalRepoSource(write_repo(tmp_path)))
        spec = make_spec(profile_digest=profile.profile_digest)
        document = spec.to_document()

        assert document["execution_profile"] == {"digest": profile.profile_digest}
        parsed = ExecutableRunSpec.from_document(document)
        assert parsed == spec

    def test_pre_profile_documents_parse_unchanged(self):
        """Additive convention: no derivation → no key → byte-identical to
        the pre-A18 shape, and the verified read is unchanged."""
        spec = make_spec()

        assert spec.profile_digest == ""
        assert "execution_profile" not in spec.to_document()
        assert ExecutableRunSpec.from_document(spec.to_document()) == spec

    def test_a_malformed_section_is_a_corrupt_spec(self):
        document = make_spec(profile_digest="c" * 64).to_document()
        document["execution_profile"] = "not an object"
        with pytest.raises(SpecInvalid, match="execution_profile"):
            ExecutableRunSpec.from_document(document)

    def test_a_non_digest_value_is_a_corrupt_spec(self):
        document = make_spec().to_document()
        document["execution_profile"] = {"digest": "zzz"}
        with pytest.raises(SpecInvalid, match="profile_digest"):
            ExecutableRunSpec.from_document(document)

    def test_gate_approves_the_derived_contract_end_to_end(self, tmp_path: Path):
        """The acceptance path in one flow: repo → profile → frozen spec →
        digest-verified read. What /go approves is the exact profile the
        derivation pinned."""
        from forge.runs.spec import canonical_json_digest, load_verified_spec

        profile = derive_from_repo(LocalRepoSource(write_repo(tmp_path)))
        spec = make_spec(
            backend="ci_harness",
            harness_workflow="forge-harness.github.yml",
            profile_digest=profile.profile_digest,
        )
        document = spec.to_document()

        verified = load_verified_spec(
            document=document,
            digest=canonical_json_digest(document),
        )
        assert verified.profile_digest == profile.profile_digest


# ----------------------------------------------------------------------
# B12: the OBSERVED execution — the declared profile's honest twin
# ----------------------------------------------------------------------


def test_observed_execution_records_facts_not_allowance() -> None:
    from forge.runs.execution_profile import observed_execution

    record = observed_execution(
        driver="claude-code",
        exit_status="completed",
        usage_completeness="aggregate",
        candidate_changed=True,
    )
    # allowance vocabulary (allowed commands) never appears here — only facts
    assert record.driver == "claude-code"
    assert record.exit_status == "completed"
    assert record.usage_completeness == "aggregate"
    assert record.candidate_changed is True
    assert record.observed_at  # ISO stamp
    # unknown stays unknown, never defaulted
    blank = observed_execution()
    assert blank.exit_status == "unknown"
    assert blank.candidate_changed is None


def test_observed_execution_carries_command_receipts() -> None:
    """C10: the trusted wrapper's receipts ride the observed record —
    (argv_head, exit, report); allowed-but-unexecuted stays absent."""
    from forge.runs.execution_profile import observed_execution

    record = observed_execution(
        driver="claude-code",
        exit_status="completed",
        commands=[("pytest -q", 0, ""), ("ruff check", 1, "ruff.log")],
    )
    assert record.commands == (("pytest -q", 0, ""), ("ruff check", 1, "ruff.log"))
    # no receipts → no claims
    blank = observed_execution()
    assert blank.commands == ()


def test_workspace_receipts_are_stamped_self_reported() -> None:
    """D08: provenance rides the record — workspace-file receipts are
    self_reported telemetry, never wrapper-observed proof."""
    from forge.runs.execution_profile import observed_execution

    record = observed_execution(driver="claude-code", receipts_producer="self_reported")
    assert record.receipts_producer == "self_reported"
    assert observed_execution().receipts_producer == ""  # absent = unknown


# ----------------------------------------------------------------------
# NXT-29 — the hardened lane execution profile: honest data + validators
# ----------------------------------------------------------------------


class TestLaneProfileV2:
    def test_staged_credentials_mount_the_provider_key_only_for_coding(self):
        """No phase receives unrelated provider or publication
        credentials: the model key exists at the coding stage ONLY and is
        stripped from every other stage."""
        coding = credentials_at(LANE_PROFILE_V2, "coding")
        assert "ANTHROPIC_AUTH_TOKEN" in coding
        assert "ZAI_API_KEY" in coding
        assert "OPENAI_API_KEY" in coding
        assert credentials_at(LANE_PROFILE_V2, "discovery") == ()
        assert credentials_at(LANE_PROFILE_V2, "verification") == ()
        # bootstrap carries the scoped READ token only — never a provider
        # key, never anything writable
        assert credentials_at(LANE_PROFILE_V2, "bootstrap") == ("FORGE_BOT_READ_TOKEN",)

    def test_no_stage_carries_a_publisher_or_write_credential(self):
        every_name = {n for _s, names in LANE_PROFILE_V2.credential_staging for n in names}
        assert not any("WRITE" in n or "PUBLISH" in n for n in every_name)
        # the scoped read token exists ONLY at bootstrap
        token_stages = [
            stage
            for stage, names in LANE_PROFILE_V2.credential_staging
            if "FORGE_BOT_READ_TOKEN" in names
        ]
        assert token_stages == ["bootstrap"]

    def test_data_boundaries_are_workspace_and_tmpfs_only(self):
        assert LANE_PROFILE_V2.writable_roots == ("/workspace",)
        assert LANE_PROFILE_V2.tmpfs_roots == ("/tmp",)
        assert LANE_PROFILE_V2.read_only_roots == ("/",)  # everything else ro

    def test_capabilities_drop_all_and_add_none(self):
        assert LANE_PROFILE_V2.cap_drop_all is True
        assert LANE_PROFILE_V2.cap_add == ()

    def test_the_egress_hook_is_env_carried_and_deny_by_default(self):
        assert LANE_PROFILE_V2.egress_allowlist_env == "FORGE_EGRESS_ALLOWLIST"
        assert "FORGE_EGRESS_ALLOWLIST" in LANE_PROFILE_V2.egress_policy
        assert "deny by default" in LANE_PROFILE_V2.egress_policy
        # the honesty bound is IN the data, not only the docstring
        assert "runner-side enforcement required" in LANE_PROFILE_V2.egress_policy
        assert "declared contract" in LANE_PROFILE_V2.egress_policy

    def test_v1_is_the_unstaged_posture_stated_as_data(self):
        from forge.runs.execution_profile import LANE_PROFILE_V1

        assert LANE_PROFILE_V1.cap_drop_all is False
        assert LANE_PROFILE_V1.writable_roots == ("/",)
        # unstaged: every phase sees the ambient set
        assert credentials_at(LANE_PROFILE_V1, "coding") == LANE_CREDENTIAL_CAPABILITIES
        assert credentials_at(LANE_PROFILE_V1, "verification") == LANE_CREDENTIAL_CAPABILITIES
        assert LANE_PROFILE_V1.to_document() != LANE_PROFILE_V2.to_document()

    def test_the_profiles_stay_out_of_the_execution_profile_digest(self):
        """The hardened-profile symbols are ADDITIVE: the versioned
        build/test record (A18) and its digest do not gain a profile
        axis — a repo derives the same digest as before."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = write_repo(Path(tmp))
            profile = derive_from_repo(LocalRepoSource(repo))
            assert "lane_profile" not in profile.to_document()
            assert set(profile.to_document()) == {
                "schema_version",
                "python_version",
                "requires_python",
                "install",
                "toolchain_pins",
                "test_commands",
                "network_policy",
                "mcp_policy",
                "credential_capabilities",
            }


class TestLaneProfileValidation:
    def test_unknown_profile_ids_fail_closed(self):
        with pytest.raises(ValueError, match="unknown lane profile"):
            lane_profile("v9")
        with pytest.raises(ValueError, match="unknown lane profile"):
            lane_profile("")

    def test_the_vocabulary_is_derived_from_the_shipped_records(self):
        assert LANE_PROFILE_VALUES == ("v1", "v2")
        assert lane_profile("v1") is not None
        assert lane_profile("v2") is LANE_PROFILE_V2

    def test_an_unnamed_v2_stage_sees_nothing(self):
        assert credentials_at(LANE_PROFILE_V2, "not-a-stage") == ()

    def test_record_construction_validates_its_own_invariants(self):
        from forge.runs.execution_profile import LaneExecutionProfile

        with pytest.raises(ValueError, match="unknown lane profile"):
            LaneExecutionProfile(
                profile_id="v3",
                credential_staging=(),
                writable_roots=(),
                read_only_roots=(),
                tmpfs_roots=(),
                cap_drop_all=True,
                cap_add=(),
                egress_policy="p",
                egress_allowlist_env="E",
            )
        with pytest.raises(ValueError, match="outside the vocabulary"):
            LaneExecutionProfile(
                profile_id="v2",
                credential_staging=(("wonderland", ()),),
                writable_roots=(),
                read_only_roots=(),
                tmpfs_roots=(),
                cap_drop_all=True,
                cap_add=(),
                egress_policy="p",
                egress_allowlist_env="E",
            )
        with pytest.raises(ValueError, match="no duplicates"):
            LaneExecutionProfile(
                profile_id="v2",
                credential_staging=(("coding", ()), ("coding", ())),
                writable_roots=(),
                read_only_roots=(),
                tmpfs_roots=(),
                cap_drop_all=True,
                cap_add=(),
                egress_policy="p",
                egress_allowlist_env="E",
            )


class TestLaneProfileDeclaration:
    """The declaration validator: refusal is observable, never a silent
    fallback to an unrestricted profile."""

    def test_an_undeclared_profile_is_refused(self):
        ok, reason = validate_lane_profile_declaration({})
        assert ok is False
        assert "FORGE_LANE_PROFILE is not declared" in reason

    def test_a_value_outside_the_vocabulary_is_refused(self):
        ok, reason = validate_lane_profile_declaration(
            {"FORGE_LANE_PROFILE": "v3", "FORGE_EGRESS_ALLOWLIST": ""}
        )
        assert ok is False
        assert "outside the vocabulary" in reason

    def test_v1_declares_cleanly_without_needing_the_hook(self):
        assert validate_lane_profile_declaration({"FORGE_LANE_PROFILE": " v1 "}) == (True, "ok")

    def test_v2_without_the_egress_hook_is_a_refused_half_declaration(self):
        ok, reason = validate_lane_profile_declaration({"FORGE_LANE_PROFILE": "v2"})
        assert ok is False
        assert "FORGE_EGRESS_ALLOWLIST" in reason

    def test_v2_with_an_empty_allowlist_is_deny_all_and_valid(self):
        assert validate_lane_profile_declaration(
            {"FORGE_LANE_PROFILE": "v2", "FORGE_EGRESS_ALLOWLIST": ""}
        ) == (True, "ok")
        assert validate_lane_profile_declaration(
            {"FORGE_LANE_PROFILE": "v2", "FORGE_EGRESS_ALLOWLIST": "api.z.ai,pypi.org"}
        ) == (True, "ok")


class TestSdkLaneTemplatesDeclareTheProfile:
    """The v2 template jobs (ci/templates/*sdk-lane*) CAN declare the
    hardened profile: each template's variables block validates as-is
    (v1 default) and still validates with the dispatch's v2 flip —
    because the egress hook rides in the same block."""

    TEMPLATES = (
        Path(__file__).resolve().parent.parent / "ci" / "templates" / name
        for name in (
            "claude-sdk-lane.gitlab-ci.yml",
            "codex-sdk-lane.gitlab-ci.yml",
            "opencode-sdk-lane.gitlab-ci.yml",
        )
    )

    def _variables(self, template: Path) -> dict:
        import yaml

        doc = yaml.safe_load(template.read_text())
        lane_key = next(k for k in doc if k.startswith("forge-agent"))
        return doc[lane_key]["variables"]

    def test_every_sdk_lane_template_declares_a_valid_default(self):
        for template in self.TEMPLATES:
            variables = self._variables(template)
            assert variables["FORGE_LANE_PROFILE"] == "v1", template.name
            assert validate_lane_profile_declaration(variables) == (True, "ok"), template.name

    def test_every_sdk_lane_template_can_declare_v2(self):
        for template in self.TEMPLATES:
            declared = dict(self._variables(template))
            declared["FORGE_LANE_PROFILE"] = "v2"  # the dispatch's opt-in flip
            assert validate_lane_profile_declaration(declared) == (True, "ok"), template.name

    def test_the_templates_do_not_silently_default_to_v2(self):
        # Opt-in means opt-in: the YAML default stays the v1 posture; only
        # a dispatch/pipeline variable (which beats YAML) selects v2.
        for template in self.TEMPLATES:
            assert self._variables(template)["FORGE_LANE_PROFILE"] == "v1", template.name


# ---------------------------------------------------------------------------
# R28-24: runner-side runtime enforcement
# ---------------------------------------------------------------------------

#: A v2-compliant coding-stage env: the egress hook present (deny-all
#: value is valid) and no credential leaking from another stage.
_V2_COMPLIANT_ENV = {
    "FORGE_EGRESS_ALLOWLIST": "api.z.ai,pypi.org",
    "ZAI_API_KEY": "sk-live",
    "PATH": "/usr/bin:/bin",
}

#: A v2 container's mount table: read-only rootfs, tmpfs /tmp (the
#: declared data boundaries), a workspace over-mount.
_V2_RO_ROOT_MOUNTS = (
    "/dev/sda1 / ext4 ro,relatime 0 0\n"
    "tmpfs /tmp tmpfs rw,nosuid,nodev 0 0\n"
    "/dev/sda2 /workspace ext4 rw,relatime 0 0\n"
)

_RW_ROOT_MOUNTS = "/dev/sda1 / ext4 rw,relatime 0 0\ntmpfs /tmp tmpfs rw 0 0\n"


class TestRuntimeEnforcement:
    """``validate_runtime`` — the measurable slice of the v2 contract a
    process can check about itself; v2-declared + non-compliant is the
    fail-closed lane-entry refusal, never a silent downgrade."""

    def test_v2_compliant_runtime_passes(self):
        assert (
            validate_runtime(
                LANE_PROFILE_V2,
                stage="coding",
                env=_V2_COMPLIANT_ENV,
                proc_mounts=_V2_RO_ROOT_MOUNTS,
            )
            == ()
        )

    def test_v2_with_an_empty_allowlist_is_deny_all_and_compliant(self):
        assert (
            validate_runtime(
                LANE_PROFILE_V2,
                stage="coding",
                env={"FORGE_EGRESS_ALLOWLIST": ""},
                proc_mounts=_V2_RO_ROOT_MOUNTS,
            )
            == ()
        )

    def test_v2_without_the_egress_allowlist_env_is_refused(self):
        violations = validate_runtime(
            LANE_PROFILE_V2,
            stage="coding",
            env={"ZAI_API_KEY": "sk-live"},
            proc_mounts=_V2_RO_ROOT_MOUNTS,
        )
        assert [v.check for v in violations] == ["egress_allowlist_declared"]
        assert "FORGE_EGRESS_ALLOWLIST" in violations[0].detail

    def test_v2_with_a_writable_root_filesystem_is_refused(self):
        violations = validate_runtime(
            LANE_PROFILE_V2,
            stage="coding",
            env=dict(_V2_COMPLIANT_ENV),
            proc_mounts=_RW_ROOT_MOUNTS,
        )
        assert [v.check for v in violations] == ["root_filesystem_read_only"]
        assert "rw" in violations[0].detail

    def test_v2_with_an_empty_mount_table_cannot_prove_the_boundary(self):
        violations = validate_runtime(
            LANE_PROFILE_V2, stage="coding", env=dict(_V2_COMPLIANT_ENV), proc_mounts=""
        )
        assert "root_filesystem_read_only" in [v.check for v in violations]

    def test_a_bootstrap_credential_leaking_into_coding_is_refused(self):
        leaked = dict(_V2_COMPLIANT_ENV)
        leaked["FORGE_BOT_READ_TOKEN"] = "ghp-read"
        violations = validate_runtime(
            LANE_PROFILE_V2, stage="coding", env=leaked, proc_mounts=_V2_RO_ROOT_MOUNTS
        )
        assert [v.check for v in violations] == ["credential_staging"]
        assert "FORGE_BOT_READ_TOKEN" in violations[0].detail

    def test_a_provider_key_reaching_verification_is_refused(self):
        violations = validate_runtime(
            LANE_PROFILE_V2,
            stage="verification",
            env={
                "FORGE_EGRESS_ALLOWLIST": "",
                "ANTHROPIC_API_KEY": "sk-ant",  # models must not reach tests
            },
            proc_mounts=_V2_RO_ROOT_MOUNTS,
        )
        assert [v.check for v in violations] == ["credential_staging"]
        assert "ANTHROPIC_API_KEY" in violations[0].detail

    def test_an_allowed_credential_merely_absent_is_not_a_violation(self):
        # Fail-closed on ISOLATION, never on availability: a missing key
        # is the driver's own startup error, not an isolation breach.
        assert (
            validate_runtime(
                LANE_PROFILE_V2,
                stage="coding",
                env={"FORGE_EGRESS_ALLOWLIST": "api.z.ai"},
                proc_mounts=_V2_RO_ROOT_MOUNTS,
            )
            == ()
        )

    def test_an_unknown_stage_cannot_be_scoped(self):
        violations = validate_runtime(
            LANE_PROFILE_V2, stage="wonderland", env={}, proc_mounts=_V2_RO_ROOT_MOUNTS
        )
        assert [v.check for v in violations] == ["stage_identity"]

    def test_v1_declares_no_isolation_so_nothing_is_checked(self):
        """v1 IS the unstaged posture — the same env that fails v2 passes
        v1. This is not a downgrade path: v1 makes no claims to verify."""
        assert (
            validate_runtime(
                lane_profile("v1"),
                stage="coding",
                env={"FORGE_BOT_READ_TOKEN": "leak", "ANTHROPIC_API_KEY": "x"},
                proc_mounts=_RW_ROOT_MOUNTS,
            )
            == ()
        )

    def test_every_violation_carries_an_actionable_detail(self):
        violations = validate_runtime(LANE_PROFILE_V2, stage="coding", env={}, proc_mounts="")
        checks = [v.check for v in violations]
        assert "egress_allowlist_declared" in checks
        assert "root_filesystem_read_only" in checks
        for violation in violations:
            assert str(violation).startswith(f"[{violation.check}]")
            assert len(violation.detail) > 20  # says WHAT was observed


class TestLaneEntryEnforcesTheProfile:
    """The lane entry (forge.harness_entry) calls validate_runtime at
    startup: v2-declared + non-compliant fails the lane CLOSED with an
    actionable message — never a silent downgrade to the unstaged
    posture."""

    def _run_main(self, tmp_path, monkeypatch, env: dict) -> tuple[int, str]:
        from forge import harness_entry

        for name in ("FORGE_LANE_PROFILE", "FORGE_LANE_STAGE", "FORGE_EGRESS_ALLOWLIST"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("FORGE_EXIT_FILE", str(tmp_path / "exit"))
        monkeypatch.chdir(tmp_path)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        rc = harness_entry.main(["--driver", "claude-code"])
        return rc, (tmp_path / "exit").read_text()

    def test_v2_on_a_non_compliant_runner_fails_closed(self, tmp_path, monkeypatch, capsys):
        rc, exit_status = self._run_main(tmp_path, monkeypatch, {"FORGE_LANE_PROFILE": "v2"})
        assert rc == 1
        assert exit_status.strip() == "failed"
        message = capsys.readouterr().err
        assert "NON-COMPLIANT" in message
        assert "no silent downgrade" in message
        assert "FORGE_LANE_PROFILE=v1" in message  # the actionable exit

    def test_v1_runs_its_own_startup_checks_instead(self, tmp_path, monkeypatch, capsys):
        # v1 declares no isolation: the lane proceeds past the profile
        # check and fails on its OWN next precondition (the missing brief
        # file), proving the profile check did not fire.
        rc, exit_status = self._run_main(tmp_path, monkeypatch, {"FORGE_LANE_PROFILE": "v1"})
        assert rc == 1
        message = capsys.readouterr().err
        assert "NON-COMPLIANT" not in message
        assert "brief file" in message

    def test_an_unset_profile_is_the_legacy_dispatch_no_check(self, tmp_path, monkeypatch, capsys):
        rc, _ = self._run_main(tmp_path, monkeypatch, {})
        assert rc == 1
        assert "NON-COMPLIANT" not in capsys.readouterr().err

    def test_an_unknown_profile_id_fails_closed_too(self, tmp_path, monkeypatch, capsys):
        rc, exit_status = self._run_main(tmp_path, monkeypatch, {"FORGE_LANE_PROFILE": "v9"})
        assert rc == 1
        assert exit_status.strip() == "failed"
        assert "unknown lane profile" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# NEXT-15 — the recipe/harness decomposition
# ---------------------------------------------------------------------------


class TestRuntimeRecipeHarnessDecomposition:
    """A lane is a (recipe, harness) tuple; ``dotnet-lane`` is an alias for
    ("dotnet-9", "claude-code") — the decomposition is the authority."""

    def test_the_dotnet_lane_alias_resolves_to_its_decomposition(self):
        profile = resolve_driver_profile("dotnet-lane")
        assert profile.recipe.recipe_id == "dotnet-9"
        assert profile.harness.harness_id == "claude-code"
        assert profile.alias == "dotnet-lane"  # the legacy id rides the audit
        assert profile.driver_id == "dotnet-lane"  # dispatch spelling unchanged

    def test_a_new_recipe_harness_combination_validates(self):
        # the runtime is REUSED by a compatible harness — no second .NET
        # command implementation anywhere in the recipe axis
        profile = compile_driver_profile("dotnet-9", "copilot")
        assert profile.recipe.recipe_id == "dotnet-9"
        assert profile.harness.sdk == "copilot-acp"
        assert profile.alias == ""
        assert profile.driver_id == "dotnet-9+copilot"  # the composed spelling
        # and the composed spelling resolves back through the same authority
        resolved = resolve_driver_profile("dotnet-9+copilot")
        assert resolved.recipe.recipe_id == profile.recipe.recipe_id
        assert resolved.harness.harness_id == profile.harness.harness_id

    def test_an_unsupported_combination_is_refused_before_model_calls(self):
        with pytest.raises(ValueError, match="not in the compiled compatibility matrix"):
            compile_driver_profile("node-22", "codex")

    def test_unknown_axes_and_ids_fail_closed(self):
        with pytest.raises(ValueError, match="unknown runtime recipe"):
            compile_driver_profile("go-1-24", "claude-code")
        with pytest.raises(ValueError, match="unknown harness profile"):
            compile_driver_profile("dotnet-9", "grok")
        with pytest.raises(ValueError, match="unknown driver id"):
            resolve_driver_profile("grok-build")  # a fused id with no alias entry
        with pytest.raises(ValueError, match="non-empty"):
            resolve_driver_profile("")

    def test_the_dotnet_recipe_pins_the_template_image_digest(self):
        # the recipe's image pin IS the shipped lane's image — one pin,
        # both files (the template test pins the YAML side of the pair).
        import yaml

        template = yaml.safe_load(
            (REPO_ROOT / "ci" / "templates" / "dotnet-lane.gitlab-ci.yml").read_text()
        )
        assert template["forge-agent-dotnet"]["image"] == RUNTIME_RECIPES["dotnet-9"].image_pin

    def test_the_dotnet_recipe_carries_the_locked_tail_and_trx_prefix(self):
        recipe = RUNTIME_RECIPES["dotnet-9"]
        assert recipe.version_check == ("dotnet", "--version")
        assert ("dotnet", "restore", "--locked-mode") in recipe.build_commands
        assert ("dotnet", "build", "--no-restore", "--locked-mode") in recipe.build_commands
        assert recipe.report_prefix == "forge_"  # NEXT-16: aggregate ALL reports

    def test_the_compiled_matrix_and_aliases_are_consistent(self):
        for alias, pair in DRIVER_PROFILE_ALIASES.items():
            assert pair in COMPILED_COMBINATIONS, alias  # an alias is always compiled
        for recipe_id, harness_id in COMPILED_COMBINATIONS:
            assert recipe_id in RUNTIME_RECIPES
            assert harness_id in HARNESS_PROFILES

    def test_the_profile_document_states_both_axes(self):
        document = resolve_driver_profile("dotnet-lane").to_document()
        assert document["driver_id"] == "dotnet-lane"
        assert document["alias"] == "dotnet-lane"
        assert document["recipe"]["recipe_id"] == "dotnet-9"
        assert document["harness"]["harness_id"] == "claude-code"
        assert document["harness"]["credential_names"] == [
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
        ]


class TestRecipeGatesTheToolchain:
    """The recipe axis gates the toolchain checks INDEPENDENTLY of the
    harness — the acceptance criterion verbatim."""

    def test_a_missing_global_json_is_a_violation_naming_the_remedy(self):
        recipe = runtime_recipe("dotnet-9")
        (violation,) = recipe.gate(MaterializedFiles(files={}))
        assert violation.check == "required_file"
        assert violation.path == "global.json"
        assert "commit it" in violation.detail

    def test_a_present_global_json_gates_clean(self):
        recipe = runtime_recipe("dotnet-9")
        source = MaterializedFiles(
            files={"global.json": FileRead.found('{ "sdk": { "version": "9.0.100" } }')}
        )
        assert recipe.gate(source) == ()

    def test_an_unreadable_pin_file_is_a_violation_not_a_pass(self):
        recipe = runtime_recipe("dotnet-9")
        (violation,) = recipe.gate(MaterializedFiles(files={"global.json": FileRead.unknown()}))
        assert violation.check == "required_file_unreadable"
        assert "unverifiable" in violation.detail

    def test_the_gate_is_harness_independent(self):
        # the SAME recipe object, the SAME gate, for every compiled
        # harness over the .NET runtime — one gate, no per-agent copies
        recipe = runtime_recipe("dotnet-9")
        empty = MaterializedFiles(files={})
        for harness_id in ("claude-code", "codex", "copilot"):
            profile = compile_driver_profile("dotnet-9", harness_id)
            assert profile.recipe is recipe
            (violation,) = recipe.gate(empty)
            assert violation.path == "global.json"

    def test_the_python_recipe_gates_no_files(self):
        # lock-less repos take the documented pip fallback — the python
        # recipe's toolchain gate is deliberately empty
        assert RUNTIME_RECIPES["python-3-13"].required_files == ()
        assert runtime_recipe("python-3-13").gate(MaterializedFiles(files={})) == ()

    def test_the_node_recipe_requires_its_lockfile(self):
        recipe = runtime_recipe("node-22")
        assert recipe.required_files == ("package-lock.json",)
        (violation,) = recipe.gate(MaterializedFiles(files={}))
        assert violation.path == "package-lock.json"

    def test_an_empty_recipe_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="unexecutable"):
            RuntimeRecipe(
                recipe_id="x",
                image_pin="x:1",
                version_check=(),
                build_commands=(),
                required_files=(),
            )
