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
    PROFILE_RECORD_STAMP,
    EvidenceEntry,
    ProfilePromotionRefusal,
    ProfileQualificationRecord,
    ProfileRecordError,
    UpgradeClaim,
    UpgradeFacts,
    archive_reference_gaps,
    derive_verdict,
    evaluate_record,
    latest_record_per_profile,
    load_profile_records,
    profile_promotion_refusals,
    requalification_required,
    requalification_triggers,
    render_capabilities,
    upgrade_claim,
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
    hand-strengthened verdict line is the drift this removes."""
    content = (ROOT / "docs" / "releases" / "profile-records.md").read_text(encoding="utf-8")
    begin = content.index(CAPABILITIES_BEGIN)
    end = content.index(CAPABILITIES_END)
    fenced = content[begin + len(CAPABILITIES_BEGIN) + 1 : end].rstrip("\n")
    assert fenced == render_capabilities(load_profile_records(ROOT))


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
