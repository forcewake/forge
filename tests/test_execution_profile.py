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
    FORGE_BOOTSTRAP_FAILED_MARKER,
    INSTALL_PIP_MINIMAL,
    INSTALL_UV_FROZEN,
    LANE_CREDENTIAL_CAPABILITIES,
    LANE_PYTHON_VERSION,
    LANE_TEST_COMMANDS,
    LOCK_ABSENT,
    LOCK_LOCKED,
    LOCK_UNKNOWN,
    ExecutionProfile,
    FileRead,
    LocalRepoSource,
    MaterializedFiles,
    bootstrap_failed,
    classify_bootstrap_failure,
    derive_from_reader,
    derive_from_repo,
)
from forge.runs.spec import ExecutableRunSpec, SpecInvalid

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
