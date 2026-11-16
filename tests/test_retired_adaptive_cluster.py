"""The first retired adaptive cluster is absent from the product package."""

import importlib.util
import pytest


@pytest.mark.parametrize("name", ["workpackage_service", "runtime", "dedup", "snapshots"])
def test_retired_test_only_module_is_absent(name):
    try:
        found = importlib.util.find_spec(f"forge.adaptive.{name}")
    except ModuleNotFoundError:
        found = None
    assert found is None
