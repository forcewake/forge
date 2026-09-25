"""Q39-08 (#327): the consumer-contract arms of the conformance gate.

``scripts/gate_conformance.py``'s check 5 proves the NEW contracts —
the #320 operation grant, the #321 revision rebind — through their
ACTUAL consumers. This module pins BOTH halves, exactly like
``tests/test_gate_conformance.py`` does for checks 1-4:

- the DECISION LOGIC (the comment-only spoof transform and lens, the
  identity stamping) — pure, fast;
- the EXECUTED ARMS themselves — the grant redeemed through the REAL
  ASGI endpoint over ``httpx.ASGITransport`` (sentinel values, the
  zero-broker refusal assertion), the runner-side typed verification as
  REAL ``python -m forge.lane_driver`` subprocesses against a CANNED
  endpoint (the #320 CD-9 shape: a wrong-slot/expired ANSWER never
  constructs the vendor client), and the #321 rebind digest through a
  REAL ``RunService`` dispatch over the fake native server — the
  persisted ``revision.executor_input_digest`` equals the digest
  recomputed from the RECORDED dispatch variables, and the dispatched
  envelope digest verifies over the dispatched ``FORGE_PLAN`` bytes.

Every mutation arm follows the baseline-first discipline: the BASELINE
passes the SAME trace before the mutant is caught — a mutant that
preserves the DTO values while disconnecting the production caller is
the issue's named trap and has its own arm.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = REPO_ROOT / "scripts" / "gate_conformance.py"
TEMPLATES_DIR = REPO_ROOT / "ci" / "templates"

#: The recipes that MUST ship a credential-consumption block today (the
#: gate's own required set — mirrored here for the spoof lens tests).
BLOCKED_RECIPES = (
    "claude-code.gitlab-ci.yml",
    "claude-sdk-lane.gitlab-ci.yml",
    "forge-harness.github.yml",
    "forge-lane.azure-pipelines.yml",
)


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gate_consumers_under_test", GATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    return _load_gate()


@pytest.fixture(scope="module")
def consumer_seams(gate: ModuleType) -> dict[str, Any]:
    seams = gate._consumer_seams()
    seams["harness_templates"] = gate._load_seams()["HARNESS_TEMPLATES"]
    return seams


# ---------------------------------------------------------------------------
# The grant through the REAL ASGI endpoint — sentinels, zero-broker
# ---------------------------------------------------------------------------


class TestGrantAsgiArms:
    def test_the_granted_route_redeems_the_sibling_refuses_zero_broker(
        self, gate: ModuleType, consumer_seams: dict[str, Any], tmp_path: Path
    ):
        """The P01 world driven through the real endpoint: the granted
        route redeems the DELIVERED sentinel (the baseline leg of the
        trace), the sibling route of the same project refuses typed with
        ZERO broker calls, and the durable audit is value-free."""
        arms = asyncio.run(gate._drive_grant_asgi(consumer_seams, tmp_path))
        by_arm = {arm.arm: arm for arm in arms}
        assert set(by_arm) == {
            "grant:redeem_granted_route",
            "grant:sibling_route_refused_zero_broker",
            "grant:audit_value_free",
            "grant:mutation:dto_preserved_caller_disconnected",
        }
        baseline = by_arm["grant:redeem_granted_route"]
        assert baseline.status == "pass", baseline.detail
        assert baseline.expectations == {
            "http_200": True,
            "delivered_sentinel_redeemed": True,
            "grant_id_join": True,
            "broker_resolved_the_granted_ref": True,
        }
        sibling = by_arm["grant:sibling_route_refused_zero_broker"]
        assert sibling.status == "pass", sibling.detail
        assert sibling.expectations["refused_403"] is True
        assert sibling.expectations["typed_route_mismatch"] is True
        # THE zero-broker assertion: the refusal preceded all broker I/O.
        assert sibling.expectations["zero_broker_calls"] is True
        audit = by_arm["grant:audit_value_free"]
        assert audit.status == "pass", audit.expectations
        assert audit.expectations["audit_is_value_free"] is True

    def test_the_dto_preserving_disconnect_is_caught_on_the_same_trace(
        self, gate: ModuleType, consumer_seams: dict[str, Any], tmp_path: Path
    ):
        """The issue's named trap: a grant document whose every DTO value
        is intact, parked where the production caller cannot load it. The
        BASELINE (the same trace, the properly-keyed grant) redeems; the
        disconnected mutant authorizes NOTHING — typed refusal, zero
        broker calls, no audit row."""
        arms = asyncio.run(gate._drive_grant_asgi(consumer_seams, tmp_path))
        by_arm = {arm.arm: arm for arm in arms}
        assert by_arm["grant:redeem_granted_route"].status == "pass"  # the baseline
        mutant = by_arm["grant:mutation:dto_preserved_caller_disconnected"]
        assert mutant.status == "caught", mutant.detail
        assert mutant.expectations == {
            "refused_403": True,
            "typed_grant_absent": True,
            "zero_broker_calls": True,
            "no_new_audit_row": True,
        }

    def test_no_real_secret_ever_rides_the_arm_environment(
        self, gate: ModuleType, consumer_seams: dict[str, Any], tmp_path: Path
    ):
        """AC-04: the sentinel values are fixture spellings, and the arm
        report carries booleans and refs only — the values never enter
        it, and nothing dials a provider."""
        arms = asyncio.run(gate._drive_grant_asgi(consumer_seams, tmp_path))
        document = "\n".join(str(arm.as_document()) for arm in arms)
        assert gate.DELIVERED_SENTINEL not in document
        assert gate.AMBIENT_SENTINEL not in document


# ---------------------------------------------------------------------------
# The runner-side typed verification — canned answers, real subprocess
# ---------------------------------------------------------------------------


class TestRunnerVerificationArms:
    def test_baseline_passes_then_the_wrong_slot_and_expired_mutants_fail(
        self, gate: ModuleType, consumer_seams: dict[str, Any], tmp_path: Path
    ):
        """The #320 CD-9 shape: against the SAME canned endpoint the
        correct document's driven turn presents the delivered sentinel
        (the baseline — the refusal legs can never pass vacuously), then
        a wrong-slot and an expired ANSWER each halt the lane before the
        vendor client exists: typed ``credential_redemption_failed``, the
        failed axis named, ZERO calls at the model endpoint, and the
        ambient key never substitutes."""
        arms = gate._drive_runner_verification(consumer_seams, tmp_path)
        by_arm = {arm.arm: arm for arm in arms}
        assert by_arm["runner-verification:baseline_applies_delivered"].status == "pass"
        assert by_arm["runner-verification:baseline_applies_delivered"].expectations[
            "endpoint_received_the_delivered_sentinel"
        ]
        for axis in ("env_var", "expires_at"):
            refusal = by_arm[f"runner-verification:{axis}_answer_halts_zero_calls"]
            assert refusal.status == "pass", refusal.detail
            assert refusal.expectations["lane_halted"] is True
            assert refusal.expectations["typed_redemption_failure"] is True
            assert refusal.expectations[f"the_{axis}_axis_named"] is True
            assert refusal.expectations["zero_model_calls"] is True
            assert refusal.expectations["ambient_never_substituted"] is True


# ---------------------------------------------------------------------------
# The #321 rebind digest through the recorded dispatch — the offline
# three-way equality
# ---------------------------------------------------------------------------


class TestRebindDigestArms:
    def test_the_dispatch_carries_the_envelope_and_the_equality_holds(
        self, gate: ModuleType, consumer_seams: dict[str, Any], tmp_path: Path
    ):
        """A REAL ``RunService`` dispatch under an ACTIVE revision: the
        recorded variables carry the executor-input identity, the
        persisted ``revision.executor_input_digest`` equals the digest
        recomputed from THOSE variables, and the dispatched envelope
        digest verifies over the dispatched ``FORGE_PLAN`` bytes."""
        drive = asyncio.run(gate._drive_rebind_digest(consumer_seams, tmp_path))
        by_arm = {arm.arm: arm for arm in drive["arms"]}
        assert by_arm["rebind:dispatch_carries_the_envelope"].status == "pass"
        equality = by_arm["rebind:three_way_digest_equality"]
        assert equality.status == "pass", equality.detail
        assert all(equality.expectations.values())
        assert by_arm["rebind:templates_consume_the_digested_bytes"].status == "pass"
        # the drive recorded the identity it verified (refs/digests only)
        assert "FORGE_PLAN_DIGEST" in drive["recorded_variables"]
        assert "FORGE_BRIEF_ENVELOPE_DIGEST" in drive["recorded_variables"]

    def test_the_plan_binding_removal_and_the_source_identity_swap_are_caught(
        self, gate: ModuleType, consumer_seams: dict[str, Any], tmp_path: Path
    ):
        """The two rebind mutations, on the SAME recorded trace: briefing
        from the superseded SPEC bytes must fail the envelope (the #321
        counterexample), and swapping the plan-digest axis must break the
        identity equality — each caught only after the baseline passed."""
        drive = asyncio.run(gate._drive_rebind_digest(consumer_seams, tmp_path))
        by_arm = {arm.arm: arm for arm in drive["arms"]}
        assert by_arm["rebind:three_way_digest_equality"].status == "pass"  # the baseline
        unbound = by_arm["rebind:mutation:plan_binding_removed"]
        assert unbound.status == "caught", unbound.detail
        assert unbound.expectations["envelope_refuses_the_superseded_brief"] is True
        assert unbound.expectations["the_spec_brief_is_not_the_dispatched_bytes"] is True
        swapped = by_arm["rebind:mutation:source_identity_swapped"]
        assert swapped.status == "caught", swapped.detail
        assert swapped.expectations["a_swapped_source_identity_changes_the_digest"] is True
        assert swapped.expectations["the_active_revision_digest_is_what_was_dispatched"] is True


# ---------------------------------------------------------------------------
# The mutation-suite discipline end to end (one run of check 5)
# ---------------------------------------------------------------------------


class TestCheckFiveEndToEnd:
    def test_every_arm_passes_or_is_caught_and_the_escapes_stay_empty(
        self, gate: ModuleType, tmp_path: Path
    ):
        report = gate.run_consumer_contracts(gate._load_seams(), tmp_path)
        assert report["status"] == "pass", report["failures"]
        assert report["failures"] == []
        assert report["mutation_escapes"] == []
        statuses = {arm["arm"]: arm["status"] for arm in report["arms"]}
        # the contract arms pass; the mutation arms are CAUGHT (never pass)
        for arm, status in statuses.items():
            assert status in {"pass", "caught"}, (arm, status)
        assert statuses["grant:mutation:dto_preserved_caller_disconnected"] == "caught"
        assert statuses["rebind:mutation:plan_binding_removed"] == "caught"
        assert statuses["rebind:mutation:source_identity_swapped"] == "caught"
        # the rebind facts (refs/digests only) ride the report
        assert report["rebind"]["active_revision_digest"]


# ---------------------------------------------------------------------------
# The comment-only marker spoof — a marker in comments is not a consumer
# ---------------------------------------------------------------------------


class TestCommentOnlySpoof:
    def test_the_spoof_transform_comments_exactly_the_block(self, gate: ModuleType):
        text = (TEMPLATES_DIR / "claude-code.gitlab-ci.yml").read_text(encoding="utf-8")
        block = gate.credential_block(text)
        assert block is not None
        spoofed = gate.comment_only_spoof(text, block)
        # every expected STRING still exists in the spoofed file…
        for marker in ("FORGE_CREDENTIAL_REF", "FORGE_BOOTSTRAP_FAILED", "ANTHROPIC_AUTH_TOKEN"):
            assert marker in spoofed
        # …the extraction refuses it (the strings are comments now)
        assert gate.credential_block(spoofed) is None

    def test_a_block_absent_from_its_own_text_refuses_the_transform(self, gate: ModuleType):
        with pytest.raises(gate.PrerequisiteError, match="comment-only spoof"):
            gate.comment_only_spoof("echo unrelated\n", "if true; then\n  echo x\nfi\n")

    def test_the_shipped_blocks_survive_the_stripped_lens(self, gate: ModuleType):
        """The spoof detector's other direction: the SHIPPED recipes'
        blocks extract even with full-line comments stripped — a real
        consumer is never only a comment. (The Azure template's consumer
        mapping is the driver STEP's env mapping, not an in-block export
        — its block need only extract with its fail-closed markers.)"""
        in_block_mapping = BLOCKED_RECIPES[:3]
        for name in BLOCKED_RECIPES:
            text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
            stripped = gate.strip_line_comments(text)
            block = gate.credential_block(stripped)
            assert block is not None, name
            assert "FORGE_CREDENTIAL_REF" in block, name
            assert "FORGE_BOOTSTRAP_FAILED" in block, name
            if name in in_block_mapping:
                assert gate.consumer_export_lines(block), name

    def test_the_spoof_arm_is_caught_on_every_blocked_recipe(
        self, gate: ModuleType, tmp_path: Path
    ):
        """The executed arm: within the secret-consumers check, every
        recipe with a block replays the spoof and the extraction MUST
        refuse it (an escape would mean the gate cannot tell a comment
        from a consumer)."""
        report = gate.run_secret_consumers(gate._load_seams())
        assert report["status"] == "pass", report["failures"]
        by_template = {recipe["template"]: recipe for recipe in report["recipes"]}
        for name in BLOCKED_RECIPES:
            arms = {arm["arm"]: arm for arm in by_template[name]["arms"]}
            spoof = arms["mutation:comment_only_marker_spoof"]
            assert spoof["status"] == "caught", (name, spoof)
            assert spoof["expectations"]["strings_only_in_comments_refuse"] is True
            assert spoof["expectations"]["the_shipped_block_survives_the_stripped_lens"] is True
        assert report["mutation_escapes"] == []

    def test_a_required_recipe_whose_block_is_comment_only_fails_the_check(
        self, gate: ModuleType, monkeypatch, tmp_path: Path
    ):
        """AC-02: a shipped recipe whose expected strings exist ONLY in
        comments is a MISSING consumer implementation — the check fails
        it loudly even though every marker string is present."""
        import shutil
        import tempfile

        sandbox = Path(tempfile.mkdtemp(prefix="spoofed-recipes-", dir=tmp_path))
        try:
            for path in sorted(TEMPLATES_DIR.glob("*.yml")):
                shutil.copy(path, sandbox / path.name)
            target = sandbox / "claude-code.gitlab-ci.yml"
            text = target.read_text(encoding="utf-8")
            block = gate.credential_block(text)
            assert block is not None
            target.write_text(gate.comment_only_spoof(text, block), encoding="utf-8")

            monkeypatch.setattr(gate, "TEMPLATES_DIR", sandbox)
            report = gate.run_secret_consumers(gate._load_seams())
            assert report["status"] == "fail"
            assert "claude-code.gitlab-ci.yml:credential_block_missing" in report["failures"]
            by_template = {recipe["template"]: recipe for recipe in report["recipes"]}
            assert by_template["claude-code.gitlab-ci.yml"]["status"] == "fail"
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)


# ---------------------------------------------------------------------------
# The identity stamping — offline vs native vs paid, distinguishable
# ---------------------------------------------------------------------------


class TestIdentityStamping:
    def test_every_arm_class_is_named_and_scoped(self, gate: ModuleType):
        consumers = {
            "arms": [
                {"arm": "grant:redeem_granted_route", "status": "pass"},
                {"arm": "runner-verification:env_var_answer_halts_zero_calls", "status": "pass"},
                {"arm": "rebind:three_way_digest_equality", "status": "pass"},
            ]
        }
        identities = gate.case_identities(
            {"digest_inventory": {"claude-code.gitlab-ci.yml": "a" * 16}}, consumers
        )
        assert identities["runtime"]["python"]
        assert identities["runtime"]["runner"] in {"local", "github-actions"}
        consumer_block = identities["checks"]["consumer_contracts"]
        assert "ASGI" in consumer_block["consumers"]
        classes = consumer_block["arm_classes"]
        assert any("grant:redeem_granted_route" in arms for arms in classes.values())
        assert any(
            "runner-verification:env_var_answer_halts_zero_calls" in arms
            for arms in classes.values()
        )
        assert any("rebind:three_way_digest_equality" in arms for arms in classes.values())
        # offline / native / paid are DISTINGUISHABLE in the report
        assert set(identities["execution_classes"]) == {"offline", "native", "paid"}
        assert identities["checks"]["shipped_recipes"]["execution"].startswith("offline")
        assert identities["checks"]["native_locators"]["digest_inventory"] == {
            "claude-code.gitlab-ci.yml": "a" * 16
        }

    def test_the_required_case_missing_counter_names_the_gap(
        self, gate: ModuleType, tmp_path: Path
    ):
        """A partial consumer run (the check never reached the rebind
        arms) is NAMED in the observability block — never a silent
        absence inside a green aggregate."""
        report = gate.build_report(
            None,
            None,
            None,
            None,
            None,
            "2026-09-24T00:00:00+00:00",
            0.1,
            [gate.PrerequisiteError("the gate broke before the consumer arms")],
        )
        missing = report["observability"]["conformance.required_case_missing"]
        assert "consumer-contracts/rebind:three_way_digest_equality" in missing
        assert report["observability"]["conformance.executed_case_count"] == 0
