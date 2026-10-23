"""Historical serialized documents exercise the shipped readers directly.

The examples are inert JSON. No parallel parser or synthetic authorization
model survives the removed compatibility registry.
"""

import copy
import json
from pathlib import Path

import httpx
import pytest

from forge.adaptive.checkpoint_repository import (
    FilesystemCheckpointRepository,
    HttpCheckpointRepository,
)
from forge.adaptive.composition_adoption import legacy_attempt_start_view
from forge.adaptive.continuation import (
    ContinuationConflictError,
    ContinuationEvidence,
    decision_history,
    matching_decision,
    normalize_checkpoint_result,
)
from forge.api_checkpoint_channel import CheckpointStore
from forge.lane_driver import _resume_checkpoint_target
from forge.runs.composition import CompositionBoundaryError, ResumeSpec, assert_publication_identity
from forge.runs.spec import SpecInvalid, SpecLegacy, canonical_json_digest, load_verified_spec

FIXTURES = Path(__file__).parent / "fixtures" / "compat_documents.json"


def document(kind, version):
    return json.loads(FIXTURES.read_text())[f"{kind}/v{version}"]


@pytest.mark.parametrize("version", [1, 2])
def test_digest_only_specs_require_new_approval(version):
    old = document("run_spec", version)
    before = copy.deepcopy(old)
    with pytest.raises(SpecLegacy, match="re-approval required"):
        load_verified_spec(document=old, digest=canonical_json_digest(old), schema_version=version)
    assert old == before
    assert "read_scope" not in old and "revision_permission" not in old


@pytest.mark.parametrize("version", [1, 2, 3])
def test_spec_corruption_is_refused_before_any_version_adaptation(version):
    old = document("run_spec", version)
    digest = canonical_json_digest(old)
    old["subject"]["project_id"] = 999
    with pytest.raises(SpecInvalid, match="digest mismatch"):
        load_verified_spec(document=old, digest=digest, schema_version=version)


def test_current_spec_uses_the_real_executable_reader():
    current = document("run_spec", 3)
    digest = canonical_json_digest(current)
    parsed = load_verified_spec(
        document=current, digest=digest, run_spec_digest=digest, schema_version=3
    )
    assert parsed.task_title == "Add a widget"
    assert parsed.provider == "gitlab" and parsed.profile_digest


@pytest.mark.parametrize("version", [0, 4, 999, True, "3"])
def test_unknown_or_malformed_spec_version_never_becomes_current(version):
    current = document("run_spec", 3)
    with pytest.raises(SpecInvalid):
        load_verified_spec(
            document=current, digest=canonical_json_digest(current), schema_version=version
        )


def test_gate_approved_spec_digest_must_match_the_stored_document():
    current = document("run_spec", 3)
    with pytest.raises(SpecInvalid, match="gate-approved"):
        load_verified_spec(
            document=current,
            digest=canonical_json_digest(current),
            run_spec_digest="f" * 64,
            schema_version=3,
        )


def test_missing_current_task_is_corruption_not_a_legacy_upgrade():
    current = document("run_spec", 3)
    current.pop("task")
    with pytest.raises(SpecInvalid):
        load_verified_spec(
            document=current, digest=canonical_json_digest(current), schema_version=3
        )


def write_index(root, value):
    path = root / "works" / "run-77.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def test_old_filesystem_index_keeps_sequence_order_and_read_only_bytes(tmp_path):
    old = document("checkpoint_metadata", 1)
    old["checkpoints"].reverse()
    old["checkpoints"].append({"checkpoint_id": "e" * 64, "sequence": 0, "files": 1})
    path = write_index(tmp_path, old)
    before = path.read_bytes()
    store = CheckpointStore(tmp_path)
    assert store.entry("run-77")["checkpoint_id"] == "b" * 64
    assert [entry["sequence"] for entry in store.list_entries()] == [0, 1, 2]
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"work_id": "run-77", "checkpoints": "nope"},
        {"work_id": "another-work", "checkpoints": []},
    ],
)
async def test_malformed_checkpoint_index_is_corrupt_never_absent(tmp_path, bad):
    write_index(tmp_path, bad)
    repository = FilesystemCheckpointRepository(tmp_path)
    outcome = await repository.lookup_outcome("run-77")
    assert outcome.state == "corrupt"
    assert normalize_checkpoint_result(outcome) == (None, None)


async def test_unreadable_checkpoint_index_is_unavailable_never_absent(tmp_path, monkeypatch):
    path = write_index(tmp_path, document("checkpoint_metadata", 1))
    original = Path.read_text

    def read(target, *args, **kwargs):
        if target == path:
            raise PermissionError("index read denied")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    outcome = await FilesystemCheckpointRepository(tmp_path).lookup_outcome("run-77")
    assert outcome.state == "unavailable"
    assert normalize_checkpoint_result(outcome) == (None, None)


@pytest.mark.parametrize("version,expected", [(1, None), (2, "b" * 64)])
def test_real_lane_reads_legacy_and_exact_resume_commands(httpx_mock, version, expected):
    command = document("control_command", version)
    httpx_mock.add_response(
        url="https://control.test/lane/controls/resume-spec?work_id=run-77",
        json={"command": command},
    )
    target = _resume_checkpoint_target("run-77", "https://control.test", "attempt-token")
    assert target["checkpoint_id"] == expected
    assert not target["refused"] and not target["lookup_failed"]
    if version == 1:
        assert "legacy" in target["note"].lower() or "fallback" in target["note"].lower()
    with pytest.raises(CompositionBoundaryError, match="resume_mode"):
        ResumeSpec.from_payload(command["payload"])


def test_cross_work_resume_binding_is_refused_without_latest_fallback(httpx_mock):
    command = document("control_command", 2)
    command["payload"]["checkpoint_ref"] = "another-work@" + "b" * 64
    httpx_mock.add_response(json={"command": command})
    target = _resume_checkpoint_target("run-77", "https://control.test", "attempt-token")
    assert target["refused"] and target["checkpoint_id"] is None


def test_resume_control_outage_is_unknown_without_latest_fallback(httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("control unavailable"))
    target = _resume_checkpoint_target("run-77", "https://control.test", "attempt-token")
    assert target["lookup_failed"] and target["checkpoint_id"] is None


def test_legacy_attempt_is_audit_only_and_cannot_authorize_publication():
    old = document("attempt_start", 1)
    view = legacy_attempt_start_view(old)
    assert view["execution_attempt_id"] is None
    assert view["source_base_oid"] == "e" * 40
    assert view["identity_strength"] == "source_oid_only"
    with pytest.raises(CompositionBoundaryError, match="audit"):
        assert_publication_identity(
            persisted=old,
            presented_execution_attempt_id="b" * 64,
            presented_source_base_oid="e" * 40,
        )


def test_current_attempt_binds_execution_and_epoch_separately():
    current = document("attempt_start", 2)
    assert legacy_attempt_start_view(current) is None
    assert_publication_identity(
        persisted=current, presented_execution_attempt_id="b" * 64, presented_authority_epoch=3
    )
    with pytest.raises(CompositionBoundaryError, match="epoch"):
        assert_publication_identity(
            persisted=current, presented_execution_attempt_id="b" * 64, presented_authority_epoch=4
        )
    with pytest.raises(CompositionBoundaryError, match="identity"):
        assert_publication_identity(
            persisted=current,
            presented_execution_attempt_id="a" * 64,
            presented_source_base_oid="e" * 40,
        )


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_continuation_history_preserves_original_pin_and_lineage(version):
    old = document("continuation_decision", version)
    history = decision_history(old, subject="run-77")
    assert len(history) == 1
    carried = next(iter(history.values()))
    assert carried["event"]["source_attempt"] == old.get("source_attempt")
    assert carried["event"]["native_command_id"] == old.get("native_command_id")
    assert carried.get("checkpoint_digest") == old.get("checkpoint_digest")
    assert carried["decided_at"] == old["decided_at"]


def test_continuation_replay_keeps_pinned_bytes_and_refuses_changed_evidence():
    old = document("continuation_decision", 2)
    evidence = ContinuationEvidence(
        vendor_started=True,
        checkpoint_committed=True,
        source_attempt=3,
        native_command_id=old["native_command_id"],
        checkpoint_digest="d" * 64,
        subject="run-77",
    )
    old["evidence_digest"] = evidence.digest()
    replay = matching_decision(old, evidence)
    assert replay.reused and replay.evidence.checkpoint_digest == "c" * 64
    old["evidence_digest"] = "f" * 64
    with pytest.raises(ContinuationConflictError):
        matching_decision(old, evidence)
    old["mode"] = "maybe"
    assert matching_decision(old, evidence) is None


@pytest.mark.parametrize(
    "status,state", [(404, "absent"), (403, "unauthorized"), (503, "unavailable"), (500, "corrupt")]
)
async def test_current_checkpoint_reader_keeps_absence_and_failures_distinct(status, state):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as client:
        repository = HttpCheckpointRepository(
            base_url="https://control.test", token="attempt-token", client=client
        )
        outcome = await repository.lookup_outcome("run-77")
    assert outcome.state == state
    assert normalize_checkpoint_result(outcome) == (
        (False, None) if state == "absent" else (None, None)
    )


async def test_current_checkpoint_reader_refuses_an_invented_content_address():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"checkpoint_id": "short"})
        )
    ) as client:
        outcome = await HttpCheckpointRepository(
            base_url="https://control.test", token="attempt-token", client=client
        ).lookup_outcome("run-77")
    assert outcome.state == "corrupt"
