"""ADR-0023: the pure harness-selection compiler + dispatch fallback step.

Rules 1–6 of the v0.9 brief §2, each pinned by its own test, plus the
shared ``advance_harness_fallback`` helper (§6): OFF by default,
infrastructure-only, pre-candidate-only, chain-exhaustion fails visibly.
No network, no DB — everything here is a pure function of its inputs.
"""

import pytest

from forge.runs.harness_selection import (
    BUDGET_CLASSES,
    DEFAULT_DRIVER,
    SHIPPED_DRIVERS,
    advance_harness_fallback,
    compile_harness_selection,
    current_driver,
    parse_preference,
    selection_from_spec_document,
    validate_preference,
)

LANES = {"claude-code", "grok-build", "opencode", "copilot"}


class TestCompilerRules:
    def test_rule_1_lanes_cap_the_result(self):
        """A proposal can reorder, never extend: a lane the project did not
        onboard can never be selected, whatever the planner asks for."""
        selection = compile_harness_selection(
            ["claude-code", "grok-build"],
            "ci_harness:claude-code",
            available_lanes={"grok-build"},  # claude-code not onboarded
            planner_proposal={"harness": "claude-code", "reason": "wants claude"},
        )
        assert selection.harness == "grok-build"
        assert selection.fallbacks == ()

    def test_rule_2_empty_preference_is_the_backend_one_element_list(self):
        """Byte-compatible default: no preference ⇒ [current_backend], no
        fallbacks — the pre-ADR-0023 behavior, verbatim."""
        selection = compile_harness_selection([], "ci_harness:opencode", LANES, None)
        assert selection.harness == "opencode"
        assert selection.fallbacks == ()
        assert selection.budget_class == "standard"
        assert selection.reason == "default"

    def test_rule_2_bare_ci_harness_defaults_to_claude_code(self):
        selection = compile_harness_selection([], "ci_harness", LANES, None)
        assert selection.harness == DEFAULT_DRIVER

    def test_rule_3_non_onboarded_preference_entries_are_dropped(self):
        selection = compile_harness_selection(
            ["copilot", "claude-code", "grok-build"],
            "ci_harness:claude-code",
            available_lanes={"claude-code", "grok-build"},  # copilot not onboarded
            planner_proposal=None,
        )
        assert selection.harness == "claude-code"
        assert selection.fallbacks == ("grok-build",)  # the tail is ∩ available

    def test_rule_4_planner_proposal_honored_within_preference_and_lanes(self):
        selection = compile_harness_selection(
            ["claude-code", "grok-build"],
            "ci_harness:claude-code",
            LANES,
            {"harness": "grok-build", "budget_class": "trivial", "reason": "docs one-liner"},
        )
        assert selection.harness == "grok-build"
        assert selection.fallbacks == ()
        assert selection.budget_class == "trivial"
        assert selection.reason == "docs one-liner"

    def test_rule_4_proposal_outside_preference_is_ignored(self):
        """The planner is never the authority: a harness outside the
        preference (∩ lanes) degrades to the chain head."""
        selection = compile_harness_selection(
            ["claude-code"],
            "ci_harness:claude-code",
            LANES,
            {"harness": "copilot", "reason": "overreach"},
        )
        assert selection.harness == "claude-code"
        assert selection.reason == "default"  # not honored → planner reason too

    def test_rule_4_invalid_budget_class_falls_back_to_default(self):
        selection = compile_harness_selection(
            ["claude-code"],
            "ci_harness:claude-code",
            LANES,
            {"harness": "claude-code", "budget_class": "galactic"},
        )
        assert selection.budget_class == "standard"

    def test_rule_4_missing_reason_defaults_to_planner_selection(self):
        selection = compile_harness_selection(
            ["claude-code", "opencode"],
            "ci_harness:claude-code",
            LANES,
            {"harness": "opencode"},
        )
        assert selection.harness == "opencode"
        assert selection.reason == "planner selection"

    def test_rule_4_budget_class_applies_even_without_harness(self):
        """The budget-class clause is independent of the harness clause."""
        selection = compile_harness_selection(
            ["claude-code"], "ci_harness:claude-code", LANES, {"budget_class": "heavy"}
        )
        assert selection.harness == "claude-code"
        assert selection.budget_class == "heavy"
        assert selection.reason == "default"

    def test_rule_5_fallback_tail_frozen_even_when_switch_is_off(self):
        """The spec describes the chain; the runtime switch is a separate
        policy — the tail is recorded either way."""
        selection = compile_harness_selection(
            ["claude-code", "grok-build", "opencode"],
            "ci_harness:claude-code",
            LANES,
            None,
        )
        assert selection.fallbacks == ("grok-build", "opencode")

    def test_rule_5_proposal_selection_splits_the_tail(self):
        selection = compile_harness_selection(
            ["claude-code", "grok-build", "opencode"],
            "ci_harness:claude-code",
            LANES,
            {"harness": "grok-build"},
        )
        assert selection.harness == "grok-build"
        assert selection.fallbacks == ("opencode",)

    def test_rule_6_deterministic_same_inputs_identical_output(self):
        kwds = {
            "preference": ["claude-code", "grok-build", "opencode"],
            "current_backend": "ci_harness:claude-code",
            "available_lanes": LANES,
            "planner_proposal": {"harness": "grok-build", "budget_class": "heavy"},
        }
        first = compile_harness_selection(**kwds)
        for _ in range(5):
            assert compile_harness_selection(**kwds) == first

    def test_malformed_planner_proposal_is_tolerated(self):
        """Parsing stays lenient: a non-dict proposal is no proposal at all."""
        selection = compile_harness_selection(
            ["claude-code"],
            "ci_harness:claude-code",
            LANES,
            "garbage",  # type: ignore[arg-type]
        )
        assert selection.harness == "claude-code"
        assert selection.reason == "default"


class TestBudgetClasses:
    def test_budget_class_set_is_the_briefs_triple(self):
        assert BUDGET_CLASSES == {"trivial", "standard", "heavy"}


class TestCurrentDriver:
    @pytest.mark.parametrize(
        ("backend", "driver"),
        [
            ("ci_harness:grok-build", "grok-build"),
            ("ci_harness", DEFAULT_DRIVER),
            ("builtin", DEFAULT_DRIVER),
            ("", DEFAULT_DRIVER),
            (None, DEFAULT_DRIVER),
        ],
    )
    def test_driver_resolution(self, backend, driver):
        assert current_driver(backend) == driver


class TestParsePreference:
    def test_splits_strips_and_deduplicates(self):
        assert parse_preference(" claude-code, grok-build ,claude-code,") == [
            "claude-code",
            "grok-build",
        ]

    def test_empty_is_empty(self):
        assert parse_preference("") == []
        assert parse_preference(None) == []


class TestValidatePreference:
    def test_unknown_driver_is_refused(self):
        with pytest.raises(ValueError, match="unknown harness driver"):
            validate_preference(["claude-code", "warp"], "claude-code")

    def test_list_must_include_the_configured_backend(self):
        with pytest.raises(ValueError, match="tightens, never deselects"):
            validate_preference(["claude-code"], "grok-build")

    def test_valid_list_passes(self):
        validate_preference(["claude-code", "grok-build"], "claude-code")

    def test_empty_list_is_always_valid(self):
        validate_preference([], "copilot")

    def test_shipped_driver_set_is_the_four_templates(self):
        assert SHIPPED_DRIVERS == {"claude-code", "grok-build", "opencode", "copilot"}


class TestSelectionFromSpecDocument:
    def test_round_trips_the_backend_config_fragment(self):
        selection = compile_harness_selection(
            ["claude-code", "grok-build"], "ci_harness:claude-code", LANES, None
        )
        document = {"backend_config": {"backend": "ci_harness", **selection.as_document()}}
        assert selection_from_spec_document(document) == selection

    def test_pre_v2_document_has_no_selection(self):
        assert selection_from_spec_document({"backend_config": {"backend": "builtin"}}) is None
        assert selection_from_spec_document(None) is None
        assert selection_from_spec_document({}) is None


class TestAdvanceHarnessFallback:
    """Brief §6: OFF by default, infrastructure-only, pre-candidate-only."""

    def make_selection(self) -> object:
        return compile_harness_selection(
            ["claude-code", "grok-build", "opencode"], "ci_harness:claude-code", LANES, None
        )

    def test_off_by_default_never_advances(self):
        selection = self.make_selection()
        assert (
            advance_harness_fallback(
                selection,  # type: ignore[arg-type]
                failed_driver="claude-code",
                failure_kind="infrastructure",
                fallback_enabled=False,
                candidate_exists=False,
            )
            is None
        )

    def test_infrastructure_failure_advances_to_the_next_entry(self):
        selection = self.make_selection()
        nxt = advance_harness_fallback(
            selection,  # type: ignore[arg-type]
            failed_driver="claude-code",
            failure_kind="infrastructure",
            fallback_enabled=True,
            candidate_exists=False,
        )
        assert nxt is not None
        assert nxt.harness == "grok-build"
        assert nxt.fallbacks == ("opencode",)
        assert nxt.budget_class == selection.budget_class  # type: ignore[attr-defined]
        assert "claude-code" in nxt.reason and "infrastructure" in nxt.reason

    def test_code_failure_never_switches(self):
        """A CI-code failure is a signal about the CHANGE (ADR-0008)."""
        selection = self.make_selection()
        assert (
            advance_harness_fallback(
                selection,  # type: ignore[arg-type]
                failed_driver="claude-code",
                failure_kind="code",
                fallback_enabled=True,
                candidate_exists=False,
            )
            is None
        )

    def test_config_failure_never_switches(self):
        selection = self.make_selection()
        assert (
            advance_harness_fallback(
                selection,  # type: ignore[arg-type]
                failed_driver="claude-code",
                failure_kind="config",
                fallback_enabled=True,
                candidate_exists=False,
            )
            is None
        )

    def test_never_switches_once_a_candidate_exists(self):
        """One frozen attempt base → one candidate → one producer (ADR-0016)."""
        selection = self.make_selection()
        assert (
            advance_harness_fallback(
                selection,  # type: ignore[arg-type]
                failed_driver="claude-code",
                failure_kind="infrastructure",
                fallback_enabled=True,
                candidate_exists=True,
            )
            is None
        )

    def test_stale_event_for_a_different_driver_is_ignored(self):
        selection = self.make_selection()
        assert (
            advance_harness_fallback(
                selection,  # type: ignore[arg-type]
                failed_driver="opencode",
                failure_kind="infrastructure",
                fallback_enabled=True,
                candidate_exists=False,
            )
            is None
        )

    def test_chain_exhaustion_returns_none(self):
        """The last entry failing means fail visibly — wait for a human."""
        selection = compile_harness_selection(
            ["claude-code"], "ci_harness:claude-code", LANES, None
        )
        assert selection.fallbacks == ()
        assert (
            advance_harness_fallback(
                selection,
                failed_driver="claude-code",
                failure_kind="infrastructure",
                fallback_enabled=True,
                candidate_exists=False,
            )
            is None
        )

    def test_chain_walks_to_exhaustion(self):
        selection = self.make_selection()
        second = advance_harness_fallback(
            selection,  # type: ignore[arg-type]
            failed_driver="claude-code",
            failure_kind="infrastructure",
            fallback_enabled=True,
            candidate_exists=False,
        )
        assert second is not None and second.harness == "grok-build"
        third = advance_harness_fallback(
            second,
            failed_driver="grok-build",
            failure_kind="infrastructure",
            fallback_enabled=True,
            candidate_exists=False,
        )
        assert third is not None and third.harness == "opencode"
        assert third.fallbacks == ()
        assert (
            advance_harness_fallback(
                third,
                failed_driver="opencode",
                failure_kind="infrastructure",
                fallback_enabled=True,
                candidate_exists=False,
            )
            is None
        )
