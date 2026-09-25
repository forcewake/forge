"""R38-11 / #312 — the customer-scale discovery PLANNING PROFILE, checked.

The machinery lives in ``forge.adaptive.discovery_profile`` (the
observation cache, the exhaustion taxonomies, the budget-suited
synthesis validation, the carry-forward); the scaled scenario lives in
``evaluation/discovery_live/manifest-customer-v1.json`` +
``fixtures-customer-v1/`` and its driver/grader in
``scripts/run_discovery_live.py`` (``--profile``).  What is held here:

- the OBSERVATION CACHE — repeated reads of one immutable source under
  one policy scope reuse the verified bytes without re-paying the
  reader (windows included), a different policy scope is a MISS with
  its own fresh read (no cross-scope leak, asserted), and the cache
  rides the discovery record so a restart resumes on the recorded
  observations instead of re-reading them;
- the EXHAUSTION TAXONOMIES — each of the five classes with its OWN
  bounded recovery (exhaustion surfaces and asks with the retained
  findings; a truncated window earns exactly one continuation read; an
  invalid synthesis earns exactly one re-synthesis; a policy conflict
  becomes an explicit question with BOTH citations); the precedence is
  deterministic and NO class ever licenses a complete-understanding
  claim;
- the BUDGET-SUITED SYNTHESIS — the reasoning-heavy route reserves the
  larger share, unknown modes refuse, and schema validation NEVER
  silently changes plan meaning (a ``True`` line number is an ERROR,
  not a coerced 1; the failed document rides back unmodified);
- the CARRY-FORWARD — the plan's evidence section rides the approved
  brief bytes the lane already verifies (the A03 envelope seam), so
  implementation receives the findings instead of re-paying discovery;
- the SCALED MANIFEST — nine frozen repositories re-deriving their
  OIDs, all three decisive depth kinds, the conflict pair, the LARGE
  budget sink, and every refusal the contract makes;
- the SCRIPTED QUALIFICATION — the deterministic run over the scaled
  graph passes every grader arm, and each arm FAILS when its evidence
  is removed (a grader that cannot fail is not a grader).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from forge.adaptive.discovery_profile import (
    CLASSIFICATION_VALUES,
    CLASS_COMPLETED,
    CLASS_CONFUSED_POLICY,
    CLASS_EXHAUSTED_BUDGET,
    CLASS_INVALID_SYNTHESIS,
    CLASS_TRUNCATED_OUTPUT,
    CARRY_FORWARD_BEGIN,
    CARRY_FORWARD_END,
    ConflictSide,
    ImplementationCarryForward,
    InvestigationOutcome,
    ObservationCache,
    PolicyConflict,
    RECOVERY_CONTINUATION_READ,
    RECOVERY_EXPLICIT_QUESTION,
    RECOVERY_NONE,
    RECOVERY_RE_SYNTHESIS,
    RECOVERY_SURFACE_AND_ASK,
    SYNTHESIS_BUDGET_PROFILES,
    SynthesisBudgetProfile,
    Recovery,
    attach_observation_cache,
    brief_envelope_seam,
    budget_profile_for,
    cache_from_record,
    carry_forward,
    classify_investigation,
    conflict_question,
    planning_scope,
    render_carry_forward_section,
    review_scope,
    validate_plan_synthesis,
    verify_carry_forward,
)
from scripts.run_discovery_live import (
    CUST_AUDIT_KEY,
    CUST_BILLING_KEY,
    CUST_DOCS_KEY,
    CUST_OWN_KEY,
    CUST_SHIPPING_KEY,
    CUSTOMER_MANIFEST_FILENAME,
    ROLE_BUDGET_SINK,
    ROLE_DECISIVE,
    ROLE_NOISE,
    ROLE_WRITABLE,
    _load_fixture_files,
    _oid,
    build_customer_manifest_document,
    capture_customer_profile,
    customer_files,
    grade_customer_profile,
    load_customer_manifest,
    validate_customer_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "evaluation" / "discovery_live"
CUSTOMER_MANIFEST = EVAL_DIR / CUSTOMER_MANIFEST_FILENAME
CUSTOMER_RUN = EVAL_DIR / "customer-profile-run" / "profile-run.json"

SPEC_DIGEST = "a" * 64


def _manifest() -> dict:
    return load_customer_manifest(CUSTOMER_MANIFEST)


def _files(manifest: dict) -> dict[str, dict[str, str]]:
    return customer_files(manifest, EVAL_DIR)


def _passing_plan() -> dict:
    return {
        "steps": [{"step_id": "s1", "objective": "gate by age", "repo": CUST_OWN_KEY}],
        "claims": [
            {
                "claim_id": "c1",
                "text": "the neighbor owns the threshold",
                "repo": CUST_BILLING_KEY,
                "path": "src/policy/refunds.py",
                "line_start": 210,
                "line_end": 230,
                "asserted_content": "REFUND_MANUAL_APPROVAL_THRESHOLD_CENTS = 5000",
                "source_oid": "f" * 40,
            }
        ],
        "questions": ["Who performs the approval step for orders-api?"],
        "assumptions": ["the checkout gate stays age-only"],
        "write_targets": [CUST_OWN_KEY],
    }


def _conflict() -> PolicyConflict:
    return PolicyConflict(
        repository=CUST_SHIPPING_KEY,
        source_oid="e" * 40,
        path="src/shipping/confirmation.py",
        current=ConflictSide(
            revision="S-2026-03",
            line=39,
            content='SHIP_CONFIRMATION_RECIPIENT_CURRENT = "customer-and-fulfillment"',
        ),
        obsolete=ConflictSide(
            revision="S-2024-08",
            line=78,
            content='SHIP_CONFIRMATION_RECIPIENT_OBSOLETE = "warehouse-only"',
        ),
    )


# ---------------------------------------------------------------------------
# The observation cache
# ---------------------------------------------------------------------------


class TestObservationCache:
    def _reader(self, content: str) -> tuple[Callable[[str], str], dict]:
        state = {"reads": 0}

        def read(path: str) -> str:
            state["reads"] += 1
            return content

        return read, state

    def test_repeated_reads_of_one_immutable_source_reuse_without_repaying(self):
        read, state = self._reader("line\n" * 50)
        cache = ObservationCache()
        first = cache.observe(
            read, repository="r", source_oid="oid", path="p", policy_scope="planning:x"
        )
        second = cache.observe(
            read, repository="r", source_oid="oid", path="p", policy_scope="planning:x"
        )
        assert first is second
        assert (cache.hits, cache.misses, cache.underlying_reads) == (1, 1, 1)
        assert state["reads"] == 1  # the reader ran ONCE

    def test_a_second_window_of_the_same_source_is_also_reuse(self):
        read, state = self._reader("x" * 9000)
        cache = ObservationCache()
        cache.observe(read, repository="r", source_oid="oid", path="p", policy_scope="s")
        window = cache.observe(
            read,
            repository="r",
            source_oid="oid",
            path="p",
            policy_scope="s",
            offset=2000,
            length=2000,
        )
        assert window.window(2000, 2000) == "x" * 2000
        assert cache.hits == 1 and state["reads"] == 1

    def test_a_moved_oid_is_a_new_immutable_source(self):
        read, state = self._reader("content")
        cache = ObservationCache()
        cache.observe(read, repository="r", source_oid="oid-1", path="p", policy_scope="s")
        cache.observe(read, repository="r", source_oid="oid-2", path="p", policy_scope="s")
        assert (cache.hits, cache.underlying_reads) == (0, 2)

    def test_cross_scope_isolation_a_different_scope_never_reuses(self):
        read, state = self._reader("content")
        cache = ObservationCache()
        cache.observe(read, repository="r", source_oid="oid", path="p", policy_scope="planning:x")
        review = cache.observe(
            read, repository="r", source_oid="oid", path="p", policy_scope="review:x"
        )
        # the review scope MISSED: its own fresh verified read, its own entry
        assert (cache.hits, cache.misses) == (0, 2)
        assert state["reads"] == 2
        assert cache.get("r", "oid", "p", "planning:x") is not review
        assert cache.scopes_of("r", "oid", "p") == ("planning:x", "review:x")
        cache.assert_no_cross_scope_leak()

    def test_the_isolation_assertion_catches_an_injected_leak(self):
        read, _ = self._reader("content")
        cache = ObservationCache()
        planning = cache.observe(
            read, repository="r", source_oid="oid", path="p", policy_scope="planning:x"
        )
        cache.observe(read, repository="r", source_oid="oid", path="p", policy_scope="review:x")
        # inject exactly the failure mode the assertion exists for: the
        # review key served the PLANNING-verified entry
        cache._entries[("r", "oid", "p", "review:x")] = planning
        cache.observe(read, repository="r", source_oid="oid", path="p", policy_scope="review:x")
        with pytest.raises(AssertionError, match="cross-scope leak"):
            cache.assert_no_cross_scope_leak()

    def test_the_cache_rides_the_record_and_resumes_after_restart(self):
        read, state = self._reader("content")
        cache = ObservationCache()
        cache.observe(read, repository="r", source_oid="oid", path="p", policy_scope="planning:x")
        record = attach_observation_cache({"discovery_id": "d1"}, cache)
        assert isinstance(record["observation_cache"], dict)

        restored = cache_from_record(record)
        assert restored is not None
        reads_before = state["reads"]
        resumed = restored.observe(
            read, repository="r", source_oid="oid", path="p", policy_scope="planning:x"
        )
        assert resumed.policy_scope == "planning:x"
        assert restored.hits == 1 and restored.underlying_reads == 0
        assert state["reads"] == reads_before  # the restart re-paid NOTHING

    def test_a_record_without_a_cache_section_restores_none(self):
        assert cache_from_record({"discovery_id": "d1"}) is None
        assert cache_from_record({}) is None

    def test_a_foreign_schema_refuses_to_restore(self):
        with pytest.raises(ValueError, match="schema"):
            ObservationCache.from_document({"schema": "something.else/1", "entries": []})

    def test_the_document_round_trip_preserves_the_observations(self):
        read, _ = self._reader("content")
        cache = ObservationCache()
        cache.observe(read, repository="r", source_oid="oid", path="p", policy_scope="s")
        restored = ObservationCache.from_document(cache.as_document())
        assert restored.as_document()["entries"] == cache.as_document()["entries"]

    def test_an_incomplete_key_refuses(self):
        cache = ObservationCache()
        with pytest.raises(ValueError, match="non-empty"):
            cache.observe(lambda p: "", repository="", source_oid="o", path="p", policy_scope="s")

    def test_scope_helpers_name_the_consumer_and_the_authorization(self):
        digest = "b" * 64
        assert planning_scope(digest) == f"planning:{digest[:12]}"
        assert review_scope(digest) == f"review:{digest[:12]}"
        assert planning_scope(digest) != review_scope(digest)


# ---------------------------------------------------------------------------
# The exhaustion taxonomies
# ---------------------------------------------------------------------------


class TestExhaustionTaxonomy:
    def test_a_declared_done_run_with_nothing_pending_is_completed(self):
        verdict = classify_investigation(InvestigationOutcome(declared_done=True))
        assert verdict.classification == CLASS_COMPLETED
        assert verdict.recovery.kind == RECOVERY_NONE

    def test_exhausted_budget_surfaces_and_asks_with_the_retained_findings(self):
        verdict = classify_investigation(
            InvestigationOutcome(declared_done=False, stopped_reason="max_calls")
        )
        assert verdict.classification == CLASS_EXHAUSTED_BUDGET
        assert verdict.recovery.kind == RECOVERY_SURFACE_AND_ASK
        assert verdict.recovery.max_extra_reads == 0  # asking, not reading
        assert verdict.recovery.question
        assert verdict.retained_findings_visible
        assert not verdict.complete_understanding_claimed
        assert "partial" in verdict.honest_summary

    @pytest.mark.parametrize("stopped_reason", ["max_calls", "wall_time", "spend_cap"])
    def test_every_budget_stop_is_exhausted_budget(self, stopped_reason: str):
        verdict = classify_investigation(
            InvestigationOutcome(declared_done=False, stopped_reason=stopped_reason)
        )
        assert verdict.classification == CLASS_EXHAUSTED_BUDGET

    def test_a_run_that_neither_stopped_nor_finished_is_exhausted_not_complete(self):
        verdict = classify_investigation(InvestigationOutcome(declared_done=False))
        assert verdict.classification == CLASS_EXHAUSTED_BUDGET
        assert not verdict.complete_understanding_claimed

    def test_truncated_output_earns_exactly_one_continuation_read(self):
        verdict = classify_investigation(
            InvestigationOutcome(declared_done=False, truncated_observations=2)
        )
        assert verdict.classification == CLASS_TRUNCATED_OUTPUT
        assert verdict.recovery.kind == RECOVERY_CONTINUATION_READ
        assert verdict.recovery.max_extra_reads == 1
        assert verdict.recovery.continuation is not None

    def test_invalid_synthesis_earns_exactly_one_bounded_re_synthesis(self):
        bad = validate_plan_synthesis({"steps": "nope"})
        assert bad.ok is False
        verdict = classify_investigation(InvestigationOutcome(declared_done=True, synthesis=bad))
        assert verdict.classification == CLASS_INVALID_SYNTHESIS
        assert verdict.recovery.kind == RECOVERY_RE_SYNTHESIS
        assert verdict.recovery.max_extra_syntheses == 1
        assert verdict.recovery.max_extra_reads == 0

    def test_confused_policy_becomes_an_explicit_question_with_both_citations(self):
        conflict = _conflict()
        verdict = classify_investigation(
            InvestigationOutcome(declared_done=True, conflicts=(conflict,))
        )
        assert verdict.classification == CLASS_CONFUSED_POLICY
        assert verdict.recovery.kind == RECOVERY_EXPLICIT_QUESTION
        assert verdict.recovery.question == conflict_question(conflict)
        citations = {json.dumps(dict(c), sort_keys=True) for c in verdict.recovery.citations}
        assert len(citations) == 2  # BOTH sides, never one

    def test_the_conflict_question_names_both_lines_and_both_values(self):
        question = conflict_question(_conflict())
        assert "line 39" in question and "line 78" in question
        assert "customer-and-fulfillment" in question
        assert "warehouse-only" in question
        assert "src/shipping/confirmation.py" in question

    def test_precedence_a_conflict_dominates_an_exhausted_run(self):
        verdict = classify_investigation(
            InvestigationOutcome(
                declared_done=False, stopped_reason="max_calls", conflicts=(_conflict(),)
            )
        )
        assert verdict.classification == CLASS_CONFUSED_POLICY

    def test_precedence_an_invalid_synthesis_dominates_exhaustion(self):
        bad = validate_plan_synthesis({"steps": []})
        verdict = classify_investigation(
            InvestigationOutcome(declared_done=False, stopped_reason="max_calls", synthesis=bad)
        )
        assert verdict.classification == CLASS_INVALID_SYNTHESIS

    def test_precedence_a_recorded_stop_dominates_a_pending_truncation(self):
        verdict = classify_investigation(
            InvestigationOutcome(
                declared_done=False, stopped_reason="max_calls", truncated_observations=3
            )
        )
        assert verdict.classification == CLASS_EXHAUSTED_BUDGET

    @pytest.mark.parametrize(
        "outcome",
        [
            InvestigationOutcome(declared_done=True),
            InvestigationOutcome(declared_done=False, stopped_reason="max_calls"),
            InvestigationOutcome(declared_done=False, truncated_observations=1),
            InvestigationOutcome(
                declared_done=True, synthesis=validate_plan_synthesis({"steps": "x"})
            ),
            InvestigationOutcome(declared_done=True, conflicts=(_conflict(),)),
        ],
    )
    def test_no_classification_ever_claims_complete_understanding(self, outcome):
        assert classify_investigation(outcome).complete_understanding_claimed is False

    def test_the_classification_vocabulary_is_closed(self):
        outcomes = {
            classify_investigation(outcome).classification
            for outcome in (
                InvestigationOutcome(declared_done=True),
                InvestigationOutcome(declared_done=False, stopped_reason="max_calls"),
                InvestigationOutcome(declared_done=False, truncated_observations=1),
                InvestigationOutcome(declared_done=True, synthesis=validate_plan_synthesis(None)),
                InvestigationOutcome(declared_done=True, conflicts=(_conflict(),)),
            )
        }
        assert outcomes == set(CLASSIFICATION_VALUES)

    def test_recovery_construction_is_typed(self):
        with pytest.raises(ValueError, match="unknown recovery kind"):
            Recovery(kind="read_everything", detail="unbounded")
        with pytest.raises(ValueError, match="must carry its question"):
            Recovery(kind=RECOVERY_EXPLICIT_QUESTION, detail="no question")
        with pytest.raises(ValueError, match="continuation window"):
            Recovery(kind=RECOVERY_CONTINUATION_READ, detail="no window")


# ---------------------------------------------------------------------------
# Budget-suited synthesis
# ---------------------------------------------------------------------------


class TestSynthesisBudget:
    def test_the_reasoning_heavy_route_reserves_more_than_standard(self):
        heavy, standard = budget_profile_for("reasoning-heavy"), budget_profile_for("standard")
        assert heavy.reasoning_reserve_tokens > standard.reasoning_reserve_tokens
        assert heavy.plan_content_tokens > 0 and standard.plan_content_tokens > 0

    def test_the_profiles_table_agrees_with_the_lookup(self):
        for mode, profile in SYNTHESIS_BUDGET_PROFILES.items():
            assert budget_profile_for(mode) == profile

    def test_an_unknown_mode_fails_closed(self):
        with pytest.raises(ValueError, match="no synthesis budget profile"):
            budget_profile_for("maybe-reasoning")

    def test_a_reserve_that_leaves_no_content_refuses(self):
        with pytest.raises(ValueError, match="room for plan content"):
            SynthesisBudgetProfile(
                mode="standard",
                plan_max_tokens=100,
                reasoning_reserve_tokens=100,
                research_max_tokens_per_call=1000,
            )


# ---------------------------------------------------------------------------
# Synthesis validation — a verdict, never a repair
# ---------------------------------------------------------------------------


class TestSynthesisValidation:
    def test_a_well_formed_plan_passes(self):
        assert validate_plan_synthesis(_passing_plan()).ok

    def test_a_non_object_fails(self):
        verdict = validate_plan_synthesis(None)
        assert verdict.ok is False and verdict.raw is None
        assert verdict.classification == CLASS_INVALID_SYNTHESIS

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda plan: plan.pop("questions"),
            lambda plan: plan.__setitem__("steps", "not-a-list"),
            lambda plan: plan["claims"][0].pop("repo"),
            lambda plan: plan["claims"][0].pop("path"),
            lambda plan: plan["claims"][0].__setitem__("line_start", 0),
            lambda plan: plan["claims"][0].__setitem__("line_end", "220"),
            lambda plan: plan["questions"].__setitem__(0, 42),
            lambda plan: plan["write_targets"].clear(),
        ],
    )
    def test_malformed_plans_fail_with_errors(self, mutation):
        plan = _passing_plan()
        mutation(plan)
        verdict = validate_plan_synthesis(plan)
        assert verdict.ok is False
        assert verdict.errors

    def test_a_boolean_line_number_is_an_error_not_a_silent_one(self):
        plan = _passing_plan()
        plan["claims"][0]["line_start"] = True
        verdict = validate_plan_synthesis(plan)
        assert verdict.ok is False  # never coerced to 1
        assert any("line_start" in error for error in verdict.errors)

    def test_validation_never_changes_the_plan(self):
        plan = _passing_plan()
        snapshot = json.dumps(plan, sort_keys=True)
        verdict = validate_plan_synthesis(plan)
        assert verdict.ok
        assert json.dumps(plan, sort_keys=True) == snapshot

    def test_a_failed_validation_returns_the_document_unmodified(self):
        plan = _passing_plan()
        plan["claims"][0]["line_start"] = True
        verdict = validate_plan_synthesis(plan)
        assert verdict.raw is plan  # the very object, unrepaired
        assert verdict.raw["claims"][0]["line_start"] is True

    def test_a_failure_routes_to_the_bounded_re_synthesis_recovery(self):
        verdict = validate_plan_synthesis({"steps": []})
        assert verdict.recovery.kind == RECOVERY_RE_SYNTHESIS
        assert verdict.recovery.max_extra_syntheses == 1


# ---------------------------------------------------------------------------
# The carry-forward — paid once, consumed through the approved brief bytes
# ---------------------------------------------------------------------------


class TestCarryForward:
    def _carry(self) -> ImplementationCarryForward:
        return carry_forward(_passing_plan(), discovery_id="disc-1", snapshot_digest="c" * 64)

    def test_facts_assumptions_and_questions_all_carry(self):
        carry = self._carry()
        assert [entry.kind for entry in carry.entries] == ["fact", "assumption", "question"]
        fact = carry.facts[0]
        assert (fact.repository, fact.path, fact.line_start, fact.line_end) == (
            CUST_BILLING_KEY,
            "src/policy/refunds.py",
            210,
            230,
        )
        assert fact.source_oid == "f" * 40

    def test_the_section_rides_the_plan_text_and_verifies(self):
        carry = self._carry()
        plan_text = "THE PLAN\n" + render_carry_forward_section(carry)
        assert CARRY_FORWARD_BEGIN in plan_text and CARRY_FORWARD_END in plan_text
        assert verify_carry_forward(carry, plan_text)

    def test_a_plan_text_without_the_section_does_not_verify(self):
        assert verify_carry_forward(self._carry(), "THE PLAN (no section)") is False

    def test_a_section_missing_an_entry_does_not_verify(self):
        carry = self._carry()
        document = carry.as_document()
        document["entries"] = document["entries"][:-1]  # the question fell out
        body = json.dumps(document, sort_keys=True, separators=(",", ":"))
        plan_text = f"{CARRY_FORWARD_BEGIN}\n{body}\n{CARRY_FORWARD_END}"
        assert verify_carry_forward(carry, plan_text) is False

    def test_the_brief_envelope_seam_holds(self):
        carry = self._carry()
        plan_text = "THE PLAN\n" + render_carry_forward_section(carry)
        seam = brief_envelope_seam(
            carry,
            plan_text,
            run_id="run-1",
            task_title="Refund flow",
            task_description="Wire refund requests through.",
            spec_digest=SPEC_DIGEST,
        )
        assert seam["verified"], seam
        assert seam["envelope_digest"]

    def test_the_seam_fails_when_the_plan_bytes_lost_the_section(self):
        carry = self._carry()
        seam = brief_envelope_seam(
            carry,
            "THE PLAN WITHOUT THE SECTION",
            run_id="run-1",
            task_title="Refund flow",
            task_description="Wire refund requests through.",
            spec_digest=SPEC_DIGEST,
        )
        assert seam["verified"] is False

    def test_bounded_rendering_is_explicit_about_dropped_entries(self):
        plan = _passing_plan()
        plan["assumptions"] = [f"assumption {index}" for index in range(60)]
        carry = carry_forward(plan, discovery_id="d", snapshot_digest="s" * 64)
        section = render_carry_forward_section(carry, max_chars=1200)
        document = json.loads(
            section.split(CARRY_FORWARD_BEGIN, 1)[1].rsplit(CARRY_FORWARD_END, 1)[0]
        )
        assert document.get("truncated") is True
        assert document.get("dropped_entries", 0) >= 1


# ---------------------------------------------------------------------------
# The scaled manifest
# ---------------------------------------------------------------------------


class TestCustomerManifest:
    def test_the_checked_in_manifest_loads_and_validates(self):
        assert _manifest()["schema"] == "forge.discovery.customer.manifest/1"

    def test_the_graph_is_nine_repositories_with_the_expected_roles(self):
        manifest = _manifest()
        roles = [entry["role"] for entry in manifest["repos"]]
        assert len(roles) == 9
        assert roles.count(ROLE_WRITABLE) == 1
        assert roles.count(ROLE_DECISIVE) == 3
        assert roles.count(ROLE_BUDGET_SINK) == 1
        assert roles.count(ROLE_NOISE) == 4

    def test_the_fixtures_rederive_the_frozen_oids(self):
        # equality with a fresh derivation proves every recorded OID matches
        assert build_customer_manifest_document(EVAL_DIR) == _manifest()

    def test_validation_refuses_a_graph_below_scale(self):
        manifest = _manifest()
        document = {**manifest, "repos": manifest["repos"][:4]}
        with pytest.raises(ValueError, match="eight repositories"):
            validate_customer_manifest(document, EVAL_DIR)

    def test_validation_refuses_a_missing_depth_kind(self):
        manifest = _manifest()
        document = copy.deepcopy(manifest)
        audit = next(e for e in document["repos"] if e["key"] == CUST_AUDIT_KEY)
        audit["decisive"]["depth"] = "line-200+"  # a duplicate kind
        with pytest.raises(ValueError, match="three depth kinds"):
            validate_customer_manifest(document, EVAL_DIR)

    def test_validation_refuses_two_writables(self):
        manifest = _manifest()
        document = copy.deepcopy(manifest)
        noise = next(e for e in document["repos"] if e["role"] == ROLE_NOISE)
        noise["role"] = ROLE_WRITABLE
        with pytest.raises(ValueError, match="exactly one"):
            validate_customer_manifest(document, EVAL_DIR)

    def test_validation_refuses_a_small_budget_sink(self):
        manifest = _manifest()
        document = copy.deepcopy(manifest)
        sink = next(e for e in document["repos"] if e["role"] == ROLE_BUDGET_SINK)
        sink["fixture"] = "fixtures-customer-v1/marketing-site"  # 2 files
        sink["source_oid"] = _oid(_load_fixture_files(EVAL_DIR, sink["fixture"]))
        with pytest.raises(ValueError, match="not LARGE"):
            validate_customer_manifest(document, EVAL_DIR)

    def test_validation_refuses_a_leaked_decisive_marker(self):
        manifest = _manifest()
        document = copy.deepcopy(manifest)
        marker = next(e for e in document["repos"] if e["key"] == CUST_BILLING_KEY)["decisive"][
            "marker"
        ]
        document["task"]["statement"] = f"do it with {marker} please"
        with pytest.raises(ValueError, match="leak"):
            validate_customer_manifest(document, EVAL_DIR)

    def test_validation_refuses_a_marker_duplicated_in_another_repo(self, tmp_path):
        manifest = _manifest()
        document = copy.deepcopy(manifest)
        marker = next(e for e in document["repos"] if e["key"] == CUST_BILLING_KEY)["decisive"][
            "marker"
        ]
        noise = next(e for e in document["repos"] if e["role"] == ROLE_NOISE)
        # mirror the real fixture tree with symlinks, then replace the noise
        # repository with one that QUOTES the billing marker
        for entry in document["repos"]:
            if entry is noise:
                continue
            source = (EVAL_DIR / str(entry["fixture"])).resolve()
            target = tmp_path / str(entry["fixture"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
        fixtures = tmp_path / str(noise["fixture"])
        fixtures.mkdir(parents=True)
        duplicated = f"we also quote {marker}\n"
        (fixtures / "README.md").write_text(duplicated, encoding="utf-8")
        noise["source_oid"] = _oid({"README.md": duplicated})
        with pytest.raises(ValueError, match="ONLY in their own repositories"):
            validate_customer_manifest(document, tmp_path)

    def test_validation_refuses_a_second_page_marker_inside_the_first_page(self):
        manifest = _manifest()
        document = copy.deepcopy(manifest)
        audit = next(e for e in document["repos"] if e["key"] == CUST_AUDIT_KEY)
        audit["decisive"]["page_chars"] = 99999
        with pytest.raises(ValueError, match="first"):
            validate_customer_manifest(document, EVAL_DIR)

    def test_validation_refuses_bad_caps(self):
        manifest = _manifest()
        for mutate in (
            lambda caps: caps.__setitem__("sink_max_calls", 99),
            lambda caps: caps.__setitem__("budget_mode", "maybe"),
            lambda caps: caps.__setitem__("max_usd", 5.0),
        ):
            document = copy.deepcopy(manifest)
            mutate(document["caps"])
            with pytest.raises(ValueError):
                validate_customer_manifest(document, EVAL_DIR)

    def test_the_budget_sink_is_genuinely_large(self):
        manifest = _manifest()
        sink = next(e for e in manifest["repos"] if e["role"] == ROLE_BUDGET_SINK)
        files = _files(manifest)[sink["key"]]
        assert len(files) >= 40 and sum(len(text) for text in files.values()) >= 100_000


# ---------------------------------------------------------------------------
# The scripted qualification — the deterministic run and its grader arms
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def captured_run() -> dict:
    import asyncio

    manifest = _manifest()
    return asyncio.run(capture_customer_profile(manifest, EVAL_DIR))


class TestScriptedQualification:
    def test_the_run_is_offline_scripted_with_the_nine_repo_graph(self, captured_run):
        assert captured_run["capture"]["provenance"] == "offline-scripted-model"
        assert captured_run["manifest"]["repos"] == 9
        assert captured_run["research_document"]["complete"] is True

    def test_every_grader_arm_passes(self, captured_run):
        grade = grade_customer_profile(captured_run, _manifest(), _files(_manifest()))
        assert grade.passed, grade.failed_checks

    def test_the_terminal_verdict_is_the_confused_policy_question(self, captured_run):
        verdict = captured_run["final_verdict"]
        assert verdict["classification"] == CLASS_CONFUSED_POLICY
        assert verdict["recovery"]["kind"] == RECOVERY_EXPLICIT_QUESTION
        assert CUST_SHIPPING_KEY in verdict["recovery"]["question"]
        assert not verdict["complete_understanding_claimed"]

    def test_the_truncation_recovery_was_spent_on_a_continuation(self, captured_run):
        ledger = captured_run["recovery_ledger"]
        assert ledger, "the truncated first-page read must be recorded"
        entry = ledger[0]
        assert entry["verdict"]["classification"] == CLASS_TRUNCATED_OUTPUT
        assert entry["verdict"]["recovery"]["kind"] == RECOVERY_CONTINUATION_READ
        assert entry["spent"] is True

    def test_the_budget_sink_exhausted_with_retained_findings(self, captured_run):
        sink = captured_run["budget_sink"]
        assert sink["stopped_reason"] == "max_calls"
        assert sink["repos_consulted"] == [CUST_DOCS_KEY]
        assert sink["verdict"]["classification"] == CLASS_EXHAUSTED_BUDGET
        assert sink["verdict"]["recovery"]["kind"] == RECOVERY_SURFACE_AND_ASK
        assert sink["retained_findings"] >= 1
        assert sink["verdict"]["retained_findings_visible"] is True
        assert sink["verdict"]["complete_understanding_claimed"] is False

    def test_the_cache_served_repeats_and_isolated_scopes(self, captured_run):
        cache = captured_run["observation_cache"]
        assert cache["stats"]["hits"] >= 1
        assert cache["repeat_hit"] is True
        assert cache["cross_scope_isolated"] is True
        assert len(cache["scope_probe"]["scopes_of_source"]) == 2
        assert cache["restart_resumed"] is True and cache["restart_hits"] >= 1

    def test_the_carry_forward_is_populated_and_seam_verified(self, captured_run):
        carry = captured_run["carry_forward"]
        assert carry["counts"]["facts"] >= 2
        assert carry["counts"]["questions"] >= 1
        assert carry["verified_in_plan"] is True
        assert captured_run["carry_forward_seam"]["verified"] is True

    def test_the_checked_in_run_if_present_regrades_passing(self):
        if not CUSTOMER_RUN.exists():
            pytest.skip("the qualification run has not been checked in yet")
        checked_in = json.loads(CUSTOMER_RUN.read_text(encoding="utf-8"))
        grade = grade_customer_profile(checked_in, _manifest(), _files(_manifest()))
        assert grade.passed, grade.failed_checks

    def test_a_fresh_capture_reproduces_the_checked_in_run(self, captured_run):
        if not CUSTOMER_RUN.exists():
            pytest.skip("the qualification run has not been checked in yet")
        checked_in = json.loads(CUSTOMER_RUN.read_text(encoding="utf-8"))
        assert captured_run == checked_in


class TestGraderArms:
    """Each arm must FAIL when its evidence is removed."""

    @pytest.fixture()
    def run_and_grade(self, captured_run):
        manifest = _manifest()

        def _grade(run: dict) -> dict:
            return grade_customer_profile(run, manifest, _files(manifest)).as_document()

        return copy.deepcopy(captured_run), _grade

    def test_the_run_passes_before_any_mutation(self, run_and_grade):
        run, grade = run_and_grade
        assert grade(run)["passed"] is True

    def test_dropping_the_billing_claim_fails_the_line_depth_arm(self, run_and_grade):
        run, grade = run_and_grade
        run["plan"]["claims"] = [
            claim for claim in run["plan"]["claims"] if claim["repo"] != CUST_BILLING_KEY
        ]
        assert grade(run)["decisive_line_depth_found"] is False

    def test_a_first_page_sighting_of_the_audit_marker_fails_the_depth_arm(self, run_and_grade):
        run, grade = run_and_grade
        manifest = _manifest()
        audit = next(e for e in manifest["repos"] if e["key"] == CUST_AUDIT_KEY)["decisive"]
        file_text = _files(manifest)[CUST_AUDIT_KEY][audit["path"]]
        run["observations"].append(
            {
                "tool": "read_file",
                "repo_key": CUST_AUDIT_KEY,
                "call": f"{audit['path']} offset 0",
                "content": file_text,  # the whole file 'seen' on page one
                "error": "",
                "truncated": False,
            }
        )
        assert grade(run)["decisive_second_page_found"] is False

    def test_removing_the_continuation_read_fails_the_continuation_arm(self, run_and_grade):
        run, grade = run_and_grade
        manifest = _manifest()
        audit = next(e for e in manifest["repos"] if e["key"] == CUST_AUDIT_KEY)["decisive"]
        page = audit["page_chars"]
        run["observations"] = [
            observation
            for observation in run["observations"]
            if not (
                observation["repo_key"] == CUST_AUDIT_KEY
                and f"offset {page}" in observation["call"]
            )
        ]
        result = grade(run)
        assert result["second_page_continuation_used"] is False
        assert result["decisive_second_page_found"] is False

    def test_asserting_a_conflict_side_fails_the_silent_pick_arm(self, run_and_grade):
        run, grade = run_and_grade
        run["plan"]["claims"].append(
            {
                "claim_id": "cX",
                "text": "the recipient is decided",
                "repo": CUST_SHIPPING_KEY,
                "path": "src/shipping/confirmation.py",
                "line_start": 1,
                "line_end": 5,
                "asserted_content": 'SHIP_CONFIRMATION_RECIPIENT_CURRENT = "customer-and-fulfillment"',
            }
        )
        assert grade(run)["conflict_silent_pick"] is True

    def test_dropping_the_conflict_question_fails_the_question_arm(self, run_and_grade):
        run, grade = run_and_grade
        run["plan"]["questions"] = []
        result = grade(run)
        assert result["conflict_became_question"] is False

    def test_a_completed_sink_fails_the_exhaustion_signal(self, run_and_grade):
        run, grade = run_and_grade
        run["budget_sink"]["verdict"]["classification"] = CLASS_COMPLETED
        assert grade(run)["budget_sink_signalled"] is False

    def test_a_silent_stop_fails_the_exhaustion_signal(self, run_and_grade):
        run, grade = run_and_grade
        run["budget_sink"]["stopped_reason"] = ""
        assert grade(run)["budget_sink_signalled"] is False

    def test_zero_cache_hits_fail_the_reuse_arm(self, run_and_grade):
        run, grade = run_and_grade
        run["observation_cache"]["stats"]["hits"] = 0
        run["observation_cache"]["repeat_hit"] = False
        assert grade(run)["cache_reuse_on_repeat"] is False

    def test_a_collapsed_scope_probe_fails_the_isolation_arm(self, run_and_grade):
        run, grade = run_and_grade
        run["observation_cache"]["scope_probe"]["scopes_of_source"] = ["planning:x"]
        assert grade(run)["no_cross_scope_leak"] is False

    def test_no_restart_hits_fail_the_resumption_arm(self, run_and_grade):
        run, grade = run_and_grade
        run["observation_cache"]["restart_hits"] = 0
        assert grade(run)["restart_resumption"] is False

    def test_the_standard_budget_fails_the_reserve_arm(self, run_and_grade):
        run, grade = run_and_grade
        standard = SYNTHESIS_BUDGET_PROFILES["standard"].as_document()
        run["budget_profile"] = standard
        assert grade(run)["budget_profile_reserved"] is False

    def test_an_invalid_synthesis_fails_the_validation_arm(self, run_and_grade):
        run, grade = run_and_grade
        run["synthesis"]["ok"] = False
        assert grade(run)["synthesis_validated"] is False

    def test_a_repaired_probe_fails_the_routing_arm(self, run_and_grade):
        run, grade = run_and_grade
        run["synthesis_probe"]["invalid_ok"] = True
        assert grade(run)["synthesis_invalid_routes_to_recovery"] is False

    def test_an_empty_carry_forward_fails_the_carry_arm(self, run_and_grade):
        run, grade = run_and_grade
        run["carry_forward"]["counts"] = {"facts": 0, "assumptions": 0, "questions": 0}
        assert grade(run)["carry_forward_populated"] is False

    def test_a_broken_seam_fails_the_seam_arm(self, run_and_grade):
        run, grade = run_and_grade
        run["carry_forward_seam"]["verified"] = False
        assert grade(run)["carry_forward_seam_verified"] is False

    def test_a_widened_write_scope_fails_the_target_arm(self, run_and_grade):
        run, grade = run_and_grade
        run["plan"]["write_targets"] = [CUST_OWN_KEY, CUST_BILLING_KEY]
        assert grade(run)["write_scope_single_target"] is False

    def test_a_missing_plan_fails_honestly(self, run_and_grade):
        run, grade = run_and_grade
        run["plan"] = None
        result = grade(run)
        assert result["plan_present"] is False and result["passed"] is False
