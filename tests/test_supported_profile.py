"""R38-06 (#307) — the frozen supported profile and its cold-install proof.

Two scripts carry the issue's contract —
``scripts/freeze_supported_profile.py`` (the manifest-of-manifests whose
every value traces to an actual receipt) and
``scripts/cold_install_check.py`` (fresh / upgrade / verify). These
tests hold:

- **field sourcing**: every manifest field equals the value in the
  receipt it cites; a contradicting, missing or drifted receipt REFUSES
  the freeze (fail-closed, never a guessed value);
- **fresh-install verification**: the wheel identity gate (a different
  wheel under the SAME version string is the mutable-tag refusal, fired
  BEFORE anything installs), the template rendered FROM the manifest
  (round-trips to the frozen bytes; the moving working tree is a named
  divergence, never the recipe source);
- **upgrade honesty**: a seeded N-1 -> head transition reports its
  ACTUAL edge; an unchanged head is refused as an upgrade claim;
  preservation is per-table counts + sha256 fingerprints with each
  changed table named;
- **the verify arms** (fixtures against the pure check functions): an
  old worker image, a missing shared checkpoint mount on the worker
  only, and a mismatched template each produce a NAMED refusal before
  any model call;
- **round-trip + record validators**: the committed manifest loads
  clean, vouches for itself, and the profile-record validators stay
  green on a record built from its axes.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from cold_install_check import (  # noqa: E402
    CheckRefused,
    FingerprintRow,
    InstalledObservation,
    check_wheel_identity,
    extract_template_section,
    frozen_template,
    load_manifest,
    preflight_refusals,
    preservation_findings,
    recover_frozen_section,
    render_target_template,
    schema_transition_findings,
    template_preflight_findings,
    verify_installed,
)
from freeze_supported_profile import (  # noqa: E402
    SUPPORTED_PROFILE_STAMP,
    CaptureInputs,
    FreezeRefused,
    TemplateReceiptProbe,
    capture_supported_profile,
    recover_template_bytes,
    validate_manifest,
    _TEMPLATE_RECEIPT_HEADER,
    _TEMPLATE_RECEIPT_TAIL_MARKER,
)

from forge.profile_qualification import (  # noqa: E402
    EvidenceEntry,
    ProfileQualificationRecord,
    load_profile_records,
    load_supported_profile,
    load_trace_records,
    supported_profile_binding,
    validate_record,
)

MANIFEST = ROOT / "qualification" / "profiles" / "supported-gitlab-ce-v1.json"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return load_manifest(MANIFEST)


class FakeReceiptProbe(TemplateReceiptProbe):
    """The template-receipt probe, faked: returns the generated CI whose
    middle section is whatever bytes the test pins (default: the frozen
    template)."""

    def __init__(self, template_bytes: str) -> None:
        self.template_bytes = template_bytes

    def fetch_installed_ci_yaml(self, project_id: int) -> str:
        return (
            _TEMPLATE_RECEIPT_HEADER
            + self.template_bytes
            + _TEMPLATE_RECEIPT_TAIL_MARKER
            + "smoke:\n  script: […]\n"
        )


def _inputs_from_repo(template_bytes: str | None = None) -> CaptureInputs:
    probe = FakeReceiptProbe(template_bytes or frozen_template(load_manifest(MANIFEST)))
    return CaptureInputs.from_root(ROOT, probe)


# ---------------------------------------------------------------------------
# The freeze: every field traces to its receipt
# ---------------------------------------------------------------------------


class TestFreezeFieldSourcing:
    def test_committed_manifest_matches_every_cited_receipt(self) -> None:
        """Every frozen value IS the receipt's value — read both, compare."""
        document = load_manifest(MANIFEST)
        promotion = _load(ROOT / "docs/releases/evidence/v0.39.0/promotion.json")
        trace = _load(
            ROOT / "docs/evaluation/2026-09-25-supported-composition-v2/useful-wip-resume-v2.json"
        )
        inventory = _load(ROOT / "qualification/inventory-2026-09-25-v2.json")
        from scripts.freeze_supported_profile import CLOSURE_RECEIPT_CANDIDATES

        closure_path = next(
            (ROOT / c for c in CLOSURE_RECEIPT_CANDIDATES if (ROOT / c).exists()),
            None,
        )
        assert closure_path is not None, "no closure receipt (dist build or committed copy)"
        closure = _load(closure_path)
        record = _load(ROOT / "qualification/records/gitlab-ce-v1@0.39.0.json")

        promoted = document["control_plane"]["promoted"]
        assert promoted["wheel_sha256"] == promotion["wheel_sha256"]
        assert promoted["image_digest"] == promotion["image_digest"]
        assert promoted["source_sha"] == promotion["head_sha"]
        assert promoted["wheel_url"] == promotion["wheel_url"]
        assert promoted["release_version"] == promotion["version"]

        executed = document["control_plane"]["executed_lab"]
        assert (
            executed["image_digest"]
            == inventory["stages"]["control-plane"]["image"]["image_digest"]
        )
        assert executed["reported_version"] == "0.38.0"  # the v2 composition (Q39-07/#326)

        assert document["lane"]["executed_live"]["git_sha"] == trace["task"]["lane_ref"]
        assert document["lane"]["closure"]["closure_digest"] == closure["closure_digest"]
        assert document["harness"]["version"] == record["harness_version"] == "2.1.273"
        assert (
            document["verification_contract"]["verified_candidate_sha"]
            == trace["mr"]["candidate_sha"]
        )
        runner_rows = inventory["stages"]["runner"]["gitlab_runners"]["runners"]
        unraid = next(row for row in runner_rows if row["id"] == 4)
        assert document["runner"]["description"] == unraid["description"] == "unraid"

    def test_capture_reproduces_the_committed_manifest_values(self) -> None:
        inputs = _inputs_from_repo()
        document = capture_supported_profile(inputs)
        committed = load_manifest(MANIFEST)
        # frozen_at differs by design (the instant of the capture); every
        # identity axis must reproduce exactly.
        for section in (
            "control_plane",
            "lane",
            "target_template",
            "runner",
            "harness",
            "model_route",
            "credential_route",
            "verification_contract",
            "evidence_records",
            "exclusions",
        ):
            assert document[section] == committed[section], section

    def test_every_field_traces_to_a_receipt(self, manifest: dict[str, Any]) -> None:
        """Each bound block names its receipt and the receipt exists."""
        receipts: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("receipt", "receipts"):
                        receipts.extend([value] if isinstance(value, str) else value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(manifest)
        assert receipts, "the manifest must cite receipts"
        for receipt in receipts:
            if "::" in receipt or " + " in receipt or receipt.startswith(("http", "forge.")):
                continue  # a module/contract/composite receipt, not a single file
            path = ROOT / receipt.split("#")[0]
            if receipt.startswith(("dist/", "litellm-config")) and not path.exists():
                # working-tree-only artifacts (a local closure build; the
                # gitignored lab gateway config) — the manifest cites the
                # candidate LIST, and the committed fallback beside them is
                # verified below.
                continue
            assert path.exists(), receipt

    def test_missing_receipt_refuses(self, tmp_path: Path) -> None:
        # a vanished receipt tree: point the loader at an empty root
        with pytest.raises(FreezeRefused, match="receipt .* is missing"):
            CaptureInputs.from_root(tmp_path, FakeReceiptProbe(""))

    def test_template_reconstruction_disagreeing_with_the_trace_refuses(self) -> None:
        inputs = _inputs_from_repo(
            template_bytes="forge-agent-claude-sdk:\n  script: a foreign recipe\n"
        )
        with pytest.raises(FreezeRefused, match="disagree; refusing to freeze"):
            capture_supported_profile(inputs)

    def test_record_and_promotion_wheel_disagreement_refuses(self) -> None:
        inputs = _inputs_from_repo()
        mutated = copy.deepcopy(inputs)
        promotion = dict(mutated.promotion)
        promotion["wheel_sha256"] = "b" * 64
        mutated = type(mutated)(**{**mutated.__dict__, "promotion": promotion})
        with pytest.raises(FreezeRefused, match="two wheels under one freeze"):
            capture_supported_profile(mutated)

    def test_wrong_harness_version_refuses(self) -> None:
        inputs = _inputs_from_repo()
        record = dict(inputs.record)
        record["harness_version"] = "2.0.0"
        mutated = type(inputs)(**{**inputs.__dict__, "record": record})
        with pytest.raises(FreezeRefused, match="expected the live-evidenced claude-code 2.1.273"):
            capture_supported_profile(mutated)

    def test_task_shapes_disagreeing_with_the_trace_refuse(self) -> None:
        inputs = _inputs_from_repo()
        mutated = type(inputs)(**{**inputs.__dict__, "task_shapes": (("src/other.py", "new"),)})
        with pytest.raises(FreezeRefused, match="verification contract cannot be bound"):
            capture_supported_profile(mutated)

    def test_recover_template_bytes_is_marker_exact(self) -> None:
        template = "job:\n  script: echo hi\n"
        generated = (
            _TEMPLATE_RECEIPT_HEADER + template + _TEMPLATE_RECEIPT_TAIL_MARKER + "smoke: […]\n"
        )
        assert recover_template_bytes(generated) == template
        with pytest.raises(FreezeRefused, match="deterministic header"):
            recover_template_bytes("not the generator's output\n")


# ---------------------------------------------------------------------------
# The frozen manifest's own contract
# ---------------------------------------------------------------------------


class TestManifestContract:
    def test_manifest_vouches_for_itself(self, manifest: dict[str, Any]) -> None:
        assert validate_manifest(manifest) == []
        body = {k: v for k, v in manifest.items() if k != "manifest_digest"}
        recomputed = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        assert manifest["manifest_digest"] == recomputed

    def test_tampered_bytes_refuse(self, manifest: dict[str, Any]) -> None:
        tampered = copy.deepcopy(manifest)
        other = base64.b64encode(b"different bytes entirely\n").decode()
        tampered["target_template"]["frozen"]["bytes_b64"] = other
        findings = validate_manifest(tampered)
        assert any("does not vouch for itself" in finding for finding in findings)

    def test_human_support_approval_stays_pending(self, manifest: dict[str, Any]) -> None:
        records = manifest["evidence_records"]
        assert set(records) == {
            "source_review",
            "release_canary",
            "native_workflow_qualification",
            "human_support_approval",
        }
        assert records["human_support_approval"]["status"] == "pending"
        approved = copy.deepcopy(manifest)
        approved["evidence_records"]["human_support_approval"]["status"] = "approved"
        assert any("must stay 'pending'" in f for f in validate_manifest(approved))

    def test_the_divergence_must_be_stated(self, manifest: dict[str, Any]) -> None:
        """The same-version-two-compositions defect stays named, never merged."""
        promoted = manifest["control_plane"]["promoted"]["image_digest"]
        executed = manifest["control_plane"]["executed_lab"]["image_digest"]
        assert promoted != executed
        assert manifest["control_plane"]["divergence"]
        merged = copy.deepcopy(manifest)
        merged["control_plane"]["promoted"]["image_digest"] = executed
        assert any("divergence is UNSTATED" in f for f in validate_manifest(merged))

    def test_schema_predecessor_differs_from_head(self, manifest: dict[str, Any]) -> None:
        revision = manifest["control_plane"]["schema_revision"]
        assert revision["head"] == "028"  # 028_credential_receipts (Q39-03/Q39-05)
        assert revision["predecessor"] == "027"
        assert revision["head"] != revision["predecessor"]

    def test_load_manifest_refuses_a_stale_manifest(self, tmp_path: Path) -> None:
        stale = copy.deepcopy(load_manifest(MANIFEST))
        stale["manifest_digest"] = "0" * 64
        path = tmp_path / "supported-gitlab-ce-v1.json"
        path.write_text(json.dumps(stale), encoding="utf-8")
        with pytest.raises(CheckRefused, match="self-inconsistent"):
            load_manifest(path)


# ---------------------------------------------------------------------------
# fresh: the wheel identity gate + the template from the manifest
# ---------------------------------------------------------------------------


class TestFreshInstallVerification:
    def test_wheel_identity_match(self, manifest: dict[str, Any]) -> None:
        sha = manifest["lane"]["wheel"]["sha256"]
        finding = check_wheel_identity(
            expected_sha256=sha,
            actual_sha256=sha,
            filename="forge-0.37.0-py3-none-any.whl",
            version="0.37.0",
        )
        assert finding.severity == "match"
        assert "immutable identity, not a tag" in finding.detail

    def test_mutable_tag_refusal_names_the_defect(self, manifest: dict[str, Any]) -> None:
        """The negative arm: one wheel digest swapped under a CONSTANT
        version string — the check refuses BEFORE anything installs."""
        finding = check_wheel_identity(
            expected_sha256=manifest["lane"]["wheel"]["sha256"],
            actual_sha256="e" * 64,
            filename="forge-0.37.0-py3-none-any.whl",
            version="0.37.0",
        )
        assert finding.severity == "refusal"
        assert "DIFFERENT wheel under the SAME version string" in finding.detail
        assert "refusing BEFORE anything installs" in finding.detail
        assert preflight_refusals([finding])

    def test_template_round_trips_to_the_frozen_bytes(self, manifest: dict[str, Any]) -> None:
        rendered = render_target_template(manifest)
        recovered = recover_frozen_section(rendered)
        assert recovered == frozen_template(manifest)
        assert extract_template_section(rendered) == frozen_template(manifest)
        # and the manifest's own frozen sha is the frozen bytes' sha
        import hashlib as _hashlib

        assert (
            _hashlib.sha256(recovered.encode()).hexdigest()
            == manifest["target_template"]["frozen"]["sha256"]
        )

    def test_mismatched_template_refuses(self, manifest: dict[str, Any]) -> None:
        """A moving working tree's template must never silently replace
        the frozen recipe — the preflight arm fires before a model call.
        (The drifting bytes are a MUTATED copy: on the v2 freeze the
        working tree legitimately equals the frozen bytes — the template
        was recovered FROM it — so the raw tree cannot carry the
        negative arm anymore.)"""
        working_tree = (ROOT / "ci/templates/claude-sdk-lane.gitlab-ci.yml").read_text("utf-8")
        drifted = working_tree.replace("forge-agent-claude-sdk:", "forge-agent-claude-sdk-x:", 1)
        assert drifted != working_tree
        findings = template_preflight_findings(
            rendered_ci_yaml=drifted, manifest=manifest, template_source=drifted
        )
        refusals = preflight_refusals(findings)
        assert refusals and any("MISMATCHED template" in r for r in refusals)

    def test_frozen_template_carries_the_lane_job_and_collector(
        self, manifest: dict[str, Any]
    ) -> None:
        template = frozen_template(manifest)
        assert "forge-agent-claude-sdk:" in template
        assert "--require-generation" in template

    def test_rendered_oracle_asserts_the_manifest_cases(self, manifest: dict[str, Any]) -> None:
        rendered = render_target_template(manifest)
        for text, _expected in manifest["verification_contract"]["slugify_cases"]:
            assert f'("{text}",' in rendered


# ---------------------------------------------------------------------------
# upgrade: the actual edge + per-table preservation
# ---------------------------------------------------------------------------


class TestUpgradePreservation:
    def test_the_actual_transition_matches(self) -> None:
        findings = schema_transition_findings(
            source_head="026", target_head="027", declared_head="027", declared_predecessor="026"
        )
        assert [f.severity for f in findings] == ["match"]
        assert "ACTUAL schema transition 026 -> 027" in findings[0].detail

    def test_an_unchanged_head_never_implies_a_migration(self) -> None:
        findings = schema_transition_findings(
            source_head="027", target_head="027", declared_head="027", declared_predecessor="026"
        )
        refusals = preflight_refusals(findings)
        assert refusals and "UNCHANGED head" in refusals[0]
        assert "never implies a schema upgrade" in refusals[0]

    def test_undisclosed_starting_schema_refuses(self) -> None:
        findings = schema_transition_findings(
            source_head="025", target_head="027", declared_head="027", declared_predecessor="026"
        )
        assert preflight_refusals(findings)

    def test_preservation_equal_fingerprints_match(self) -> None:
        rows = [
            FingerprintRow(table="flow_runs", count=1, digest="a" * 64),
            FingerprintRow(table="run_specs", count=1, digest="b" * 64),
        ]
        findings = preservation_findings(rows, list(rows))
        assert [f.severity for f in findings] == ["match"]
        assert "row counts and sha256 fingerprints equal" in findings[0].detail

    def test_preservation_names_each_changed_table(self) -> None:
        before = [
            FingerprintRow(table="flow_runs", count=1, digest="a" * 64),
            FingerprintRow(table="run_specs", count=1, digest="b" * 64),
        ]
        after = [
            FingerprintRow(table="flow_runs", count=1, digest="a" * 64),
            FingerprintRow(table="run_specs", count=2, digest="c" * 64),  # changed
        ]
        findings = preservation_findings(before, after)
        refusals = preflight_refusals(findings)
        assert refusals and "upgrade.preservation.run_specs" in refusals[0]
        assert "run_specs" in refusals[0]

    def test_a_vanished_table_is_a_refusal(self) -> None:
        before = [FingerprintRow(table="control_commands", count=1, digest="a" * 64)]
        findings = preservation_findings(before, [])
        assert any("VANISHED" in f.detail for f in findings)


# ---------------------------------------------------------------------------
# verify: the named refusal arms (fixtures over the pure check function)
# ---------------------------------------------------------------------------


def _lab_observation(manifest: dict[str, Any]) -> InstalledObservation:
    """The aligned live lab, as the receipts bind it (matches the
    executed_lab identity on every axis)."""
    executed = manifest["control_plane"]["executed_lab"]
    runner = dict(manifest["runner"])
    runner["status"] = runner["observed_status"]
    template = _TEMPLATE_RECEIPT_HEADER + frozen_template(manifest) + _TEMPLATE_TAIL_STUB
    return InstalledObservation(
        app_image_digest=executed["image_digest"],
        worker_image_digest=executed["image_digest"],
        app_reported_version=manifest["control_plane"]["promoted"]["release_version"],
        schema_head=manifest["control_plane"]["schema_revision"]["head"],
        app_caps_numerical=True,
        worker_caps_numerical=True,
        app_data_mount=True,
        worker_data_mount=True,
        runner=runner,
        target_project_template=template,
    )


_TEMPLATE_TAIL_STUB = _TEMPLATE_RECEIPT_TAIL_MARKER + "smoke:\n  script: […]\n"


class TestVerifyArms:
    def test_aligned_lab_verifies_with_named_divergence(self, manifest: dict[str, Any]) -> None:
        findings = verify_installed(manifest, _lab_observation(manifest))
        assert not preflight_refusals(findings)
        assert any(
            f.axis == "control_plane.app_image"
            and f.severity == "match"
            and "matches the executed_lab bind" in f.detail
            for f in findings
        )
        assert any(f.axis == "target_template" and f.severity == "match" for f in findings)

    def test_unknown_image_identity_refuses(self, manifest: dict[str, Any]) -> None:
        """Neither the promoted NOR the executed-lab digest — an unknown
        composition under this version string."""
        observed = _lab_observation(manifest)
        unknown = InstalledObservation(
            **{**observed.__dict__, "app_image_digest": "sha256:" + "f" * 64}
        )
        findings = verify_installed(manifest, unknown)
        refusals = preflight_refusals(findings)
        assert any(
            "is NEITHER the promoted digest" in r and "UNKNOWN composition" in r for r in refusals
        )

    def test_old_worker_image_arm(self, manifest: dict[str, Any]) -> None:
        """An old worker image beside the newer control plane is named
        precisely — parity refused before a model call."""
        observed = _lab_observation(manifest)
        stale = InstalledObservation(
            **{
                **observed.__dict__,
                "worker_image_digest": "sha256:" + "0" * 64,  # an older build
            }
        )
        findings = verify_installed(manifest, stale)
        refusals = preflight_refusals(findings)
        assert any(
            "worker_app_parity" in r and "OLD worker image beside a newer control plane" in r
            for r in refusals
        )

    def test_missing_shared_mount_on_the_worker_only(self, manifest: dict[str, Any]) -> None:
        observed = _lab_observation(manifest)
        torn = InstalledObservation(**{**observed.__dict__, "worker_data_mount": False})
        findings = verify_installed(manifest, torn)
        refusals = preflight_refusals(findings)
        assert any(
            "shared_checkpoint_mount" in r
            and "WORKER's shared checkpoint mount" in r
            and "inconsistent authority" in r
            for r in refusals
        )

    def test_missing_mount_on_the_app_only_is_also_refused(self, manifest: dict[str, Any]) -> None:
        observed = _lab_observation(manifest)
        torn = InstalledObservation(**{**observed.__dict__, "app_data_mount": False})
        findings = verify_installed(manifest, torn)
        assert preflight_refusals(findings)

    def test_mismatched_installed_template_refuses(self, manifest: dict[str, Any]) -> None:
        observed = _lab_observation(manifest)
        foreign = InstalledObservation(
            **{
                **observed.__dict__,
                "target_project_template": (
                    _TEMPLATE_RECEIPT_HEADER
                    + "forge-agent-claude-sdk:\n  script: a hand-patched recipe\n"
                    + _TEMPLATE_TAIL_STUB
                ),
            }
        )
        findings = verify_installed(manifest, foreign)
        refusals = preflight_refusals(findings)
        assert any(
            "target_template" in r and ("MISMATCHED template" in r or "frozen bytes" in r)
            for r in refusals
        )

    def test_unrecognizable_installed_template_refuses(self, manifest: dict[str, Any]) -> None:
        observed = _lab_observation(manifest)
        blob = InstalledObservation(
            **{**observed.__dict__, "target_project_template": "not yaml at all"}
        )
        findings = verify_installed(manifest, blob)
        assert any(
            f.axis == "target_template" and "no recognizable frozen section" in f.detail
            for f in findings
        )

    def test_wrong_schema_head_and_version_refuse(self, manifest: dict[str, Any]) -> None:
        observed = _lab_observation(manifest)
        drifted = InstalledObservation(
            **{**observed.__dict__, "schema_head": "026", "app_reported_version": "0.36.0"}
        )
        findings = verify_installed(manifest, drifted)
        refusals = preflight_refusals(findings)
        assert any("control_plane.schema" in r for r in refusals)
        assert any(
            "control_plane.version" in r and "version TEXT is never sufficient" in r
            for r in refusals
        )

    def test_missing_caps_refuse(self, manifest: dict[str, Any]) -> None:
        observed = _lab_observation(manifest)
        uncapped = InstalledObservation(**{**observed.__dict__, "worker_caps_numerical": False})
        findings = verify_installed(manifest, uncapped)
        assert any(
            f.axis == "budget_caps" and "refuses to start without caps" in f.detail
            for f in findings
        )


# ---------------------------------------------------------------------------
# Round-trip + the profile-record validators on the new artifact
# ---------------------------------------------------------------------------


class TestManifestRoundTrip:
    def test_the_committed_manifest_loads_through_the_store_loader(self) -> None:
        document = load_supported_profile(ROOT)
        assert document is not None
        assert document["schema"] == SUPPORTED_PROFILE_STAMP

    def test_the_manifest_does_not_disturb_the_record_and_trace_stores(self) -> None:
        assert load_profile_records(ROOT)  # loads clean, manifest included nowhere
        assert load_trace_records(ROOT)

    def test_a_record_built_from_the_manifest_passes_the_strict_validators(
        self, manifest: dict[str, Any]
    ) -> None:
        promoted = manifest["control_plane"]["promoted"]
        record = ProfileQualificationRecord(
            record_id="supported-gitlab-ce-v1@binding-check",
            profile="gitlab-ce-v1",
            provider="gitlab",
            release_version=promoted["release_version"],
            provider_version="GitLab CE 19.3.2 (revision 34042bf7d00)",
            runtime_recipe="python-3.13 (uv standalone) on the unraid docker-executor runner",
            harness_binary=manifest["harness"]["binary"],
            harness_version=manifest["harness"]["version"],
            credential_route="bot PAT control plane; read-only lane PAT; BYOK gateway",
            verification_contract="one required job named smoke (six exact slugify cases)",
            capabilities=("real-provider-e2e",),
            evidence=(
                EvidenceEntry(
                    evidence_class="live-provider",
                    capability="real-provider-e2e",
                    outcome="pass",
                    covers="the useful-WIP trace (R38-05), candidate "
                    f"{manifest['verification_contract']['verified_candidate_sha'][:12]}…",
                    executed_at="2026-09-24T23:07:26+00:00",
                    artifact_sha256=promoted["wheel_sha256"],
                ),
            ),
            image_digest=promoted["image_digest"],
            wheel_sha256=promoted["wheel_sha256"],
            runtime_dependency_fingerprint=manifest["lane"]["executed_live"]["install"],
            template_defaults_digest=manifest["target_template"]["frozen"]["sha256"],
            authority_contract_version="filesystem-checkpoint-authority",
            provider_behavior_fingerprint="GitLab CE 19.3.2 observed live",
            legacy=False,
        )
        assert validate_record(record.to_json()) == []

    def test_binding_to_the_cited_record(self) -> None:
        document = load_supported_profile(ROOT)
        assert document is not None
        bindings = supported_profile_binding(document, load_profile_records(ROOT))
        by_axis = {binding.axis: binding for binding in bindings}
        assert by_axis["wheel_sha256"].status == "match"
        assert by_axis["image_digest"].status == "match"
        assert by_axis["harness_version"].status == "match"
        assert by_axis["release_version"].status == "match"

    def test_binding_names_a_divergence_honestly(self, manifest: dict[str, Any]) -> None:
        """A record pinning a DIFFERENT wheel under the same freeze is a
        named divergence, never a silent merge."""
        records = list(load_profile_records(ROOT))
        cited = next(r for r in records if r.record_id == "gitlab-ce-v1@0.37.0")
        swapped = ProfileQualificationRecord(**{**cited.__dict__, "wheel_sha256": "d" * 64})
        bindings = supported_profile_binding(manifest, [swapped])
        wheel = next(b for b in bindings if b.axis == "wheel_sha256")
        assert wheel.status == "divergent"
        assert wheel.manifest_value != wheel.record_value

    def test_binding_without_any_record_is_unbound(self, manifest: dict[str, Any]) -> None:
        bindings = supported_profile_binding(manifest, [])
        assert [b.status for b in bindings] == ["unbound"]
        assert "binds nothing" in bindings[0].note
