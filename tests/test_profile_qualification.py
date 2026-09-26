"""R36-22 (#281) — profile qualification from separate evidence records.

A promoted release artifact qualifies the DIGEST; these tests hold the
OTHER contract — the profile-qualification record store
(:mod:`forge.profile_qualification`, ``qualification/records/``):

- record round-trip under the versioned stamp, malformed shapes refused;
- verdict derivation per evidence-class combination, with the SUBSTITUTION
  rules enforced: model-fixture alone caps at ``declared_only``,
  offline-operational (and pg-integration) at ``lab-qualified``,
  ``supported`` needs live-provider-or-stronger evidence for EVERY
  required capability — a fixture never stands in for live results;
- requalification triggers per change type (runtime dependency
  fingerprint, template defaults, authority contract, provider behavior),
  degrading the verdict to ``unqualified`` with the trigger named — as a
  view, never a mutation of history;
- upgrade-claim honesty: a same-head preservation run (027→027, the
  v0.35.0 canary) NEVER labels itself a schema upgrade; a transition
  without seeded records makes no claim at all;
- the gaps join: a profile-class capability uncovered (or covered only by
  declared_only evidence) stays a NAMED gap; executed live evidence clears;
- the skip-refusal hook: a required evidence entry marked ``skip`` refuses
  the profile's promotion even with core CI green;
- honest handling of image-only historical releases and of deleted
  referenced evidence (degrade, never crash, never mutate).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.profile_qualification import (
    CAPABILITIES_BEGIN,
    CAPABILITIES_END,
    EVIDENCE_CLASSES,
    MANIFEST_STAMP,
    MANIFEST_STATUSES,
    MANIFEST_TRIGGER_AXES,
    PROFILE_RECORD_STAMP,
    SUPPORTED_PROFILE_STAMP,
    TESTED_SHA_OBSERVABILITY,
    TRACE_RECORD_STAMP,
    UNMET_CAPABILITIES_OBSERVABILITY,
    WITHDRAWN_OBSERVABILITY,
    EvidenceEntry,
    ProfileApproval,
    ProfilePromotionRefusal,
    ProfileQualificationRecord,
    ProfileRecordError,
    ProfileRecordImmutableError,
    ProfileRecordStore,
    RefusalResolution,
    SupportedProfileEntry,
    TraceRecord,
    UpgradeClaim,
    UpgradeFacts,
    ValidationFinding,
    archive_reference_gaps,
    build_supported_profiles,
    derive_evidence_tier,
    derive_verdict,
    evaluate_record,
    latest_record_per_profile,
    load_profile_approvals,
    load_profile_records,
    load_supported_profile,
    load_trace_records,
    manifest_trigger_triggers,
    profile_promotion_refusals,
    requalification_required,
    requalification_triggers,
    render_capabilities,
    supported_profile_binding,
    upgrade_claim,
    validate_record,
    write_profile_record,
)
from forge.release_promotion import qualification_gaps

ROOT = Path(__file__).resolve().parents[1]

SHA = "1" * 40
CAP = "real-provider-e2e"


def _entry(evidence_class: str, outcome: str = "pass", capability: str = CAP) -> EvidenceEntry:
    return EvidenceEntry(
        evidence_class=evidence_class,
        capability=capability,
        outcome=outcome,
        covers=f"trace for {evidence_class} (trace-id-1)",
        executed_at=SHA,
    )


def _record(
    evidence: tuple[EvidenceEntry, ...],
    *,
    capabilities: tuple[str, ...] = (CAP,),
    release_version: str = "0.35.0",
    provider: str = "gitlab",
    **overrides: object,
) -> ProfileQualificationRecord:
    values: dict = dict(
        record_id=f"probe@{release_version}",
        profile="probe",
        provider=provider,
        release_version=release_version,
        provider_version="GitLab CE 19.3.2 (revision 34042bf7d00)" if provider != "*" else "",
        runtime_recipe="python-3.13",
        harness_binary="claude-code",
        harness_version="2.1.273",
        credential_route="bot PAT / read-only clone PAT / BYOK",
        verification_contract="required-jobs=smoke",
        capabilities=capabilities,
        evidence=evidence,
    )
    values.update(overrides)
    return ProfileQualificationRecord(**values)


# ---------------------------------------------------------------------------
# Record round-trip and shape validation
# ---------------------------------------------------------------------------


def test_a_record_round_trips_under_the_versioned_stamp() -> None:
    record = _record(
        (_entry("live-provider"),),
        provider_behavior_fingerprint="GitLab CE 19.3.2",
        evidence_refs=("docs/releases/evidence/v0.35.0/promotion.json",),
        upgrade=UpgradeFacts("027", "027", ("flow_runs: 1",), ("row counts",), "promotion.json"),
    )
    document = record.to_json()
    assert document["stamp"] == PROFILE_RECORD_STAMP
    loaded = ProfileQualificationRecord.from_json(json.loads(json.dumps(document)))
    assert loaded == record
    # There is deliberately NO verdict field — the verdict is derived, never
    # stored, so an edited record cannot assert what its evidence denies.
    assert "verdict" not in document
    assert derive_verdict(loaded) == "supported"


def test_loading_refuses_a_foreign_stamp(tmp_path: Path) -> None:
    store = tmp_path / "qualification" / "records"
    store.mkdir(parents=True)
    (store / "foreign.json").write_text(json.dumps({"stamp": "other/9"}), encoding="utf-8")
    with pytest.raises(ProfileRecordError, match="stamp"):
        load_profile_records(tmp_path)


def test_an_off_vocabulary_evidence_class_is_refused() -> None:
    with pytest.raises(ProfileRecordError, match="unknown evidence class"):
        _record((_entry("live-e2e-trust-me"),))


def test_a_record_without_required_capabilities_qualifies_nothing() -> None:
    with pytest.raises(ProfileRecordError, match="qualifying nothing"):
        _record((), capabilities=())


def test_a_provider_profile_must_name_its_provider_version() -> None:
    with pytest.raises(ProfileRecordError, match="provider_version"):
        _record((), provider="gitlab", provider_version="")


def test_the_closure_digest_is_the_r3610_field_or_empty() -> None:
    with pytest.raises(ProfileRecordError, match="closure_digest"):
        _record((), closure_digest="not-a-digest")
    assert _record((), closure_digest="a" * 64).closure_digest == "a" * 64
    # empty is the honest wheel-pinned state, never a defaulted digest:
    assert _record(()).closure_digest == ""


def test_evidence_refs_stay_repo_relative() -> None:
    with pytest.raises(ProfileRecordError, match="repo-relative"):
        _record((), evidence_refs=("/etc/passwd",))


# ---------------------------------------------------------------------------
# Verdict derivation: the evidence-class lattice, per combination
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("evidence_class", "expected"),
    [
        ("binary-smoke", "declared_only"),
        ("model-fixture", "declared_only"),
        ("pg-integration", "lab-qualified"),
        ("offline-operational", "lab-qualified"),
        ("live-provider", "supported"),
        ("customer-acceptance", "supported"),
    ],
)
def test_verdict_derivation_per_evidence_class(evidence_class: str, expected: str) -> None:
    """One required capability, one passing entry: the class alone decides."""
    assert derive_verdict(_record((_entry(evidence_class),))) == expected


def test_no_evidence_at_all_is_unqualified() -> None:
    report = evaluate_record(_record(()))
    assert report.verdict == "unqualified"
    assert any("no evidence entry" in reason for reason in report.reasons)


def test_a_failing_entry_never_qualifies() -> None:
    report = evaluate_record(_record((_entry("live-provider", "fail"),)))
    assert report.verdict == "unqualified"
    assert any("no passing evidence" in reason for reason in report.reasons)


def test_a_skipped_entry_alone_is_not_a_pass() -> None:
    report = evaluate_record(_record((_entry("live-provider", "skip"),)))
    assert report.verdict == "unqualified"
    assert any("SKIPPED" in reason for reason in report.reasons)


def test_the_weakest_required_capability_decides_the_record() -> None:
    """supported requires live-or-stronger evidence for EVERY required
    capability — a live sibling never auto-supports a fixture-only one."""
    record = _record(
        (
            _entry("live-provider", capability="cap-live"),
            _entry("model-fixture", capability="cap-fixture"),
        ),
        capabilities=("cap-live", "cap-fixture"),
    )
    report = evaluate_record(record)
    assert report.verdict == "declared_only"
    contributions = dict(report.per_capability)
    assert contributions == {"cap-live": "supported", "cap-fixture": "declared_only"}
    assert any("capped at declared_only" in reason for reason in report.reasons)


def test_offline_operational_caps_the_record_at_lab_qualified() -> None:
    record = _record(
        (
            _entry("offline-operational", capability="cap-a"),
            _entry("pg-integration", capability="cap-b"),
        ),
        capabilities=("cap-a", "cap-b"),
    )
    assert derive_verdict(record) == "lab-qualified"


def test_live_provider_supports_only_when_complete() -> None:
    complete = _record(
        (
            _entry("live-provider", capability="cap-a"),
            _entry("live-provider", capability="cap-b"),
        ),
        capabilities=("cap-a", "cap-b"),
    )
    assert derive_verdict(complete) == "supported"
    incomplete = _record(
        (
            _entry("live-provider", capability="cap-a"),
            _entry("live-provider", capability="cap-b", outcome="skip"),
        ),
        capabilities=("cap-a", "cap-b"),
    )
    # the skipped capability has no passing evidence -> the record is
    # unqualified, and the skip is on record in the reasons:
    report = evaluate_record(incomplete)
    assert report.verdict == "unqualified"
    assert any("SKIPPED" in reason for reason in report.reasons)


def test_fixture_only_evidence_caps_the_record_at_declared_only() -> None:
    """The headline substitution rule (the review's exact demand): authored
    cohort/model fixtures alone — however many, however green — cap the
    record at ``declared_only``; they are a different evidence class from
    live task or recovery results and never substitute for one."""
    fixture_only = _record(
        (
            _entry("model-fixture", capability="cap-a"),
            _entry("model-fixture", capability="cap-b"),
            _entry("model-fixture", capability="cap-c"),
        ),
        capabilities=("cap-a", "cap-b", "cap-c"),
    )
    report = evaluate_record(fixture_only)
    assert report.verdict == "declared_only"
    assert all(contribution == "declared_only" for _, contribution in report.per_capability)
    assert any("capped at declared_only" in reason for reason in report.reasons)
    # and one live entry for the same required capability is what lifts it:
    promoted = _record(
        (
            _entry("model-fixture", capability="cap-a"),
            _entry("live-provider", capability="cap-a"),
        ),
        capabilities=("cap-a",),
    )
    assert derive_verdict(promoted) == "supported"


def test_customer_acceptance_is_live_or_stronger() -> None:
    assert derive_verdict(_record((_entry("customer-acceptance"),))) == "supported"


def test_the_substitution_rules_are_enforced_not_asserted() -> None:
    """A record whose JSON was hand-strengthened still derives the honest
    verdict — the derived verdict is recomputed from evidence on read."""
    document = _record((_entry("model-fixture"),)).to_json()
    document["note"] = "supported on this profile, definitely"
    assert derive_verdict(ProfileQualificationRecord.from_json(document)) == "declared_only"


# ---------------------------------------------------------------------------
# Requalification triggers
# ---------------------------------------------------------------------------


def _pinned_record() -> ProfileQualificationRecord:
    return _record(
        (_entry("live-provider"),),
        runtime_dependency_fingerprint="sha256:" + "d" * 58,
        template_defaults_digest="sha256:" + "e" * 58,
        authority_contract_version="checkpoint-repository/1",
        provider_behavior_fingerprint="GitLab CE 19.3.2 (revision 34042bf7d00)",
    )


@pytest.mark.parametrize(
    ("axis", "changed"),
    [
        ("runtime_dependency_fingerprint", "sha256:" + "f" * 58),
        ("template_defaults_digest", "sha256:" + "a" * 58),
        ("authority_contract_version", "checkpoint-repository/2"),
        ("provider_behavior_fingerprint", "GitLab CE 19.4.0 (revision abc)"),
    ],
)
def test_requalification_triggers_per_change_type(axis: str, changed: str) -> None:
    record = _pinned_record()
    assert requalification_required(record, {axis: changed})
    triggers = requalification_triggers(record, {axis: changed})
    assert len(triggers) == 1, triggers
    assert axis in triggers[0] and "requalification required" in triggers[0]
    # the affected record's verdict degrades to unqualified, trigger named:
    report = evaluate_record(record, changes={axis: changed})
    assert report.verdict == "unqualified"
    assert triggers[0] in report.reasons


def test_unchanged_axes_trigger_nothing() -> None:
    record = _pinned_record()
    assert requalification_triggers(record, {}) == ()
    assert not requalification_required(record, {})
    same = {
        "runtime_dependency_fingerprint": record.runtime_dependency_fingerprint,
        "template_defaults_digest": record.template_defaults_digest,
        "authority_contract_version": record.authority_contract_version,
        "provider_behavior_fingerprint": record.provider_behavior_fingerprint,
    }
    assert requalification_triggers(record, same) == ()
    assert not requalification_required(record, same)
    assert derive_verdict(record, changes=same) == "supported"


def test_an_unpinned_axis_never_silently_absorbs_a_change() -> None:
    record = _record((_entry("live-provider"),))  # every axis unpinned
    triggers = requalification_triggers(
        record, {"authority_contract_version": "checkpoint-repository/2"}
    )
    assert len(triggers) == 1
    assert "unpinned" in triggers[0]
    assert derive_verdict(
        record, changes={"authority_contract_version": "checkpoint-repository/2"}
    ) == ("unqualified")


def test_degradation_is_a_view_and_never_mutates_history(tmp_path: Path) -> None:
    record = _pinned_record()
    path = write_profile_record(tmp_path, record)
    before = path.read_bytes()
    assert (
        derive_verdict(record, changes={"provider_behavior_fingerprint": "other"}) == "unqualified"
    )
    assert path.read_bytes() == before, "degradation never rewrites the record"
    # and the original verdict is still derived when the world is unchanged:
    assert derive_verdict(load_profile_records(tmp_path)[0]) == "supported"


# ---------------------------------------------------------------------------
# Upgrade-claim honesty
# ---------------------------------------------------------------------------


def test_a_same_head_run_never_labels_itself_a_schema_upgrade() -> None:
    """The v0.35.0 note: a 027→027 canary is same-head PRESERVATION, never
    a schema-upgrade proof."""
    record = _record(
        (_entry("binary-smoke"),),
        upgrade=UpgradeFacts(
            source_schema="027",
            target_schema="027",
            seeded_records=("flow_runs: 1", "run_specs: 1", "step_runs: bounded set"),
            preservation_checks=("row counts", "sha256 row-identity fingerprints"),
            evidence_ref="docs/releases/evidence/v0.35.0/promotion.json",
        ),
    )
    claim = upgrade_claim(record)
    assert claim is not None
    assert claim.kind == "same-head-preservation"
    assert not claim.is_schema_upgrade
    assert "same-head-preservation" in json.dumps(claim.to_json())
    assert "schema-transition" not in json.dumps(claim.to_json())


def test_a_real_transition_claims_schema_upgrade_with_its_seeded_records() -> None:
    record = _record(
        (_entry("binary-smoke"),),
        upgrade=UpgradeFacts(
            source_schema="026",
            target_schema="027",
            seeded_records=("flow_runs: 1", "checkpoint_metadata: 2"),
            preservation_checks=("row counts", "sha256 row-identity fingerprints"),
            evidence_ref="docs/releases/evidence/v0.36.0/promotion.json",
        ),
    )
    claim = upgrade_claim(record)
    assert claim is not None
    assert claim.kind == "schema-transition"
    assert claim.is_schema_upgrade
    assert claim.source_schema == "026" and claim.target_schema == "027"
    assert "checkpoint_metadata: 2" in claim.seeded_records


def test_a_transition_that_seeded_nothing_makes_no_claim_at_all() -> None:
    """The v0.33.0 canary migrated 024→026 but seeded nothing — an upgrade
    claim must name representative seeded records and executed checks."""
    record = _record(
        (_entry("binary-smoke"),),
        upgrade=UpgradeFacts("024", "026", (), (), "docs/releases/evidence/v0.33.0/promotion.json"),
    )
    assert upgrade_claim(record) is None


def test_a_record_without_upgrade_facts_makes_no_claim() -> None:
    assert upgrade_claim(_record((_entry("live-provider"),))) is None


def test_a_schema_transition_claim_cannot_be_constructed_without_seeded_records() -> None:
    with pytest.raises(ProfileRecordError, match="seeded records"):
        UpgradeClaim(
            kind="schema-transition",
            source_schema="026",
            target_schema="027",
            seeded_records=(),
            preservation_checks=("row counts",),
            evidence_ref="x",
        )


# ---------------------------------------------------------------------------
# The skip-refusal hook
# ---------------------------------------------------------------------------


def test_a_skipped_required_evidence_refuses_the_profile_promotion() -> None:
    """The R36-22 negative test: a required profile test made to skip
    refuses the profile's promotion EVEN WITH everything else green."""
    record = _record(
        (
            _entry("live-provider", capability="cap-ok"),
            _entry("live-provider", capability="cap-skipped", outcome="skip"),
        ),
        capabilities=("cap-ok", "cap-skipped"),
    )
    refusals = profile_promotion_refusals((record,))
    assert len(refusals) == 1
    refusal = refusals[0]
    assert refusal.profile == "probe"
    assert any(
        "SKIPPED" in reason and "even with core CI green" in reason for reason in refusal.reasons
    )


def test_a_failing_required_evidence_refuses_the_profile_promotion() -> None:
    record = _record((_entry("live-provider", "fail"),))
    refusals = profile_promotion_refusals((record,))
    assert len(refusals) == 1
    assert any("FAILED" in reason for reason in refusals[0].reasons)


def test_a_clean_record_is_not_refused() -> None:
    assert profile_promotion_refusals((_record((_entry("live-provider"),)),)) == ()


def test_only_the_latest_record_per_profile_is_judged() -> None:
    """An older record replayed beside a newer one never wins: ordering
    comes from the release version, not from file order."""
    old = _record(
        (_entry("live-provider", "skip"),),
        release_version="0.34.0",
    )
    new = _record((_entry("live-provider"),), release_version="0.35.0")
    assert profile_promotion_refusals((old, new)) == ()
    assert profile_promotion_refusals((new, old)) == ()
    latest = latest_record_per_profile((old, new))
    assert [record.record_id for record in latest] == [new.record_id]


def test_the_cli_gate_refuses_on_a_skipped_record_and_passes_the_committed_store(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from forge import profile_qualification as pq

    write_profile_record(tmp_path, _record((_entry("live-provider", "skip"),)))
    assert pq.main(["gate", "--root", str(tmp_path)]) == 1
    assert "REFUSED" in capsys.readouterr().err
    # the committed store carries no skip/fail/missing required evidence:
    assert pq.main(["gate", "--root", str(ROOT)]) == 0


# ---------------------------------------------------------------------------
# The store: loading, immutability, references
# ---------------------------------------------------------------------------


def test_the_committed_store_loads_and_every_reference_exists() -> None:
    records = load_profile_records(ROOT)
    assert len(records) >= 4
    versions = [record.release_version for record in records]
    assert versions == sorted(versions, key=lambda v: tuple(int(p) for p in v.split(".")[:3]))
    for record in records:
        assert record.to_json()["stamp"] == PROFILE_RECORD_STAMP
        for ref in record.evidence_refs:
            assert (ROOT / ref).exists(), f"{record.record_id}: dangling evidence ref {ref}"


def test_a_deleted_referenced_artifact_degrades_without_mutating_history(
    tmp_path: Path,
) -> None:
    record = _record(
        (_entry("live-provider"),),
        evidence_refs=("docs/releases/evidence/v0.35.0/promotion.json",),
    )
    write_profile_record(tmp_path, record)
    path = tmp_path / "qualification" / "records" / f"{record.record_id}.json"
    before = path.read_bytes()
    # the referenced archive is deleted from THIS tree copy:
    assert archive_reference_gaps(record, tmp_path) != ()
    report = evaluate_record(record, root=tmp_path)
    assert report.verdict == "unqualified"
    assert any("does not exist" in reason for reason in report.reasons)
    # the loader never crashes on it and the record on disk is untouched:
    assert len(load_profile_records(tmp_path)) == 1
    assert path.read_bytes() == before


def test_the_store_is_immutable_like_the_release_archive(tmp_path: Path) -> None:
    record = _record((_entry("live-provider"),))
    write_profile_record(tmp_path, record)
    changed = _record((_entry("model-fixture"),))
    with pytest.raises(ProfileRecordError, match="immutable"):
        write_profile_record(tmp_path, changed)
    # byte-identical rewrites are no-ops; superseding needs the flag:
    write_profile_record(tmp_path, record)
    write_profile_record(tmp_path, changed, replace=True)
    assert derive_verdict(load_profile_records(tmp_path)[0]) == "declared_only"


def test_a_missing_store_is_no_records_not_an_error(tmp_path: Path) -> None:
    assert load_profile_records(tmp_path) == ()


def test_image_only_historical_releases_are_handled_honestly() -> None:
    """The store's v0.33.0/v0.34.0 records describe IMAGE-ONLY releases:
    no wheel identity is invented (empty, stated), the canary evidence
    names the image digest it executed at, and neither relies on the
    latest-archive ordering — each record stands on its own version."""
    records = {record.record_id: record for record in load_profile_records(ROOT)}
    for version in ("0.33.0", "0.34.0"):
        record = records[f"release-artifact-canary@{version}"]
        assert record.wheel_sha256 == "", f"v{version} was image-only — no wheel identity"
        assert record.image_digest.startswith("sha256:")
        assert all(entry.executed_at == record.image_digest for entry in record.evidence)
    wheel_bearing = records["release-artifact-canary@0.35.0"]
    assert wheel_bearing.wheel_sha256 == (
        "1e365612473426a2130000784a6cd8c707ffcedae7f8f6bcb34c3f75791700d9"
    )
    # the real v0.35.0 canary claim stays what it was: same-head preservation.
    claim = upgrade_claim(wheel_bearing)
    assert claim is not None and claim.kind == "same-head-preservation"
    assert (claim.source_schema, claim.target_schema) == ("027", "027")


def test_the_real_gitlab_record_is_declared_only_and_says_why() -> None:
    """The live flow was refused at preflight, so NO live-provider entry
    exists — the machine record keeps the honest verdict."""
    gitlab = next(
        record for record in load_profile_records(ROOT) if record.profile == "gitlab-ce-v1"
    )
    assert derive_verdict(gitlab) == "declared_only"
    classes = {entry.evidence_class for entry in gitlab.evidence}
    assert "live-provider" not in classes
    assert "customer-acceptance" not in classes
    assert "refused at preflight" in gitlab.note.lower()


# ---------------------------------------------------------------------------
# The gaps join: profile-class capabilities covered / uncovered
# ---------------------------------------------------------------------------


class _Entry:
    """A minimal manifest-entry stand-in (the same duck shape the real
    manifest entries carry)."""

    def __init__(self, capability: str, provider: str, evidence_class: str) -> None:
        self.capability = capability
        self.provider = provider
        self.backend = "*"
        self.evidence_class = evidence_class


_LIVE_ENTRIES = (
    _Entry("real-provider-e2e", "gitlab", "real_provider_e2e"),
    _Entry("real-provider-e2e", "github", "real_provider_e2e"),
)


def test_an_uncovered_profile_capability_is_a_named_gap() -> None:
    records = (_record((_entry("live-provider"),), provider="gitlab"),)
    gaps = qualification_gaps(_LIVE_ENTRIES, (), version="0.35.0", profile_records=records)
    by_key = {(gap.capability, gap.provider): gap for gap in gaps}
    assert ("real-provider-e2e", "github") in by_key
    reason = by_key[("real-provider-e2e", "github")].reason
    assert "no profile-qualification record covers" in reason
    assert "github" in reason  # a gitlab record never auto-supports github


def test_a_covered_profile_capability_clears_the_gap() -> None:
    records = (
        _record((_entry("live-provider"),), provider="gitlab"),
        _record((_entry("live-provider"),), provider="github"),
    )
    gaps = qualification_gaps(_LIVE_ENTRIES, (), version="0.35.0", profile_records=records)
    assert gaps == ()


def test_declared_only_coverage_keeps_the_gap_and_names_the_reason() -> None:
    records = (_record((_entry("model-fixture"),), provider="gitlab"),)
    gaps = qualification_gaps(_LIVE_ENTRIES, (), version="0.35.0", profile_records=records)
    by_key = {(gap.capability, gap.provider): gap for gap in gaps}
    reason = by_key[("real-provider-e2e", "gitlab")].reason
    assert "declared_only" in reason
    assert "never substitutes" in reason


def test_lab_qualified_coverage_clears_the_gap() -> None:
    records = (
        _record((_entry("offline-operational"),), provider="gitlab"),
        _record((_entry("pg-integration"),), provider="github"),
    )
    gaps = qualification_gaps(_LIVE_ENTRIES, (), version="0.35.0", profile_records=records)
    assert gaps == ()


def test_stale_profile_records_do_not_clear_the_gap() -> None:
    records = (_record((_entry("live-provider"),), provider="gitlab", release_version="0.34.0"),)
    gaps = qualification_gaps(_LIVE_ENTRIES, (), version="0.35.0", profile_records=records)
    reason = {(gap.capability, gap.provider): gap.reason for gap in gaps}[
        ("real-provider-e2e", "gitlab")
    ]
    assert "stale" in reason


def test_an_unqualified_record_does_not_clear_the_gap() -> None:
    records = (_record((_entry("live-provider", "fail"),), provider="gitlab"),)
    gaps = qualification_gaps(_LIVE_ENTRIES, (), version="0.35.0", profile_records=records)
    reason = {(gap.capability, gap.provider): gap.reason for gap in gaps}[
        ("real-provider-e2e", "gitlab")
    ]
    assert "unqualified" in reason


def test_a_skipped_profile_evidence_keeps_the_gap_and_names_the_skip() -> None:
    records = (_record((_entry("live-provider", "skip"),), provider="gitlab"),)
    gaps = qualification_gaps(_LIVE_ENTRIES, (), version="0.35.0", profile_records=records)
    reason = {(gap.capability, gap.provider): gap.reason for gap in gaps}[
        ("real-provider-e2e", "gitlab")
    ]
    assert "unqualified" in reason and "SKIPPED" in reason


def test_without_profile_records_the_promotion_path_is_unchanged() -> None:
    """Additive contract: no profile records in scope -> the pre-R36-22
    promotion-record logic keeps its exact behavior."""
    from forge.release_promotion import CanaryResult, PromotionDecision, PromotionRecord

    promotion = PromotionRecord(
        version="0.35.0",
        image_ref="ghcr.io/forcewake/forge",
        image_digest="sha256:" + "a" * 64,
        canary=(CanaryResult("fresh", "release-artifact-canary", "pass"),),
        decision=PromotionDecision(verdict="promote"),
    )
    entries = _LIVE_ENTRIES + (_Entry("release-artifact-canary", "*", "boot_canary"),)
    gaps = qualification_gaps(entries, (promotion,), version="0.35.0")
    assert {gap.capability for gap in gaps} == {"real-provider-e2e"}
    assert all("no promotion record tags canary evidence" in gap.reason for gap in gaps)


# ---------------------------------------------------------------------------
# The capabilities render: derived, drift-checked
# ---------------------------------------------------------------------------


def test_the_committed_capabilities_table_matches_the_records() -> None:
    """The doc's fenced table is what the committed records derive — a
    hand-strengthened verdict line is the drift this removes. The Tiers
    column is derived from the committed EXECUTED traces
    (qualification/traces/) exactly as the CLI renders it."""
    content = (ROOT / "docs" / "releases" / "profile-records.md").read_text(encoding="utf-8")
    begin = content.index(CAPABILITIES_BEGIN)
    end = content.index(CAPABILITIES_END)
    fenced = content[begin + len(CAPABILITIES_BEGIN) + 1 : end].rstrip("\n")
    assert fenced == render_capabilities(load_profile_records(ROOT), load_trace_records(ROOT))


def test_the_capabilities_render_names_verdict_and_limiting_class() -> None:
    table = render_capabilities(
        (
            _record((_entry("model-fixture"),), profile="alpha"),
            _record((_entry("live-provider"),), profile="beta"),
        )
    )
    alpha_row = next(line for line in table.splitlines() if "| alpha |" in line)
    assert "declared_only" in alpha_row and "model-fixture" in alpha_row
    beta_row = next(line for line in table.splitlines() if "| beta |" in line)
    assert "supported" in beta_row and "live-provider" in beta_row


# ---------------------------------------------------------------------------
# The evidence-class vocabulary stays intact
# ---------------------------------------------------------------------------


def test_the_evidence_class_vocabulary_is_the_review_six() -> None:
    assert EVIDENCE_CLASSES == (
        "binary-smoke",
        "model-fixture",
        "pg-integration",
        "offline-operational",
        "live-provider",
        "customer-acceptance",
    )


def test_profile_promotion_refusal_shape_round_trips() -> None:
    refusal = ProfilePromotionRefusal(
        profile="probe", record_id="probe@0.35.0", reasons=("skipped",)
    )
    assert refusal.to_json() == {
        "profile": "probe",
        "record_id": "probe@0.35.0",
        "reasons": ["skipped"],
    }


# ---------------------------------------------------------------------------
# R37-06 (#287) — strict record validation (the #298 overlap slice)
# ---------------------------------------------------------------------------


ISO = "2026-09-24T12:00:00+00:00"
HEX64 = "b" * 64


def _strict_entry(
    evidence_class: str = "live-provider", outcome: str = "pass", capability: str = CAP
) -> EvidenceEntry:
    return EvidenceEntry(
        evidence_class=evidence_class,
        capability=capability,
        outcome=outcome,
        covers=f"trace for {evidence_class} (trace-id-1)",
        executed_at=ISO,
        artifact_sha256=HEX64,
    )


def _strict_record(**overrides: object) -> ProfileQualificationRecord:
    values: dict = dict(
        record_id="probe@0.36.0",
        profile="probe",
        provider="gitlab",
        release_version="0.36.0",
        provider_version="GitLab CE 19.3.2 (revision 34042bf7d00)",
        runtime_recipe="python-3.13",
        harness_binary="claude-code",
        harness_version="2.1.273",
        credential_route="bot PAT / read-only clone PAT / BYOK",
        verification_contract="required-jobs=smoke",
        capabilities=(CAP,),
        evidence=(_strict_entry(),),
        runtime_dependency_fingerprint="closure-abc123",
        template_defaults_digest=HEX64,
        authority_contract_version="checkpoint-authority/1",
        provider_behavior_fingerprint="GitLab CE 19.3.2 (revision 34042bf7d00)",
        legacy=False,
    )
    values.update(overrides)
    return ProfileQualificationRecord(**values)


def test_a_strict_record_validates_clean_and_round_trips() -> None:
    record = _strict_record()
    document = record.to_json()
    assert validate_record(document) == []
    assert document["legacy"] is False
    assert document["evidence"][0]["artifact_sha256"] == HEX64
    assert ProfileQualificationRecord.from_json(json.loads(json.dumps(document))) == record


def test_a_digest_in_a_timestamp_field_is_a_finding_naming_it() -> None:
    record = _strict_record(
        evidence=(
            EvidenceEntry(
                evidence_class="live-provider",
                capability=CAP,
                outcome="pass",
                covers="trace",
                executed_at="1e365612473426a2130000784a6cd8c707ffcedae7f8f6bcb34c3f75791700d9",
                artifact_sha256=HEX64,
            ),
        )
    )
    (finding,) = validate_record(record.to_json())
    assert finding.kind == "timestamp-not-iso"
    assert finding.path == "evidence[0].executed_at"
    assert "1e36561247342" in finding.message  # the offending digest is NAMED


def test_non_hex_hash_fields_are_findings() -> None:
    record = _strict_record(wheel_sha256="not-a-digest", image_digest="sha256:zz")
    findings = validate_record(record.to_json())
    by_path = {finding.path for finding in findings}
    assert by_path == {"wheel_sha256", "image_digest"}
    assert all(finding.kind == "hash-not-hex64" for finding in findings)
    # closure_digest is constructor-guarded (never reaches validation); the
    # validator still checks it when handed a raw document:
    document = _strict_record().to_json()
    document["closure_digest"] = "1234"
    assert any(
        finding.path == "closure_digest" and finding.kind == "hash-not-hex64"
        for finding in validate_record(document)
    )


def test_an_artifact_sha_prefix_is_normalized_not_flagged() -> None:
    document = _strict_record().to_json()
    document["image_digest"] = f"sha256:{HEX64}"
    document["evidence"][0]["artifact_sha256"] = f"sha256:{HEX64}"
    assert validate_record(document) == []


def test_non_semantic_version_fields_are_findings() -> None:
    document = _strict_record().to_json()
    document["release_version"] = "0.36"
    findings = validate_record(document)
    assert findings == [
        ValidationFinding(
            path="release_version",
            kind="version-not-semantic",
            message=findings[0].message,
        )
    ]
    # a digest pinned into harness_version (the historical canary shape) is
    # its OWN typed finding — R37-17's sha-in-version-field conflation kind:
    document = _strict_record().to_json()
    document["harness_version"] = f"sha256:{HEX64}"
    (finding,) = validate_record(document)
    assert finding.kind == "sha-in-version-field"
    assert finding.path == "harness_version"


def test_a_supported_verdict_requires_every_fingerprint_pinned() -> None:
    pinned = dict(
        runtime_dependency_fingerprint="closure-abc123",
        template_defaults_digest=HEX64,
        authority_contract_version="checkpoint-authority/1",
        provider_behavior_fingerprint="GitLab CE 19.3.2 (revision 34042bf7d00)",
    )
    document = _strict_record(**pinned).to_json()  # live evidence -> supported
    assert validate_record(document) == []
    document["template_defaults_digest"] = ""
    document.pop("provider_behavior_fingerprint")
    findings = validate_record(document)
    assert {finding.path for finding in findings} == {
        "provider_behavior_fingerprint",
        "template_defaults_digest",
    }
    assert all(finding.kind == "fingerprint-empty-for-supported-verdict" for finding in findings)
    # a declared_only record carries no such requirement:
    document["evidence"][0]["class"] = "model-fixture"
    assert validate_record(document) == []


def test_every_finding_kind_is_reachable() -> None:
    document = _strict_record().to_json()
    document["evidence"][0]["executed_at"] = HEX64  # timestamp-not-iso
    document["wheel_sha256"] = "short"  # hash-not-hex64
    document["harness_version"] = "/usr/local/bin/claude"  # version-not-semantic
    for axis in (
        "runtime_dependency_fingerprint",
        "template_defaults_digest",
        "authority_contract_version",
        "provider_behavior_fingerprint",
    ):
        document[axis] = ""  # live evidence -> supported, axes unpinned
    kinds = {finding.kind for finding in validate_record(document)}
    assert kinds == {
        "timestamp-not-iso",
        "hash-not-hex64",
        "version-not-semantic",
        "fingerprint-empty-for-supported-verdict",
    }


def test_the_finding_kind_vocabulary_is_closed() -> None:
    with pytest.raises(ProfileRecordError, match="closed"):
        ValidationFinding(path="x", kind="made-up-rule", message="nope")


def test_the_loader_refuses_a_malformed_strict_record(tmp_path: Path) -> None:
    write_profile_record(tmp_path, _strict_record(wheel_sha256="oops"))
    with pytest.raises(ProfileRecordError, match="qualification.record_validation") as excinfo:
        load_profile_records(tmp_path)
    assert "wheel_sha256 [hash-not-hex64]" in str(excinfo.value)


def test_a_legacy_record_loads_as_history_with_its_findings_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The committed v1 store predates the strict schema: its records are
    legacy by construction, their findings REPORTED (never rewritten)."""
    import dataclasses

    record = _strict_record(wheel_sha256="oops")  # malformed AND strict-shaped
    legacy = dataclasses.replace(record, legacy=True)
    write_profile_record(tmp_path, legacy)
    with caplog.at_level("WARNING", logger="forge.profile_qualification"):
        loaded = load_profile_records(tmp_path)
    assert len(loaded) == 1 and loaded[0].legacy is True
    assert any("qualification.record_validation" in message for message in caplog.messages)
    assert any("wheel_sha256 [hash-not-hex64]" in message for message in caplog.messages)


def test_the_committed_legacy_gitlab_record_is_reported_malformed() -> None:
    """gitlab-ce-v1@0.36.0 carries a wheel sha in executed_at — the exact
    historical defect R37-06 records. Validation REPORTS it (the record is
    legacy, so it loads as history) instead of rewriting the record."""
    document = json.loads(
        (ROOT / "qualification" / "records" / "gitlab-ce-v1@0.36.0.json").read_text(
            encoding="utf-8"
        )
    )
    findings = validate_record(document)
    kinds = {finding.kind for finding in findings}
    assert "timestamp-not-iso" in kinds
    assert any(finding.path.startswith("evidence[") for finding in findings)
    assert document.get("legacy") is True  # explicitly marked, not rewritten


def test_the_committed_preflight_record_is_strict_and_validates_clean() -> None:
    """The R37-06 record opts INTO the strict schema: distinct executed_at
    timestamps, artifact hashes, populated observed fingerprints, and the
    typed refusal-resolution matrix — zero findings, or the load refuses."""
    records = {record.record_id: record for record in load_profile_records(ROOT)}
    record = records["gitlab-ce-v1@0.37.0-preflight"]
    assert record.legacy is False
    document = json.loads(
        (ROOT / "qualification" / "records" / "gitlab-ce-v1@0.37.0-preflight.json").read_text(
            encoding="utf-8"
        )
    )
    assert validate_record(document) == []
    for entry in record.evidence:
        assert entry.artifact_sha256  # the hash identity is a DISTINCT field
        assert entry.executed_at.endswith(("Z", "+00:00"))  # and the timestamp is one
    assert record.refusal_resolution  # the refusal matrix is typed record content
    for resolution in record.refusal_resolution:
        assert resolution.action.startswith("runbook") or resolution.action.startswith("docs/")
        assert resolution.owner.strip()
    # the preflight record is honest: still declared_only (no live evidence
    # claim) and its note says the live flow remains refused.
    assert derive_verdict(record) == "declared_only"
    assert "refused at preflight" in record.note.lower()


def test_refusal_resolutions_round_trip_and_refuse_garbage() -> None:
    resolution = RefusalResolution(
        refusal="deployed control plane reports 0.28.0",
        action="runbook §3 — recreate on the promoted digest",
        owner="lab operator",
        status="open",
    )
    record = _strict_record(refusal_resolution=(resolution,))
    loaded = ProfileQualificationRecord.from_json(record.to_json())
    assert loaded.refusal_resolution == (resolution,)
    with pytest.raises(ProfileRecordError, match="status"):
        RefusalResolution(refusal="r", action="a", owner="o", status="maybe")
    with pytest.raises(ProfileRecordError, match="non-empty owner"):
        RefusalResolution(refusal="r", action="a", owner=" ", status="open")


# ---------------------------------------------------------------------------
# R37-17 (#298) — evidence tiers derived from executed trace records
# ---------------------------------------------------------------------------


def _trace(
    provenance: str = "scripted",
    *,
    capability: str = CAP,
    trace_id: str = "trace-1",
    record_id: str = "probe@0.36.0",
    provider: str = "",
    model_route: str = "",
    invokes_real_protocol_code: bool = False,
) -> TraceRecord:
    return TraceRecord(
        trace_id=trace_id,
        capability=capability,
        provenance=provenance,
        executed_at=ISO,
        artifact_sha256=HEX64,
        record_id=record_id,
        provider=provider,
        model_route=model_route,
        invokes_real_protocol_code=invokes_real_protocol_code,
    )


def test_a_scripted_trace_stays_scripted_even_when_it_invokes_real_protocol_code() -> None:
    record = _strict_record(evidence=(_strict_entry("model-fixture"),))
    report = derive_evidence_tier(record, (_trace("scripted", invokes_real_protocol_code=True),))
    (tier,) = report.per_capability
    assert tier.tier == "scripted"  # real protocol code, still authored provenance
    assert tier.trace_ids == ("trace-1",)
    assert any("invokes_real_protocol_code=True" in reason for reason in tier.reasons)
    assert any("stays scripted" in reason for reason in tier.reasons)


def test_a_live_trace_upgrades_only_its_own_capability() -> None:
    record = _strict_record(
        capabilities=("cap-live", "cap-scripted"),
        evidence=(
            _strict_entry("live-provider", capability="cap-live"),
            _strict_entry("model-fixture", capability="cap-scripted"),
        ),
    )
    traces = (
        _trace("live", capability="cap-live", provider="gitlab", model_route="anthropic/claude"),
        _trace("scripted", capability="cap-scripted"),
    )
    report = derive_evidence_tier(record, traces)
    tiers = {tier.capability: tier.tier for tier in report.per_capability}
    assert tiers == {"cap-live": "live", "cap-scripted": "scripted"}


def test_a_live_trace_never_upgrades_another_providers_record() -> None:
    record = _strict_record()  # provider gitlab
    github_live = _trace("live", provider="github", model_route="anthropic/claude")
    report = derive_evidence_tier(record, (github_live,))
    (tier,) = report.per_capability
    assert tier.tier == "none"
    assert any("only its own provider" in reason for reason in tier.reasons)


def test_refusal_evidence_never_upgrades_anything() -> None:
    record = _strict_record()
    report = derive_evidence_tier(record, (_trace("refused"),))
    (tier,) = report.per_capability
    assert tier.tier == "none"
    assert any("REFUSAL" in reason and "never upgrades" in reason for reason in tier.reasons)


def test_a_fixture_only_record_with_a_real_provider_label_is_held() -> None:
    """The R37-17 negative arm: fixture-only evidence carrying the
    real-provider-e2e label is HELD — the label never upgrades the trace."""
    record = _strict_record(evidence=(_strict_entry("model-fixture"),))  # CAP is live-required
    report = derive_evidence_tier(record, (_trace("scripted"),))
    assert report.holds
    assert any(UNMET_CAPABILITIES_OBSERVABILITY in hold and "HELD" in hold for hold in report.holds)


def test_an_entry_claiming_live_provider_without_a_live_trace_is_a_hold() -> None:
    """The trace record's provenance is the source of truth: a live-provider
    LABEL with only scripted traces behind it never stands."""
    record = _strict_record()  # live-provider entry, tier-3 class claim
    report = derive_evidence_tier(record, (_trace("scripted"),))
    assert any("label never upgrades" in hold for hold in report.holds)
    (tier,) = report.per_capability
    assert tier.tier == "scripted"


def test_no_executed_trace_means_no_tier_fail_closed() -> None:
    record = _strict_record()
    report = derive_evidence_tier(record, ())
    (tier,) = report.per_capability
    assert tier.tier == "none"
    assert any("fail-closed" in reason for reason in tier.reasons)
    # a trace executed for ANOTHER record never leaks into this one:
    other = _trace("live", provider="gitlab", model_route="anthropic/claude", record_id="other@9")
    report = derive_evidence_tier(record, (other,))
    assert report.per_capability[0].tier == "none"


def test_trace_records_round_trip_and_refuse_bad_shapes() -> None:
    trace = _trace("live", provider="gitlab", model_route="anthropic/claude")
    loaded = TraceRecord.from_json(json.loads(json.dumps(trace.to_json())))
    assert loaded == trace
    assert trace.to_json()["stamp"] == TRACE_RECORD_STAMP
    with pytest.raises(ProfileRecordError, match="stamp"):
        TraceRecord.from_json({**trace.to_json(), "stamp": "other/1"})
    with pytest.raises(ProfileRecordError, match="provenance"):
        _trace("maybe-live")
    # a digest in a trace's timestamp field refuses (traces have NO legacy lane):
    with pytest.raises(ProfileRecordError, match="executed_at"):
        TraceRecord(trace_id="t", capability=CAP, provenance="scripted", executed_at=HEX64)
    # live provenance MEANS provider + model route:
    with pytest.raises(ProfileRecordError, match="live provenance"):
        _trace("live", provider="gitlab", model_route="")
    # a version string where the artifact identity belongs:
    with pytest.raises(ProfileRecordError, match="artifact_sha256"):
        TraceRecord(
            trace_id="t",
            capability=CAP,
            provenance="scripted",
            executed_at=ISO,
            artifact_sha256="0.36.0",
        )


def test_trace_files_load_from_the_store_and_bad_ones_refuse(tmp_path: Path) -> None:
    traces = tmp_path / "qualification" / "traces"
    traces.mkdir(parents=True)
    good = _trace("scripted")
    (traces / "good.json").write_text(json.dumps(good.to_json()), encoding="utf-8")
    (loaded,) = load_trace_records(tmp_path)
    assert loaded == good
    # a missing traces directory is no traces, never an error:
    assert load_trace_records(tmp_path / "elsewhere") == ()
    bad = _trace("refused")
    document = bad.to_json()
    document["executed_at"] = HEX64  # the legacy conflation, refused for traces
    (traces / "bad.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ProfileRecordError, match="executed_at"):
        load_trace_records(tmp_path)


def test_the_committed_traces_back_the_preflight_record_honestly() -> None:
    """The committed executed traces make the current gitlab-ce-v1 tier
    SCRIPTED (real protocol code, authored responses) — and hold its
    real-provider-e2e claim; the refusal trace never upgrades anything."""
    traces = load_trace_records(ROOT)
    assert len(traces) == 3
    assert {trace.provenance for trace in traces} == {"scripted", "scripted", "refused"}
    record = next(
        r for r in load_profile_records(ROOT) if r.record_id == "gitlab-ce-v1@0.37.0-preflight"
    )
    report = derive_evidence_tier(record, traces)
    (tier,) = report.per_capability
    assert tier.capability == "real-provider-e2e"
    assert tier.tier == "scripted"
    assert any("HELD" in hold for hold in report.holds)


# ---------------------------------------------------------------------------
# R37-17 (#298) — the supported-profiles manifest (the human gate)
# ---------------------------------------------------------------------------

_APPROVAL = ProfileApproval(
    profile="probe", approved_by="lab operator", approved_at=ISO, note="on live evidence"
)


def _store_root_with(record: ProfileQualificationRecord, tmp_path: Path) -> Path:
    """A store root under pytest's tmp_path holding one committed record."""
    root = tmp_path / "store"
    root.mkdir()
    write_profile_record(root, record)
    return root


def test_a_supported_verdict_requires_human_approval(tmp_path: Path) -> None:
    """The human gate: derived supported + no approval → pending-approval,
    NEVER supported; with a named approver → supported."""
    record = _strict_record()  # live evidence, every axis pinned -> supported
    live_trace = _trace("live", provider="gitlab", model_route="anthropic/claude")
    store_dir = _store_root_with(record, tmp_path)
    unapproved = build_supported_profiles(store_dir, trace_refs=(live_trace,), approvals={})
    (entry,) = unapproved.entries
    assert entry.derived_verdict == "supported"
    assert entry.status == "pending-approval"
    assert entry.human_approved_by == ""
    assert any("human approval absent" in limit for limit in entry.limitations)
    approved = build_supported_profiles(
        store_dir, trace_refs=(live_trace,), approvals={"probe": _APPROVAL}
    )
    (entry,) = approved.entries
    assert entry.status == "supported"
    assert entry.human_approved_by == "lab operator"


def test_a_supported_manifest_entry_cannot_be_built_without_an_approver() -> None:
    """The gate is structural: constructing a supported entry directly,
    without the approver, refuses."""
    with pytest.raises(ProfileRecordError, match="human gate"):
        SupportedProfileEntry(
            profile="probe",
            record_id="probe@0.36.0",
            release_version="0.36.0",
            provider="gitlab",
            derived_verdict="supported",
            status="supported",
            human_approved_by="",
            tiers=(),
            limitations=(),
            requalification=(),
            withdrawn=(),
        )


def test_a_held_profile_stays_pending_approval_even_when_approved(tmp_path: Path) -> None:
    """Holds cap support independently of the human gate: a scripted-only
    real-provider capability never becomes supported."""
    record = _strict_record()  # claims live evidence…
    store_dir = _store_root_with(record, tmp_path)
    manifest = build_supported_profiles(
        store_dir,
        trace_refs=(_trace("scripted"),),  # …but the executed trace is scripted
        approvals={"probe": _APPROVAL},
    )
    (entry,) = manifest.entries
    assert entry.status == "pending-approval"
    assert any("held" in limit.lower() for limit in entry.limitations)


def test_a_changed_wheel_identity_under_a_constant_version_withdraws_the_claim(
    tmp_path: Path,
) -> None:
    """The R37-17 negative test: a different wheel under the SAME version
    string is detected — the profile's claim withdraws."""
    record = _strict_record(wheel_sha256=HEX64)
    store_dir = _store_root_with(record, tmp_path)
    manifest = build_supported_profiles(
        store_dir,
        changes={"wheel_sha256": "f" * 64},  # same version, different wheel
        approvals={"probe": _APPROVAL},
        trace_refs=(_trace("live", provider="gitlab", model_route="anthropic/claude"),),
    )
    (entry,) = manifest.entries
    assert entry.status == "withdrawn"
    assert entry.release_version == "0.36.0"  # the version string never moved
    assert any(
        WITHDRAWN_OBSERVABILITY in reason and "wheel_sha256" in reason for reason in entry.withdrawn
    )
    assert any(WITHDRAWN_OBSERVABILITY in limit for limit in entry.limitations)


_PINNED_AXES = {
    "runtime_dependency_fingerprint": "closure-abc123",
    "template_defaults_digest": HEX64,
    "authority_contract_version": "checkpoint-authority/1",
    "provider_behavior_fingerprint": "GitLab CE 19.3.2 (revision 34042bf7d00)",
    "image_digest": f"sha256:{HEX64}",
    "wheel_sha256": HEX64,
}


@pytest.mark.parametrize("axis", sorted(_PINNED_AXES))
def test_every_manifest_axis_change_withdraws_the_claim(axis: str, tmp_path: Path) -> None:
    """Every withdrawal axis — fingerprints AND artifact identity — withdraws
    the claim when the current world names a different value; the SAME value
    never triggers."""
    record = _strict_record(**_PINNED_AXES)
    store_dir = _store_root_with(record, tmp_path)
    changed = {axis: "different-value"}
    # the trigger fires on the axis BEFORE any manifest is built:
    (trigger,) = manifest_trigger_triggers(record, changed)
    assert axis in trigger and "requalification required" in trigger
    manifest = build_supported_profiles(store_dir, changes=changed, approvals={})
    (entry,) = manifest.entries
    assert entry.status == "withdrawn"
    assert any(axis in reason for reason in entry.withdrawn)
    # an unchanged world never withdraws anything:
    same = build_supported_profiles(store_dir, changes=_PINNED_AXES, approvals={})
    assert same.entries[0].status != "withdrawn"
    assert same.entries[0].withdrawn == ()


def test_the_manifest_names_the_refusal_matrix_and_unpinned_axes_as_limitations(
    tmp_path: Path,
) -> None:
    record = _strict_record(
        evidence=(_strict_entry("model-fixture"),),  # declared_only: unpinned axes are fine
        refusal_resolution=(
            RefusalResolution(
                refusal="deployed control plane reports 0.28.0",
                action="runbook §3 — recreate on the promoted digest",
                owner="lab operator",
                status="open",
            ),
        ),
        authority_contract_version="",  # unpinned axis
    )
    store_dir = _store_root_with(record, tmp_path)
    manifest = build_supported_profiles(store_dir, approvals={})
    (entry,) = manifest.entries
    assert any(
        "refusal-resolution [open]" in limit and "0.28.0" in limit for limit in entry.limitations
    )
    assert any("unpinned authority_contract_version" in limit for limit in entry.limitations)
    # the requalification block renders every withdrawal axis with its value:
    assert len(entry.requalification) == len(MANIFEST_TRIGGER_AXES)
    assert any("wheel_sha256 = UNPINNED" in line for line in entry.requalification)


def test_the_manifest_statuses_vocabulary_is_closed() -> None:
    assert MANIFEST_STATUSES == (
        "unqualified",
        "withdrawn",
        "declared_only",
        "lab-qualified",
        "pending-approval",
        "supported",
    )


def test_the_committed_store_manifest_is_honest() -> None:
    """Over the REAL store: nothing is supported (no live evidence, no
    approvals document), the gitlab hold is a named limitation, and the
    refusal matrix rides along."""
    manifest = build_supported_profiles(ROOT)
    assert manifest.to_json()["stamp"] == MANIFEST_STAMP
    entries = {entry.profile: entry for entry in manifest.entries}
    assert set(entries) == {"gitlab-ce-v1", "release-artifact-canary"}
    assert all(entry.status in MANIFEST_STATUSES for entry in manifest.entries)
    assert all(entry.status != "supported" for entry in manifest.entries)
    gitlab = entries["gitlab-ce-v1"]
    # R37-08 (#289) landed the LIVE record: the derived verdict over the
    # claimed capability (real-provider-e2e, live-provider pass) is
    # supported — but the #298 human gate + trace-tier hold keep the
    # manifest entry pending-approval, never supported, and the live trace
    # join stays owed (no committed live TraceRecord yet).
    # The latest gitlab record tracks the tree version (records are
    # committed per release; a literal broke at the v0.37.0 bump).
    from forge import __version__

    assert gitlab.record_id == f"gitlab-ce-v1@{__version__}"
    assert gitlab.status == "pending-approval"
    assert any(UNMET_CAPABILITIES_OBSERVABILITY in limit for limit in gitlab.limitations)
    assert any("refusal-resolution" in limit for limit in gitlab.limitations)
    # the per-release record claims every capability the qualifying trace
    # proved live (four since v0.39.0); with no committed live TraceRecord
    # the derived tier stays 'none' for each — the join stays owed.
    assert {tier.capability: tier.tier for tier in gitlab.tiers} == {
        "real-provider-e2e": "none",
        "useful-wip-cross-runner-resume": "none",
        "approved-revision-rebind": "none",
        "closing-review-within-reserve": "none",
    }


def test_the_manifest_cli_prints_the_stamped_json(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from forge import profile_qualification as pq

    write_profile_record(tmp_path, _strict_record())
    assert pq.main(["manifest", "--root", str(tmp_path)]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["stamp"] == MANIFEST_STAMP
    assert document["profiles"][0]["status"] == "pending-approval"


def test_approvals_load_and_refuse_garbage(tmp_path: Path) -> None:
    assert load_profile_approvals(tmp_path) == ()  # no document: no approvals
    document = {
        "stamp": "forge.profile.approvals/1",
        "approvals": [_APPROVAL.to_json()],
    }
    (tmp_path / "qualification").mkdir()
    (tmp_path / "qualification" / "profile-approvals.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    (approval,) = load_profile_approvals(tmp_path)
    assert approval == _APPROVAL
    bad = {"stamp": "other/1", "approvals": []}
    (tmp_path / "qualification" / "profile-approvals.json").write_text(
        json.dumps(bad), encoding="utf-8"
    )
    with pytest.raises(ProfileRecordError, match="stamp"):
        load_profile_approvals(tmp_path)
    with pytest.raises(ProfileRecordError, match="approved_at"):
        ProfileApproval(profile="p", approved_by="o", approved_at=HEX64)


# ---------------------------------------------------------------------------
# R37-17 (#298) — upgrade-claim honesty wired into validation
# ---------------------------------------------------------------------------


def _upgrade_document(**overrides: object) -> dict:
    facts = UpgradeFacts(
        source_schema="027",
        target_schema="027",
        seeded_records=("flow_runs: 1",),
        preservation_checks=("row counts",),
        evidence_ref="docs/releases/evidence/v0.35.0/promotion.json",
    )
    values: dict = dict(facts.to_json())
    values.update(overrides)
    return values


def test_a_same_head_run_labelled_a_schema_upgrade_is_a_typed_finding() -> None:
    document = _strict_record().to_json()
    document["upgrade"] = _upgrade_document(claim="schema-upgrade")  # 027→027!
    (finding,) = validate_record(document)
    assert finding.kind == "upgrade-claim-mislabelled"
    assert finding.path == "upgrade.claim"
    assert "never" in finding.message and "same-head-preservation" in finding.message


def test_a_seeded_transition_labelled_preservation_is_a_finding() -> None:
    document = _strict_record().to_json()
    document["upgrade"] = _upgrade_document(
        source_schema="026", target_schema="027", claim="same-head-preservation"
    )
    (finding,) = validate_record(document)
    assert finding.kind == "upgrade-claim-mislabelled"
    assert "transition" in finding.message


def test_a_transition_that_seeded_nothing_asserting_any_claim_is_a_finding() -> None:
    """The v0.33.0 canary shape (024→026, nothing seeded) asserting a
    schema upgrade mislabels EMPTY facts — it makes no claim at all."""
    document = _strict_record().to_json()
    document["upgrade"] = _upgrade_document(
        source_schema="024", target_schema="026", seeded_records=(), claim="schema-transition"
    )
    (finding,) = validate_record(document)
    assert finding.kind == "upgrade-claim-mislabelled"
    assert "NO claim" in finding.message


def test_an_unknown_upgrade_claim_label_is_a_finding() -> None:
    document = _strict_record().to_json()
    document["upgrade"] = _upgrade_document(claim="totally-a-migration")
    (finding,) = validate_record(document)
    assert finding.kind == "upgrade-claim-mislabelled"
    assert "vocabulary" in finding.message


def test_honest_upgrade_claim_labels_validate_clean() -> None:
    for claim, source, target in (
        ("same-head-preservation", "027", "027"),
        ("schema-transition", "026", "027"),
        ("schema-upgrade", "026", "027"),
    ):
        document = _strict_record().to_json()
        document["upgrade"] = _upgrade_document(
            source_schema=source, target_schema=target, claim=claim
        )
        assert validate_record(document) == [], claim
    # and the assertion round-trips with the facts:
    facts = UpgradeFacts(
        "027", "027", ("flow_runs: 1",), ("counts",), "ref", "same-head-preservation"
    )
    assert UpgradeFacts.from_json(facts.to_json()) == facts


# ---------------------------------------------------------------------------
# R37-17 (#298) — field-shape conflation validators
# ---------------------------------------------------------------------------


def test_a_sha_in_a_version_field_is_its_own_finding() -> None:
    for field in ("harness_version", "provider_version", "authority_contract_version"):
        document = _strict_record().to_json()
        document[field] = f"sha256:{HEX64}"
        (finding,) = validate_record(document)
        assert finding.kind == "sha-in-version-field", field
        assert finding.path == field
        assert TESTED_SHA_OBSERVABILITY in finding.message


def test_a_version_in_a_hash_field_is_its_own_finding() -> None:
    document = _strict_record().to_json()
    document["wheel_sha256"] = "0.36.0"  # the version string where the wheel hash belongs
    document["evidence"][0]["artifact_sha256"] = "v0.36.0"
    findings = validate_record(document)
    assert {(finding.path, finding.kind) for finding in findings} == {
        ("wheel_sha256", "version-in-hash-field"),
        ("evidence[0].artifact_sha256", "version-in-hash-field"),
    }
    assert all("deployed artifact" in finding.message for finding in findings)


# ---------------------------------------------------------------------------
# R37-17 (#298) — the typed, queryable, append-only store
# ---------------------------------------------------------------------------


def test_the_store_refuses_overwrites_with_a_typed_error_naming_the_file(
    tmp_path: Path,
) -> None:
    record = _strict_record()
    write_profile_record(tmp_path, record)
    changed = _strict_record(evidence=(_strict_entry("model-fixture"),))
    with pytest.raises(ProfileRecordImmutableError, match="probe@0.36.0.json") as excinfo:
        write_profile_record(tmp_path, changed)
    assert "immutable" in str(excinfo.value)
    assert isinstance(excinfo.value, ProfileRecordError)
    # the typed store view refuses the same way:
    store = ProfileRecordStore(tmp_path)
    with pytest.raises(ProfileRecordImmutableError, match="probe@0.36.0.json"):
        store.write(changed)


def test_historical_records_remain_queryable_after_new_ones(tmp_path: Path) -> None:
    store = ProfileRecordStore(tmp_path)
    old = _strict_record(record_id="probe@0.35.0", release_version="0.35.0")
    new = _strict_record()
    store.write(old)
    store.write(new)
    assert [record.record_id for record in store.history("probe")] == [
        "probe@0.35.0",
        "probe@0.36.0",
    ]
    assert store.latest("probe").record_id == "probe@0.36.0"
    assert store.record("probe@0.35.0") == old  # history by id, still loadable
    assert store.latest("other") is None
    assert store.record("missing@9") is None


# ---------------------------------------------------------------------------
# R37-17 (#298) — the capabilities render's trace-derived tier column
# ---------------------------------------------------------------------------


def test_the_tiers_column_is_trace_derived() -> None:
    record = _strict_record()
    live = _trace("live", provider="gitlab", model_route="anthropic/claude")
    with_tier = render_capabilities((record,), (live,))
    row = with_tier.splitlines()[2]
    assert "real-provider-e2e: live" in row
    without = render_capabilities((record,))
    assert "real-provider-e2e: none" in without.splitlines()[2]


# ---------------------------------------------------------------------------
# R38-06 (#307) — the frozen supported-profile manifest's binding
# ---------------------------------------------------------------------------

SUPPORTED_MANIFEST = ROOT / "qualification" / "profiles" / "supported-gitlab-ce-v1.json"


def _cited_gitlab_record_id() -> str:
    receipt = str(_supported_document()["harness"]["receipt"])  # type: ignore[index]
    return receipt.rsplit("/", 1)[-1].removesuffix(".json")


def _supported_document() -> dict[str, object]:
    import json as _json

    return _json.loads(SUPPORTED_MANIFEST.read_text(encoding="utf-8"))


def test_supported_profile_stamp_is_exported() -> None:
    from forge.profile_qualification import SUPPORTED_PROFILE_STAMP

    assert SUPPORTED_PROFILE_STAMP == "forge.supported.profile/1"


def test_load_supported_profile_reads_the_committed_manifest() -> None:
    document = load_supported_profile(ROOT)
    assert document is not None
    assert document["schema"] == SUPPORTED_PROFILE_STAMP  # type: ignore[index]
    assert document["profile"] == "supported-gitlab-ce-v1"  # type: ignore[index]


def test_load_supported_profile_none_when_absent(tmp_path: Path) -> None:
    assert load_supported_profile(tmp_path) is None


def test_load_supported_profile_refuses_a_foreign_stamp(tmp_path: Path) -> None:
    profiles = tmp_path / "qualification" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "supported-fake.json").write_text('{"schema": "forge.other/1"}', encoding="utf-8")
    with pytest.raises(ProfileRecordError, match="foreign document"):
        load_supported_profile(tmp_path)


def test_supported_profile_binding_matches_the_cited_record() -> None:
    bindings = supported_profile_binding(_supported_document(), load_profile_records(ROOT))
    by_axis = {binding.axis: binding for binding in bindings}
    assert set(by_axis) == {"wheel_sha256", "image_digest", "harness_version", "release_version"}
    assert all(binding.status == "match" for binding in by_axis.values())
    assert "the record the manifest's receipt names" in by_axis["wheel_sha256"].note


def test_supported_profile_binding_names_a_swapped_wheel_under_a_constant_version() -> None:
    records = list(load_profile_records(ROOT))
    cited_id = _cited_gitlab_record_id()
    cited = next(record for record in records if record.record_id == cited_id)
    swapped = ProfileQualificationRecord(**{**cited.__dict__, "wheel_sha256": "9" * 64})
    bindings = supported_profile_binding(_supported_document(), [swapped])
    wheel = next(binding for binding in bindings if binding.axis == "wheel_sha256")
    assert wheel.status == "divergent"
    assert "never a silent merge" not in wheel.note  # the note names, the status speaks
    # an unbound axis (an image-only record) is honest absence:
    image_only = ProfileQualificationRecord(**{**cited.__dict__, "wheel_sha256": ""})
    unbound = supported_profile_binding(_supported_document(), [image_only])
    assert {binding.status for binding in unbound} <= {"match", "unbound"}
    assert next(b for b in unbound if b.axis == "wheel_sha256").status == "unbound"


def test_supported_profile_binding_falls_back_to_the_latest_record_with_a_note() -> None:
    records = [
        record
        for record in load_profile_records(ROOT)
        if record.record_id != _cited_gitlab_record_id()
    ]
    if not any(record.profile == "gitlab-ce-v1" for record in records):
        pytest.skip("no alternative gitlab record to fall back to")
    bindings = supported_profile_binding(_supported_document(), records)
    assert any("the profile's latest record" in binding.note for binding in bindings)


def test_supported_profile_binding_grants_no_verdict() -> None:
    """The binding is a cross-reference: a matched axis never upgrades a
    record's derived verdict (that comes only from its evidence)."""
    records = load_profile_records(ROOT)
    cited = next(record for record in records if record.record_id == "gitlab-ce-v1@0.37.0")
    bindings = supported_profile_binding(_supported_document(), records)
    assert all(binding.status in ("match", "divergent", "unbound") for binding in bindings)
    for field_name in ("verdict", "derived_verdict", "status_verdict"):
        assert not hasattr(bindings[0], field_name)
    # and the record's own verdict is unchanged by the binding existing:
    assert derive_verdict(cited) in ("unqualified", "declared_only", "lab-qualified", "supported")
