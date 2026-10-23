"""Runtime spend stays honest after qualification economics reports retire."""

import pytest

from forge.adaptive.usage_ingestion import UsageIngestStore, ingest_usage_artifact, spend_cap_check


def test_missing_cost_is_unknown_exposure_instead_of_free_spend():
    store = UsageIngestStore()
    ingest_usage_artifact(
        store,
        {"receipt_id": "lost", "driver": "claude-code", "final": False},
        work_id="w",
        attempt_id="a",
        source="lane",
    )
    result = spend_cap_check(store.rows(), cap_usd=10, projection_usd=0.1)
    assert not result["allowed"]
    assert any(f["type"] == "unbounded-exposure" for f in result["findings"])


def test_partial_subtotal_does_not_release_its_retained_liability():
    store = UsageIngestStore()
    ingest_usage_artifact(
        store,
        {
            "receipt_id": "partial",
            "driver": "claude-code",
            "cost_usd": 0.2,
            "cost_upper_bound_usd": 1.0,
            "final": False,
        },
        work_id="w",
        attempt_id="a",
        source="lane",
    )
    result = spend_cap_check(store.rows(), cap_usd=1, projection_usd=0.1)
    assert not result["allowed"]
    assert result["reserved_usd"] == pytest.approx(1.0)


def test_final_reconciliation_replaces_partial_once_and_replay_is_idempotent():
    store = UsageIngestStore()
    partial = {
        "receipt_id": "call",
        "driver": "claude-code",
        "cost_usd": 0.2,
        "cost_upper_bound_usd": 1.0,
        "final": False,
    }
    final = {**partial, "cost_usd": 0.3, "final": True}
    for artifact in (partial, final, final):
        ingest_usage_artifact(store, artifact, work_id="w", attempt_id="a", source="lane")
    assert len(store.rows()) == 1
    result = spend_cap_check(store.rows(), cap_usd=1, projection_usd=0.1)
    assert result["allowed"]
    assert result["settled_usd"] == pytest.approx(0.3)
    assert result["retained_liability_usd"] == 0
    assert result["reserved_usd"] == pytest.approx(0.3)


def test_estimated_cost_retains_its_basis_and_cannot_claim_human_acceptance():
    store = UsageIngestStore()
    ingest_usage_artifact(
        store,
        {"receipt_id": "estimate", "driver": "claude-code", "cost_usd": 0.2, "final": True},
        work_id="w",
        attempt_id="a",
        source="rate-card",
        cost_basis="estimated",
        rate_card_id="card-1",
    )
    row = store.rows()[0]
    assert row.cost_basis == "estimated"
    assert row.rate_card_id == "card-1"
    assert "accepted" not in row.to_json()
