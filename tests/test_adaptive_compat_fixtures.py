"""ADR-0029 §4 / R32-24 + ADR-0030 (R36-20): the versioned compatibility fixtures.

Pinned here: every registered fixture LOADS under its supported
read/recovery semantics; run_spec v1/v2 parse to the legacy
(non-executable) view with the SpecLegacy recovery; the old checkpoint
index document recovers under the migration-026 no-backfill contract;
the old control-command rows read with their labelled fallback / exact
binding; the authority-boundary schemas (attempt_start v1/v2,
continuation_decision v1/v2, checkpoint_lookup v1/v2 — ADR-0030's
persisted surfaces) read with their honest recovery semantics; an
UNKNOWN version raises UnsupportedDocumentVersion (never a silent
best-effort parse); and the inventory lists the complete kind×version
surface.
"""

import pytest

from forge.adaptive.checkpoint_repository import (
    LOOKUP_ABSENT,
    LOOKUP_UNAVAILABLE,
    CheckpointLookupOutcome,
)
from forge.adaptive.compat_fixtures import (
    CompatAttemptStartDocument,
    CompatCheckpointIndex,
    CompatContinuationDocument,
    CompatControlCommand,
    CompatSpecDocument,
    UnsupportedDocumentVersion,
    compat_document,
    compat_inventory,
    load_compat_document,
)
from forge.runs.spec import ExecutableRunSpec

EXPECTED_INVENTORY = [
    ("attempt_start", 1),
    ("attempt_start", 2),
    ("checkpoint_lookup", 1),
    ("checkpoint_lookup", 2),
    ("checkpoint_metadata", 1),
    ("continuation_decision", 1),
    ("continuation_decision", 2),
    ("control_command", 1),
    ("control_command", 2),
    ("run_spec", 1),
    ("run_spec", 2),
    ("run_spec", 3),
]


# -- inventory ----------------------------------------------------------------


class TestInventory:
    def test_inventory_lists_every_shipped_kind_and_version(self):
        assert compat_inventory() == EXPECTED_INVENTORY

    def test_every_fixture_loads(self):
        for kind, version in compat_inventory():
            document = compat_document(kind, version)
            assert isinstance(document, dict)
            assert load_compat_document(kind, version, document) is not None

    def test_canned_documents_are_fresh_copies(self):
        one = compat_document("run_spec", 1)
        one["subject"]["project_id"] = 999
        assert compat_document("run_spec", 1)["subject"]["project_id"] == 42

    def test_unknown_fixture_document_is_refused(self):
        with pytest.raises(UnsupportedDocumentVersion):
            compat_document("run_spec", 7)


# -- run_spec v1/v2: digest-only, SpecLegacy recovery --------------------------


class TestLegacySpecDocuments:
    def test_v1_parses_to_the_non_executable_legacy_view(self):
        parsed = load_compat_document("run_spec", 1, compat_document("run_spec", 1))
        assert isinstance(parsed, CompatSpecDocument)
        assert parsed.executable is False
        assert parsed.provider == "gitlab"
        assert parsed.provider_provenance.startswith("server_default")
        assert parsed.recovery == "blocked(spec_legacy: re-approval required)"

    def test_v2_reads_the_provider_from_the_subject(self):
        parsed = load_compat_document("run_spec", 2, compat_document("run_spec", 2))
        assert isinstance(parsed, CompatSpecDocument)
        assert parsed.provider == "github"
        assert parsed.provider_provenance == "subject.provider"
        assert parsed.executable is False

    @pytest.mark.parametrize("version", [1, 2])
    def test_v1_v2_digests_survive_the_read(self, version):
        parsed = load_compat_document("run_spec", version, compat_document("run_spec", version))
        assert isinstance(parsed, CompatSpecDocument)
        assert len(parsed.plan_digest) == 64
        assert len(parsed.task_digest) == 64
        assert len(parsed.policy_digest) == 64
        assert parsed.source_base_oid

    @pytest.mark.parametrize("version", [1, 2])
    def test_legacy_document_is_never_an_executablerunspec(self, version):
        parsed = load_compat_document("run_spec", version, compat_document("run_spec", version))
        assert not isinstance(parsed, ExecutableRunSpec)

    @pytest.mark.parametrize("version", [1, 2])
    def test_corrupt_legacy_subject_is_refused(self, version):
        document = compat_document("run_spec", version)
        document["subject"] = "not-an-object"
        with pytest.raises(UnsupportedDocumentVersion, match="subject"):
            load_compat_document("run_spec", version, document)


# -- run_spec v3: the executable document --------------------------------------


class TestCurrentSpecDocument:
    def test_v3_parses_through_the_real_verifier(self):
        parsed = load_compat_document("run_spec", 3, compat_document("run_spec", 3))
        assert isinstance(parsed, ExecutableRunSpec)
        assert parsed.provider == "gitlab"
        assert parsed.profile_digest
        assert parsed.task_title == "Add a widget"

    def test_v3_corruption_is_an_error_not_a_version_gap(self):
        document = compat_document("run_spec", 3)
        document.pop("task")
        with pytest.raises(ValueError, match="corrupt"):
            load_compat_document("run_spec", 3, document)


# -- checkpoint_metadata v1: the pre-migration-026 index -----------------------


class TestCheckpointIndexDocument:
    def test_old_index_recovers_under_the_no_backfill_contract(self):
        parsed = load_compat_document(
            "checkpoint_metadata", 1, compat_document("checkpoint_metadata", 1)
        )
        assert isinstance(parsed, CompatCheckpointIndex)
        assert parsed.work_id == "run-77"
        assert "no backfill" in parsed.recovery
        assert "migration 026" in parsed.recovery

    def test_entries_are_ordered_by_sequence_and_id(self):
        document = compat_document("checkpoint_metadata", 1)
        # A lower-sequence upload landing LAST must not become the tail —
        # the promotion order is (sequence, checkpoint_id), R28-06.
        document["checkpoints"].insert(
            0,
            {
                "checkpoint_id": "e" * 64,
                "sequence": 0,
                "files": 1,
                "uploaded_at": "2026-09-22T09:00:00+00:00",
            },
        )
        parsed = load_compat_document("checkpoint_metadata", 1, document)
        sequences = [entry["sequence"] for entry in parsed.checkpoints]
        assert sequences == sorted(sequences)

    def test_malformed_index_is_refused(self):
        document = compat_document("checkpoint_metadata", 1)
        document["checkpoints"] = "nope"
        with pytest.raises(UnsupportedDocumentVersion):
            load_compat_document("checkpoint_metadata", 1, document)


# -- control_command v1/v2: resume rows ----------------------------------------


class TestControlCommandDocuments:
    def test_pre_next03_resume_row_reads_as_the_labelled_fallback(self):
        parsed = load_compat_document("control_command", 1, compat_document("control_command", 1))
        assert isinstance(parsed, CompatControlCommand)
        assert parsed.kind_command == "resume"
        assert parsed.selection == "legacy_active_fallback"
        assert parsed.checkpoint_ref == ""
        assert "labelled" in parsed.recovery or "says so" in parsed.recovery

    def test_next03_resume_row_binds_the_exact_checkpoint(self):
        parsed = load_compat_document("control_command", 2, compat_document("control_command", 2))
        assert isinstance(parsed, CompatControlCommand)
        assert parsed.selection == "exact"
        assert "@" in parsed.checkpoint_ref
        assert parsed.checkpoint_sequence == 2
        assert len(parsed.source_oid) == 40

    def test_a_resume_payload_on_a_v1_row_is_refused_not_upgraded(self):
        document = compat_document("control_command", 1)
        document["payload"] = {"checkpoint_ref": f"run-77@{'b' * 64}"}
        with pytest.raises(UnsupportedDocumentVersion, match="v2"):
            load_compat_document("control_command", 1, document)

    def test_a_v2_row_without_the_reference_is_refused(self):
        document = compat_document("control_command", 2)
        document["payload"] = {}
        with pytest.raises(UnsupportedDocumentVersion, match="checkpoint_ref"):
            load_compat_document("control_command", 2, document)


# -- attempt_start v1/v2: the dispatch envelope evidence (ADR-0030) ------------


class TestAttemptStartDocuments:
    def test_v1_reads_audit_only_no_identity_manufactured(self):
        parsed = load_compat_document("attempt_start", 1, compat_document("attempt_start", 1))
        assert isinstance(parsed, CompatAttemptStartDocument)
        assert parsed.execution_attempt_id is None  # never manufactured
        assert parsed.identity_strength == "source_oid_only"
        assert parsed.source_base_oid == "e" * 40
        assert "audit-only" in parsed.recovery
        assert "never manufactured" in parsed.recovery or "none is manufactured" in parsed.recovery

    def test_v2_carries_the_separated_identity_axes(self):
        parsed = load_compat_document("attempt_start", 2, compat_document("attempt_start", 2))
        assert isinstance(parsed, CompatAttemptStartDocument)
        assert parsed.execution_attempt_id == "b" * 64
        assert parsed.identity_strength == "execution_id_v2"
        assert parsed.source_base_oid == parsed.source_base_oid  # own axis
        assert parsed.authority_epoch == 3
        assert parsed.continuation_ref_digest == "c" * 64

    def test_a_v1_document_with_an_execution_id_is_refused(self):
        document = compat_document("attempt_start", 1)
        document["execution_attempt_id"] = "b" * 64
        with pytest.raises(UnsupportedDocumentVersion, match="v2"):
            load_compat_document("attempt_start", 1, document)

    def test_a_v2_document_without_the_derived_identity_is_refused(self):
        document = compat_document("attempt_start", 2)
        document["execution_attempt_id"] = ""
        with pytest.raises(UnsupportedDocumentVersion, match="execution_attempt_id"):
            load_compat_document("attempt_start", 2, document)

    def test_a_missing_envelope_digest_is_refused(self):
        document = compat_document("attempt_start", 2)
        document["envelope_digest"] = "not-a-digest"
        with pytest.raises(UnsupportedDocumentVersion, match="envelope_digest"):
            load_compat_document("attempt_start", 2, document)


# -- continuation_decision v1/v2: the retry decision evidence (ADR-0030) -------


class TestContinuationDecisionDocuments:
    def test_v1_reads_the_decision_core_without_lineage(self):
        parsed = load_compat_document(
            "continuation_decision", 1, compat_document("continuation_decision", 1)
        )
        assert isinstance(parsed, CompatContinuationDocument)
        assert parsed.mode == "uncertain"
        assert parsed.uncertain is True
        # pre-R36-02: the lineage keys did not exist — absent, never guessed
        assert parsed.source_attempt is None
        assert parsed.native_command_id is None
        assert parsed.checkpoint_digest is None
        assert "never guessed" in parsed.recovery

    def test_v2_carries_lineage_and_the_pinned_digest(self):
        parsed = load_compat_document(
            "continuation_decision", 2, compat_document("continuation_decision", 2)
        )
        assert isinstance(parsed, CompatContinuationDocument)
        assert parsed.mode == "required"
        assert parsed.uncertain is False
        assert parsed.source_attempt == 3
        assert parsed.native_command_id == "gh-delivery-9f2c1a"
        assert parsed.native_start_verdict == "dispatched"
        assert parsed.checkpoint_digest == "c" * 64

    def test_an_unknown_mode_is_refused(self):
        document = compat_document("continuation_decision", 2)
        document["mode"] = "maybe"
        with pytest.raises(UnsupportedDocumentVersion, match="unknown mode"):
            load_compat_document("continuation_decision", 2, document)

    def test_a_missing_evidence_digest_is_refused(self):
        document = compat_document("continuation_decision", 1)
        document["evidence_digest"] = ""
        with pytest.raises(UnsupportedDocumentVersion, match="malformed"):
            load_compat_document("continuation_decision", 1, document)


# -- checkpoint_lookup v1/v2: the typed lookup outcome (ADR-0030) ---------------


class TestCheckpointLookupDocuments:
    def test_v1_reads_the_lossy_legacy_answer(self):
        parsed = load_compat_document(
            "checkpoint_lookup", 1, compat_document("checkpoint_lookup", 1)
        )
        assert isinstance(parsed, CheckpointLookupOutcome)
        assert parsed.state == LOOKUP_ABSENT
        assert parsed.authority == "legacy-http-opt-in"
        assert "collapsed" in parsed.detail

    def test_v2_reads_the_typed_five_state_outcome(self):
        parsed = load_compat_document(
            "checkpoint_lookup", 2, compat_document("checkpoint_lookup", 2)
        )
        assert isinstance(parsed, CheckpointLookupOutcome)
        assert parsed.state == LOOKUP_UNAVAILABLE
        assert parsed.authority == "postgres"

    def test_v1_cannot_claim_typed_vocabulary(self):
        document = compat_document("checkpoint_lookup", 1)
        document["state"] = "unavailable"
        with pytest.raises(UnsupportedDocumentVersion, match="v2"):
            load_compat_document("checkpoint_lookup", 1, document)

    def test_exact_requires_the_content_address(self):
        document = compat_document("checkpoint_lookup", 2)
        document["state"] = "exact"
        document["checkpoint_id"] = "short"
        with pytest.raises(UnsupportedDocumentVersion, match="content address"):
            load_compat_document("checkpoint_lookup", 2, document)

    def test_an_unknown_state_is_refused(self):
        document = compat_document("checkpoint_lookup", 2)
        document["state"] = "maybe"
        with pytest.raises(UnsupportedDocumentVersion, match="unknown state"):
            load_compat_document("checkpoint_lookup", 2, document)


# -- unknown versions: never a silent best-effort parse ------------------------


class TestUnsupportedVersions:
    @pytest.mark.parametrize(
        ("kind", "version"),
        [
            ("run_spec", 0),
            ("run_spec", 4),
            ("checkpoint_metadata", 2),
            ("control_command", 3),
            ("work_contract", 1),
            ("", 1),
        ],
    )
    def test_unknown_kind_or_version_is_refused(self, kind, version):
        with pytest.raises(UnsupportedDocumentVersion, match="unsupported document"):
            load_compat_document(kind, version, {"anything": True})

    def test_non_object_payload_is_refused(self):
        with pytest.raises(UnsupportedDocumentVersion, match="not an object"):
            load_compat_document("run_spec", 1, [1, 2, 3])

    def test_the_error_names_the_supported_surface(self):
        with pytest.raises(UnsupportedDocumentVersion, match="run_spec"):
            load_compat_document("run_spec", 9, {})
