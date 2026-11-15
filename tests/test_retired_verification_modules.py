"""Retired test-only verifier modules do not ship in the product."""

import importlib.util
import pytest


@pytest.mark.parametrize(
    "name",
    [
        "system_verification",
        "executed_evidence",
        "independent_checks",
        "verification_executor",
        "dotnet_recipe",
    ],
)
def test_retired_verifier_module_is_absent(name):
    try:
        found = importlib.util.find_spec(f"forge.adaptive.{name}")
    except ModuleNotFoundError:
        found = None
    assert found is None
