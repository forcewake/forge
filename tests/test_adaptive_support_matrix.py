"""Actual lane admission replaces qualification support badges.

A registration or model preference never authorizes a lane the project did
not onboard. These are compiler decisions, not claims of live vendor support.
"""

import pytest

from forge.runs.harness_selection import compile_harness_selection, validate_preference


def test_project_inventory_caps_model_preference():
    selection = compile_harness_selection(
        ["claude-code", "copilot"],
        "ci_harness:claude-code",
        {"copilot"},
        {"harness": "claude-code", "reason": "model preference"},
    )
    assert selection.harness == "copilot"
    assert selection.fallbacks == ()
    assert selection.reason != "model preference"


def test_disjoint_project_inventory_refuses_instead_of_creating_support():
    with pytest.raises(ValueError, match="manifest|available|onboard|empty|lane"):
        compile_harness_selection(["claude-code"], "ci_harness:claude-code", {"copilot"}, None)


def test_unknown_driver_is_not_a_supported_registration():
    with pytest.raises(ValueError):
        validate_preference(["invented-vendor"])


def test_model_can_request_budget_class_but_not_new_numeric_limits():
    selected = compile_harness_selection(
        ["claude-code"],
        "ci_harness:claude-code",
        {"claude-code"},
        {"harness": "claude-code", "budget_class": "unlimited", "max_tokens": 10**12},
    )
    assert selected.budget_class == "standard"
