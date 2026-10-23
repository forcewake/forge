"""Pricing refuses uncovered inputs before the native callback can reserve money."""

from decimal import Decimal
import json

import pytest

from tools.litellm.guard_policy import BudgetDenied, Policy, Tariff


def tariff():
    return Tariff.parse(
        {
            "native_model": "anthropic/approved-model",
            "input_rate": "0.000006",
            "output_rate": "0.000015",
            "max_input_tokens": 2048,
            "max_output_tokens": 256,
        }
    )


@pytest.mark.parametrize("route", ["completion", "acompletion", "anthropic_messages"])
def test_exact_positive_quote_covers_full_context_and_bounded_output(route):
    data = {
        "model": "approved",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 128,
    }
    assert tariff().quote(data, route) == Decimal("0.016128")


@pytest.mark.parametrize(
    "changes",
    [
        {"max_tokens": 0},
        {"max_tokens": None},
        {"max_tokens": True},
        {"max_tokens": 257},
        {"n": 2},
        {"best_of": 2},
        {"web_search_options": {}},
        {"audio": {}},
        {"messages": [{"role": "user", "content": [{"type": "image", "source": "unpriced"}]}]},
        {"tools": [{"type": "web_search_20250305", "name": "search"}]},
        {"messages": [{"role": "user", "content": "x" * 2048}]},
        {"prompt": "hidden text-completion input"},
        {"speed": "fast"},
        {"service_tier": "priority"},
        {"inference_geo": "us"},
        {"context_management": {}},
        {"extra_headers": {"anthropic-beta": "fast-mode-2026-02-01"}},
        {"extra_headers": {"x-provider-mode": "unpriced"}},
    ],
)
def test_unknown_or_unbounded_pricing_inputs_refuse(changes):
    with pytest.raises(BudgetDenied):
        tariff().quote(
            {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 128, **changes},
            "anthropic_messages",
        )


@pytest.mark.parametrize("rate", ["0", "-1", "NaN", "Infinity", 0.0001, True])
def test_free_unknown_or_inexact_tariff_never_enables_admission(rate):
    with pytest.raises(BudgetDenied):
        Tariff.parse(
            {
                "native_model": "model",
                "input_rate": rate,
                "output_rate": "0.001",
                "max_input_tokens": 1024,
                "max_output_tokens": 128,
            }
        )


def test_policy_schema_and_duplicate_keys_are_refused():
    with pytest.raises(BudgetDenied):
        Policy.parse(b'{"schema":"wrong","litellm_version":"1.106.2","models":{}}')
    with pytest.raises(BudgetDenied):
        Policy.parse(b'{"schema":"forge.strict-budget/1","schema":"forge.strict-budget/1"}')
    with pytest.raises(BudgetDenied):
        Policy.parse(
            json.dumps(
                {"schema": "forge.strict-budget/1", "litellm_version": "1.106.2", "models": {}}
            ).encode()
        )


def test_approved_text_beta_does_not_change_the_physical_context_reservation():
    assert tariff().quote(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 128,
            "extra_headers": {
                "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14"
            },
        },
        "anthropic_messages",
    ) == Decimal("0.016128")


@pytest.mark.parametrize("ttl", ["5m", "1h"])
def test_real_sdk_text_cache_control_remains_within_trusted_input_rate(ttl):
    assert tariff().quote(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "hello",
                            "cache_control": {"type": "ephemeral", "ttl": ttl},
                        }
                    ],
                }
            ],
            "max_tokens": 128,
        },
        "anthropic_messages",
    ) == Decimal("0.016128")


def test_native_guard_source_pin_covers_the_actual_deployed_bootstrap_and_callbacks():
    from forge.budget.strict import GUARD_DIGEST
    from tools.litellm.guard_policy import code_digest

    assert code_digest() == GUARD_DIGEST


def test_readiness_requires_exact_nonce_code_policy_artifact_and_native_routes():
    from forge.budget.strict import ALLOWED_ROUTES, GUARD_DIGEST, TrustedPolicy

    policy = TrustedPolicy.parse(
        json.dumps(
            {
                "schema": "forge.strict-budget/1",
                "litellm_version": "1.106.2",
                "models": {
                    "approved": {
                        "native_model": "anthropic/approved",
                        "input_rate": "0.000002",
                        "output_rate": "0.000005",
                        "max_input_tokens": 200000,
                        "max_output_tokens": 64000,
                    }
                },
            }
        ).encode()
    )
    ready = {
        "schema": "forge.strict-budget-readiness/1",
        "nonce": "a" * 32,
        "guard_digest": GUARD_DIGEST,
        "policy_digest": policy.digest,
        "artifact_digest": "b" * 64,
        "litellm_version": "1.106.2",
        "models": ["approved"],
        "routes": list(ALLOWED_ROUTES),
        "durable_unknown_holds": True,
    }
    assert policy.accepts(ready, "a" * 32, "approved", "b" * 64)
    assert not policy.accepts(ready, "a" * 32, "approved", None)
    assert not policy.accepts(ready, "a" * 32, "approved", "c" * 64)
    assert not policy.accepts(ready, "c" * 32, "approved", "b" * 64)
    for field, value in (
        ("guard_digest", "c" * 64),
        ("policy_digest", "c" * 64),
        ("litellm_version", "1.80.11"),
        ("models", ["approved", "ambient"]),
        ("routes", ["llm_api_routes"]),
        ("durable_unknown_holds", False),
    ):
        assert not policy.accepts({**ready, field: value}, "a" * 32, "approved", "b" * 64)


def test_packaged_native_configuration_cannot_change_after_the_manifest(tmp_path, monkeypatch):
    from pathlib import Path
    import shutil
    from tools.litellm.bootstrap import verify_artifact
    from tools.litellm.build_manifest import FILES, manifest

    source = Path(__file__).parents[1] / "tools/litellm"
    for name in FILES:
        shutil.copy2(source / name, tmp_path / name)
    monkeypatch.setattr("tools.litellm.bootstrap.version", lambda package: "1.106.2")
    (tmp_path / "artifact.json").write_bytes(manifest(tmp_path))
    assert len(verify_artifact(tmp_path)) == 64
    (tmp_path / "config.yaml").write_text("litellm_settings: {callbacks: []}\n")
    with pytest.raises(ValueError, match="changed after build"):
        verify_artifact(tmp_path)


@pytest.mark.parametrize(
    "value,expected",
    (
        (0.0048679999999999995, "0.004868000000000000"),
        ("0.0000000000000000001", "0.000000000000000001"),
        ("0", "0"),
        ("1.234567890123456789", "1.234567890123456789"),
    ),
)
def test_native_float_usage_rounds_up_without_forgiving_unknown_or_subcent_spend(value, expected):
    from tools.litellm.guard_policy import observed_cost

    assert observed_cost(value) == Decimal(expected)
    assert observed_cost(value) >= Decimal(str(value))


@pytest.mark.parametrize("value", (None, True, "NaN", "Infinity", "-0.1", "1e20"))
def test_native_unknown_or_nonfinite_usage_cannot_settle_a_hold(value):
    from tools.litellm.guard_policy import observed_cost

    with pytest.raises((ValueError, ArithmeticError)):
        observed_cost(value)
