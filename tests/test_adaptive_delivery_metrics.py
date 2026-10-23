"""Actual usage/exposure accounting replaces optional delivery grading."""

from dataclasses import FrozenInstanceError
import pytest
from forge.adaptive.usage_types import ProviderRoute
from forge.adaptive.usage_ingestion import UsageIngestStore, ingest_usage_artifact, spend_cap_check


def test_provider_route_is_an_immutable_runtime_identity():
    route = ProviderRoute("gateway-a", "model-a")
    assert route != ProviderRoute("gateway-b", "model-a")
    assert route != ProviderRoute("gateway-a", "model-b")
    assert route.to_json() == {"provider": "gateway-a", "model": "model-a"}
    with pytest.raises(FrozenInstanceError):
        route.model = "unapproved"


def test_missing_receipt_cost_cannot_become_a_free_delivery():
    store = UsageIngestStore()
    ingest_usage_artifact(
        store,
        {"receipt_id": "missing-cost", "driver": "claude-code", "final": False},
        work_id="work",
        attempt_id="attempt",
        source="lane",
    )
    check = spend_cap_check(store.rows(), cap_usd=10, projection_usd=0.1)
    assert not check["allowed"] and check["requires_bounded_policy"]
    assert store.rows()[0].cost_usd is None


def test_failed_attempt_receipts_stay_in_the_actual_spend_population():
    store = UsageIngestStore()
    for attempt in ("failed-attempt", "current-attempt"):
        ingest_usage_artifact(
            store,
            {
                "receipt_id": "call-" + attempt,
                "driver": "claude-code",
                "cost_usd": 0.4,
                "final": True,
            },
            work_id="work",
            attempt_id=attempt,
            source="lane",
        )
    check = spend_cap_check(store.rows(), cap_usd=1, projection_usd=0.3)
    assert check["settled_usd"] == pytest.approx(0.8)
    assert not check["allowed"]
    assert {row.attempt_id for row in store.rows()} == {"failed-attempt", "current-attempt"}
