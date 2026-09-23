"""ADR-0029 §4 / R32-24: the versioned compatibility fixtures.

Pinned here: every registered fixture LOADS under its supported
read/recovery semantics; run_spec v1/v2 parse to the legacy
(non-executable) view with the SpecLegacy recovery; the old checkpoint
index document recovers under the migration-026 no-backfill contract;
the old control-command rows read with their labelled fallback / exact
binding; an UNKNOWN version raises UnsupportedDocumentVersion (never a
silent best-effort parse); and the inventory lists the complete
kind×version surface.
"""

import pytest

from forge.adaptive.compat_fixtures import (
    CompatCheckpointIndex,
    CompatControlCommand,
    CompatSpecDocument,
    UnsupportedDocumentVersion,
    compat_document,
    compat_inventory,
    load_compat_document,
)
from forge.runs.spec import ExecutableRunSpec

EXPECTED_INVENTORY = [
    ("checkpoint_metadata", 1),
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
