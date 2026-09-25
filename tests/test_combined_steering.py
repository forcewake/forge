"""R38-12 (#313) — the combined steering machinery (offline).

``scripts/run_combined_steering.py`` drives the LIVE composition; the pure
seams it relies on are pinned here without a lab, a runner or a paid model
call:

- the COMBINED GRADER (:func:`forge.adaptive.steering_causality.
  grade_combined_trace`) — the five arms over a fully captured composed
  chain (ack → durable applied → observed vendor application → later
  checkpoint; the counterfactual; the live target; the revision's
  three-way digest switch exactly at the approval; the preserved WIP and
  the required-resume redispatch), and EVERY arm's honest failure mode;
- the LIVE-TARGET grammar — the checkable transformation the steer names
  (``entrypoint <new> (not <old>) in <path>``), its word-boundary checks
  and its refusal of unparseable instructions;
- the TRACE-RECORD schema — the causal identity chain the record must
  carry (command → delivery → vendor application → edit → checkpoint →
  decision → envelope), and the spend accounting inside the cap;
- the WAITER reuse — the #306 anchor discipline (the driver-phase stamp
  parse, the anchor epoch) and the durable-row moment extraction (the
  rungs, the lane's evidence appends, the honest delivery-mode label);
- the TASK fixture — approach X's frozen shape, the independent oracle
  committed before any run, the shipped template VERBATIM, and the steer
  text that parses to the graded target and classifies as guidance (never
  an amendment, never an acceptance change).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from forge.adaptive import steering_causality as sc
from forge.adaptive.control import classify_instruction
from scripts.run_combined_steering import (
    SPEND_CAP_USD,
    STEER_TEXT,
    STEER2_TEXT,
    VALIDATOR_PATH,
    X_NAME,
    Y_NAME,
    _seed_validator_stub,
    _TRACE_LINE_RE,
    application_moments,
    build_trace,
    ci_yaml,
    driver_phase_started_at,
    read_works_index,
    seed_files,
    smoke_oracle_script,
    spend_from_receipts,
    steer_delivery_mode,
    trace_anchor_epoch,
)

# ---------------------------------------------------------------------------
# the captured evidence one green composed run produces (the fixture the
# grader tests consume — every timestamp is an instant one clock recorded)
# ---------------------------------------------------------------------------

T0 = "2026-09-25T10:00:00.000000+00:00"
T1 = "2026-09-25T10:00:02.000000+00:00"
T2 = "2026-09-25T10:00:05.000000+00:00"
T3 = "2026-09-25T10:00:12.000000+00:00"
T4 = "2026-09-25T10:01:30.000000+00:00"
T5 = "2026-09-25T10:01:45.000000+00:00"
T6 = "2026-09-25T10:02:20.000000+00:00"
D1 = "1" * 64
D2 = "2" * 64
CKPT = "a" * 64
ENVELOPE = "b" * 12

FINAL_Y = (
    '"""Email validation."""\n'
    "\n"
    "\n"
    f"def {Y_NAME}(email: str) -> bool:\n"
    "    return True\n"
    "\n"
    "\n"
    "def validate_many(emails):\n"
    "    return [validate_email(e) for e in emails]\n"
)
FINAL_X = (
    '"""Email validation."""\n'
    "\n"
    "\n"
    f"def {X_NAME}(email: str) -> bool:\n"
    "    return True\n"
    "\n"
    "\n"
    "def validate_many(emails):\n"
    "    return [check(e) for e in emails]\n"
)


def _green_trace() -> sc.CombinedTrace:
    return sc.CombinedTrace(
        command=sc.command_evidence_of_row(
            {
                "command_id": "cmd-steer-1",
                "kind": "steer",
                "payload": {"text": STEER_TEXT},
                "status": "checkpointed",
                "journal": [
                    {"to": "received", "at": T0},
                    {"to": "authorized", "at": T1},
                    {"to": "dispatching", "at": T2},
                ],
            }
        ),
        application=sc.SteerApplicationEvidence(
            command_id="cmd-steer-1",
            applied_at=T2,
            application_observed_at=T3,
            delivery_mode="mid-turn",
        ),
        edits=sc.edit_set_of({VALIDATOR_PATH: _seed_validator_stub()}, {VALIDATOR_PATH: FINAL_Y}),
        counterfactual_edits=sc.edit_set_of(
            {VALIDATOR_PATH: _seed_validator_stub()}, {VALIDATOR_PATH: FINAL_X}
        ),
        checkpoints=(sc.CheckpointEvidence(checkpoint_id=CKPT, files=2, uploaded_at=T4),),
        revision=sc.RevisionEvidence(
            decision_id="dec-1",
            staged_at=T5,
            approved_at=T5,
            recomputed_digest=D2,
            staged_digest=D2,
            active_plan_digest_before=D1,
            active_plan_digest_after=D2,
            run_plan_digest_before=D1,
            run_plan_digest_after=D2,
            reuse_route="preserve",
            preserved_checkpoint_id=CKPT,
        ),
        resume_dispatch=sc.ResumeDispatchEvidence(
            envelope_digest=ENVELOPE,
            dispatched_checkpoint_id=CKPT,
            decision_id="dec-cont-1",
            resume_mode="required",
        ),
        provenance="live-lane:test",
    )


# ----------------------------------------------------------------------
# the combined grader — the composed chain
# ----------------------------------------------------------------------


class TestCombinedGrader:
    def test_every_arm_holds_over_a_captured_green_chain(self):
        grade = sc.grade_combined_trace(_green_trace())
        assert grade.causal, grade.as_document()
        assert [arm.name for arm in grade.arms] == [
            "ack_precedes_edit",
            "counterfactual_differs",
            "target_matched",
            "revision_identity_switched",
            "wip_preserved_and_redispatched",
        ]
        assert grade.provenance == "live-lane:test"

    def test_arm1_fails_without_application_evidence(self):
        trace = _green_trace()
        object.__setattr__(trace, "application", None)
        grade = sc.grade_combined_trace(trace)
        assert not grade.arm("ack_precedes_edit").ok
        assert "unproven" in grade.arm("ack_precedes_edit").reason

    def test_arm1_fails_when_the_ack_follows_the_application(self):
        trace = _green_trace()
        assert trace.application is not None
        object.__setattr__(
            trace,
            "application",
            sc.SteerApplicationEvidence(
                command_id="cmd-steer-1",
                applied_at="2026-09-25T09:59:00+00:00",  # BEFORE the ack
                application_observed_at=T3,
                delivery_mode="mid-turn",
            ),
        )
        grade = sc.grade_combined_trace(trace)
        assert not grade.arm("ack_precedes_edit").ok
        assert "out of order" in grade.arm("ack_precedes_edit").reason

    def test_arm1_fails_when_no_later_checkpoint_follows_the_application(self):
        trace = _green_trace()
        object.__setattr__(
            trace,
            "checkpoints",
            (
                sc.CheckpointEvidence(
                    checkpoint_id=CKPT, files=2, uploaded_at="2026-09-25T09:00:00+00:00"
                ),
            ),
        )
        grade = sc.grade_combined_trace(trace)
        assert not grade.arm("ack_precedes_edit").ok
        assert "no subsequent-work observation" in grade.arm("ack_precedes_edit").reason

    def test_arm2_fails_when_the_arms_are_identical(self):
        trace = _green_trace()
        object.__setattr__(trace, "counterfactual_edits", trace.edits)
        grade = sc.grade_combined_trace(trace)
        assert not grade.arm("counterfactual_differs").ok

    def test_arm3_fails_when_the_steered_edits_kept_approach_x(self):
        trace = _green_trace()
        object.__setattr__(
            trace,
            "edits",
            sc.edit_set_of({VALIDATOR_PATH: _seed_validator_stub()}, {VALIDATOR_PATH: FINAL_X}),
        )
        grade = sc.grade_combined_trace(trace)
        assert not grade.arm("target_matched").ok
        assert Y_NAME in grade.arm("target_matched").reason

    def test_arm3_fails_when_the_unsteered_arm_swapped_on_its_own(self):
        trace = _green_trace()
        object.__setattr__(trace, "counterfactual_edits", trace.edits)
        grade = sc.grade_combined_trace(trace)
        assert not grade.arm("target_matched").ok
        assert "task's default" in grade.arm("target_matched").reason

    def test_arm4_fails_when_any_digest_leg_disagrees(self):
        trace = _green_trace()
        revision = sc.RevisionEvidence(
            **{**_green_trace().revision.__dict__, "run_plan_digest_after": D1}  # type: ignore[arg-type]
        )
        object.__setattr__(trace, "revision", revision)
        grade = sc.grade_combined_trace(trace)
        assert not grade.arm("revision_identity_switched").ok
        assert "disagree" in grade.arm("revision_identity_switched").reason

    def test_arm4_fails_when_the_row_switched_before_the_approval(self):
        trace = _green_trace()
        revision = sc.RevisionEvidence(
            **{**_green_trace().revision.__dict__, "run_plan_digest_before": D2}  # type: ignore[arg-type]
        )
        object.__setattr__(trace, "revision", revision)
        grade = sc.grade_combined_trace(trace)
        assert not grade.arm("revision_identity_switched").ok
        assert "exactly at the approval" in grade.arm("revision_identity_switched").reason

    def test_arm5_fails_when_the_dispatch_restored_a_different_checkpoint(self):
        trace = _green_trace()
        object.__setattr__(
            trace,
            "resume_dispatch",
            sc.ResumeDispatchEvidence(
                envelope_digest=ENVELOPE,
                dispatched_checkpoint_id="f" * 64,
                decision_id="dec-cont-1",
                resume_mode="required",
            ),
        )
        grade = sc.grade_combined_trace(trace)
        assert not grade.arm("wip_preserved_and_redispatched").ok

    def test_arm5_fails_when_the_resume_was_not_required(self):
        trace = _green_trace()
        dispatch = sc.ResumeDispatchEvidence(
            envelope_digest=ENVELOPE,
            dispatched_checkpoint_id=CKPT,
            decision_id="dec-cont-1",
            resume_mode="fresh",
        )
        object.__setattr__(trace, "resume_dispatch", dispatch)
        grade = sc.grade_combined_trace(trace)
        assert not grade.arm("wip_preserved_and_redispatched").ok


# ----------------------------------------------------------------------
# the live-target grammar
# ----------------------------------------------------------------------


class TestLiveTargetGrammar:
    def test_the_drivers_steer_text_parses_to_the_tasks_target(self):
        target = sc.parse_live_target(STEER_TEXT)
        assert target == sc.LiveTarget(path=VALIDATOR_PATH, old=X_NAME, new=Y_NAME)

    def test_sentence_punctuation_never_enters_the_path(self):
        target = sc.parse_live_target(
            "entrypoint validate_email (not check) in src/validators/email.py. Done."
        )
        assert target is not None
        assert target.path == "src/validators/email.py"

    def test_unparseable_instructions_never_pass(self):
        assert sc.parse_live_target("make it better") is None
        assert sc.parse_live_target("entrypoint same (not same) in x.py") is None

    def test_applied_requires_the_swap_not_just_the_new_symbol(self):
        target = sc.parse_live_target(STEER_TEXT)
        assert target is not None
        base = {VALIDATOR_PATH: _seed_validator_stub()}
        assert sc.live_target_applied(target, sc.edit_set_of(base, {VALIDATOR_PATH: FINAL_Y}))
        # the new symbol beside the old definition is NOT the swap
        both = {VALIDATOR_PATH: _seed_validator_stub() + f"\n\ndef {Y_NAME}(e):\n    return True\n"}
        assert not sc.live_target_applied(target, sc.edit_set_of(base, both))
        # a missing file is not a swap
        assert not sc.live_target_applied(target, sc.edit_set_of({}, {}))

    def test_the_stub_carries_approach_xs_shape(self):
        assert f"def {X_NAME}(" in _seed_validator_stub()
        assert f"def {Y_NAME}(" not in _seed_validator_stub()


# ----------------------------------------------------------------------
# the trace-record schema + the spend accounting
# ----------------------------------------------------------------------


def _green_bundle() -> dict[str, Any]:
    """A captured bundle shaped exactly as the live phases record it."""
    return {
        "phases": {
            "steer": {
                "issue": {"iid": 7},
                "plan": {"run_id": "9" * 32},
                "steer_note": {"note_id": 501, "posted_at": T0, "body": STEER_TEXT},
                "steer_row": {
                    "id": "cmd-steer-1",
                    "command_id": "cmd-steer-1",
                    "status": "checkpointed",
                    "rungs": ["received", "authorized", "dispatching", "checkpointed"],
                    "moments": {
                        "received_at": T0,
                        "authorized_at": T1,
                        "applied_at": T2,
                        "application_observed_at": T3,
                    },
                    "journal": [
                        {"to": "received", "at": T0},
                        {"to": "authorized", "at": T1},
                        {"to": "dispatching", "at": T2},
                        {"to": "checkpointed", "at": T3},
                    ],
                    "dedup_key": "k1",
                },
                "interleaving_notes": {
                    "steer2": {"note_id": 502, "posted_at": T4},
                    "pause": {"note_id": 503, "posted_at": T4},
                },
                "interleaving_rows": {
                    "steer2": {
                        "command_id": "cmd-steer-2",
                        "status": "received",
                        "rungs": ["received"],
                    },
                    "pause": {
                        "command_id": "cmd-pause-1",
                        "status": "checkpointed",
                        "rungs": ["received", "authorized", "dispatching", "checkpointed"],
                        "journal": [
                            {"to": "received", "at": T4},
                            {"to": "authorized", "at": T4},
                            {"to": "dispatching", "at": T4},
                            {"to": "checkpointed", "at": T4},
                        ],
                    },
                },
                "pause_checkpoint": {
                    "checkpoint_id": CKPT,
                    "files": 2,
                    "uploaded_at": T4,
                },
                "blocked_classification": {"status": "blocked", "status_reason": "operator_pause"},
                "dispatches": [
                    {
                        "lane_job_id": 900,
                        "usage_receipt": {"total_cost_usd": 0.20},
                        "candidate_meta": {
                            "steering_journal": [
                                {
                                    "kind": "steer",
                                    "command_id": "cmd-steer-1",
                                    "outcome": "applied",
                                    "delivery": "application_observed",
                                    "detail": {"delivered": "mid-turn"},
                                }
                            ]
                        },
                    }
                ],
            },
            "revision": {
                "before": {"run_plan_digest": D1},
                "staged": {
                    "decision_id": "dec-1",
                    "staged_at": T5,
                    "active_digest_seeded": D1,
                    "revision1_digest": D1,
                    "revision2_digest": D2,
                    "staged_digest": D2,
                },
                "approve_note": {"note_id": 504, "posted_at": T5},
                "after": {
                    "run_plan_digest": D2,
                    "active_plan_digest": D2,
                    "reuse_route": "preserve",
                    "reuse_route_reason": "compatible",
                    "preserved_checkpoint_id": CKPT,
                },
            },
            "resume": {
                "retry_note": {"note_id": 505, "posted_at": T6},
                "dispatches": [
                    {
                        "lane_job_id": 901,
                        "job_status": "success",
                        "usage_receipt": {"total_cost_usd": 0.25},
                        "candidate_meta": {},
                        "restore_lines": [f"restored checkpoint {CKPT}"],
                        "envelope_lines": ["forge dispatch envelope: resume=required"],
                    }
                ],
                "resume_dispatch_envelope": {
                    "envelope_digest": ENVELOPE,
                    "checkpoint": CKPT,
                    "decision_id": "dec-cont-1",
                    "resume_mode": "required",
                },
                "mr": {
                    "mr_iid": 3,
                    "draft": True,
                    "merged": False,
                    "candidate_sha": "c" * 40,
                    "oracle_pipeline_id": 77,
                    "oracle_tampering": [],
                },
                "final_candidate": {"validator_content": FINAL_Y},
            },
            "counterfactual": {
                "unsteered_candidate": {"validator_content": FINAL_X},
                "mr": {"mr_iid": 4, "draft": True, "merged": False, "oracle_tampering": []},
                "dispatches": [{"lane_job_id": 902, "usage_receipt": {"total_cost_usd": 0.30}}],
            },
            "setup": {"seed_commit_sha": "d" * 40, "template_sha256": "e" * 64},
        },
        "failures": [],
    }


class TestTraceRecord:
    def test_a_captured_green_bundle_folds_into_a_causal_record(self):
        trace = build_trace(_green_bundle())
        assert trace["grade"]["causal"], json.dumps(trace["grade"], indent=2)
        ok, missing = sc.combined_record_valid(trace)
        assert ok, missing
        assert trace["spend"]["total_usd"] == pytest.approx(0.75)
        assert trace["spend"]["cost_basis"] == "sdk-total_cost_usd"
        assert trace["milestones"]["mr"]["draft"] is True
        assert trace["milestones"]["command"]["command_id"] == "cmd-steer-1"
        assert trace["milestones"]["application"]["delivery_mode"] == "mid-turn"

    def test_an_empty_bundle_honestly_fails_every_arm_and_the_schema(self):
        trace = build_trace({"phases": {}, "failures": []})
        assert not trace["grade"]["causal"]
        ok, missing = sc.combined_record_valid(trace)
        assert not ok
        # every causal identity is named, never a silent pass
        assert any("command_id" in hole for hole in missing)
        assert any("delivery_mode" in hole for hole in missing)
        assert any("envelope_digest" in hole for hole in missing)
        assert any("spend" in hole for hole in missing)

    def test_spend_over_the_cap_invalidates_the_record(self):
        trace = build_trace(_green_bundle())
        trace["spend"]["total_usd"] = SPEND_CAP_USD + 0.01
        ok, missing = sc.combined_record_valid(trace)
        assert not ok
        assert any("exceeds the cap" in hole for hole in missing)

    def test_a_missing_chain_field_is_named_with_its_meaning(self):
        trace = build_trace(_green_bundle())
        del trace["milestones"]["application"]["application_observed_at"]
        ok, missing = sc.combined_record_valid(trace)
        assert not ok
        assert any("vendor application" in hole for hole in missing)


class TestSpendAccounting:
    def test_the_sdk_cost_field_is_preferred_everywhere_it_exists(self):
        folded = spend_from_receipts([{"total_cost_usd": 0.25}, {"total_cost_usd": 0.5}])
        assert folded["total_usd"] == pytest.approx(0.75)
        assert folded["cost_basis"] == "sdk-total_cost_usd"

    def test_a_receipt_without_cost_falls_back_to_the_labeled_price_class(self):
        folded = spend_from_receipts([{"input_tokens": 1_000_000, "output_tokens": 500_000}])
        assert folded["cost_basis"] == "fallback-price-class(glm-5.3-flash-lab)"
        assert folded["total_usd"] == pytest.approx(0.60 + 0.5 * 2.20)

    def test_no_receipts_is_an_honest_zero_not_a_guess(self):
        folded = spend_from_receipts([])
        assert folded["total_usd"] == 0.0
        assert folded["cost_basis"] == "no-receipts-yet"


# ----------------------------------------------------------------------
# the waiter reuse — the #306 anchor discipline over the live surfaces
# ----------------------------------------------------------------------


class TestWaiterReuse:
    def test_the_driver_phase_stamp_parses_from_a_real_shaped_trace(self):
        trace = (
            "2026-09-25T10:00:00.515131Z 01O section_start:8897745304\n"
            "2026-09-25T10:00:03.111111Z 01O # before_script created .forge/; the phase owns its receipts\n"
            "2026-09-25T10:00:04.000000Z 01E some stderr line\n"
        )
        assert driver_phase_started_at(trace) == "2026-09-25T10:00:03.111111Z"
        assert driver_phase_started_at("no markers here") is None
        assert _TRACE_LINE_RE.match("2026-09-25T10:00:03.111111Z 01O body") is not None

    def test_the_anchor_epoch_is_a_utc_instant(self):
        from datetime import datetime, timezone

        stamp = "2026-09-25T10:00:03.111111Z"
        expected = (
            datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
        assert trace_anchor_epoch(stamp) == pytest.approx(expected)

    def test_a_newer_checkpoint_advances_the_predicate_exactly_once(self, monkeypatch, tmp_path):
        import scripts.run_combined_steering as driver

        works = tmp_path / "works"
        works.mkdir()
        (works / "work-1.json").write_text(
            json.dumps({"checkpoints": [{"checkpoint_id": CKPT, "files": 2}]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(driver, "checkpoint_store_root", lambda: tmp_path)
        first = driver._newer_checkpoint("work-1", None)
        assert first is not None and first["checkpoint_id"] == CKPT
        # the SAME checkpoint id advances nothing — the waiter never
        # double-samples one transaction
        assert driver._newer_checkpoint("work-1", CKPT) is None
        assert driver._newer_checkpoint("absent-work", None) is None

    def test_the_works_index_reads_are_typed_and_fail_closed(self, tmp_path):
        works = tmp_path / "works"
        works.mkdir()
        (works / "work-1.json").write_text(
            json.dumps({"checkpoints": [{"checkpoint_id": CKPT, "files": 2}]}),
            encoding="utf-8",
        )
        assert read_works_index(tmp_path, "work-1") == {
            "checkpoints": [{"checkpoint_id": CKPT, "files": 2}]
        }
        assert read_works_index(tmp_path, "absent") is None
        (works / "broken.json").write_text("{not json", encoding="utf-8")
        assert read_works_index(tmp_path, "broken") is None


class TestRowMomentExtraction:
    def test_the_moments_read_the_rungs_and_the_lane_evidence_appends(self):
        row = {
            "id": "cmd-steer-1",
            "created_at": "2026-09-25T09:59:59+00:00",
            "journal": [
                {"to": "received", "at": T0},
                {"to": "authorized", "at": T1},
                {"to": "dispatching", "at": T2},
                {"at": T3, "lane_ack": "applied", "row": {"state": "applied"}},
                {"to": "checkpointed", "at": T3},
            ],
        }
        moments = application_moments(row)
        assert moments == {
            "received_at": T0,
            "authorized_at": T1,
            "applied_at": T2,
            "application_observed_at": T3,
        }

    def test_a_row_without_evidence_appends_falls_back_to_the_checkpointed_rung(self):
        row = {"journal": [{"to": "received", "at": T0}, {"to": "checkpointed", "at": T4}]}
        assert application_moments(row)["application_observed_at"] == T4

    def test_the_delivery_mode_is_the_lanes_own_label_never_a_guess(self):
        row = {"id": "cmd-steer-1", "journal": []}
        meta = {
            "steering_journal": [
                {
                    "kind": "steer",
                    "command_id": "cmd-steer-1",
                    "outcome": "applied",
                    "delivery": "application_observed",
                    "detail": {"delivered": "mid-turn"},
                }
            ]
        }
        assert steer_delivery_mode(row, meta) == "mid-turn"
        queued = {
            "steering_journal": [
                {
                    "kind": "steer",
                    "command_id": "cmd-steer-1",
                    "outcome": "applied",
                    "detail": {"queued_for_resume": True},
                }
            ]
        }
        assert steer_delivery_mode(row, queued) == "queued-for-resume"
        assert steer_delivery_mode(row, None) == ""


# ----------------------------------------------------------------------
# the task fixture — the oracle, the template, the classification
# ----------------------------------------------------------------------


class TestTaskFixture:
    def test_the_seed_carries_the_stub_the_readme_the_ci_and_the_tests(self):
        files = seed_files("forge-steer-test")
        assert set(files) == {
            "README.md",
            ".gitlab-ci.yml",
            "tests/test_email_validator.py",
            "src/validators/__init__.py",
            VALIDATOR_PATH,
        }
        assert f"def {X_NAME}(" in files[VALIDATOR_PATH]

    def test_the_ci_is_the_shipped_template_verbatim_plus_the_oracle(self):
        from scripts.run_combined_steering import TEMPLATE_SOURCE

        yaml = ci_yaml()
        assert "forge-agent-claude-sdk:" in yaml
        assert TEMPLATE_SOURCE.read_text(encoding="utf-8") in yaml
        assert "--require-generation" in yaml
        assert "smoke:" in yaml

    def test_the_oracle_asserts_the_exact_cases_and_the_bulk_scope(self):
        script = smoke_oracle_script()
        assert "user@example.com" in script
        assert "no-at-sign" in script
        assert "validate_many" in script
        assert "assert" in script

    @pytest.mark.parametrize("text", [STEER_TEXT, STEER2_TEXT])
    def test_the_steer_texts_are_guidance_never_amendments(self, text):
        assert classify_instruction(text) == "steer"

    def test_an_acceptance_weakening_steer_would_still_be_refused(self):
        assert classify_instruction("skip the tests and ship it") == "acceptance_change"
