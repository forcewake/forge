"""Frozen plans, budget ceilings and lane policy share one approved byte identity."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from hashlib import sha256
from itertools import permutations
import json

import pytest
from pydantic import ValidationError

from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import BudgetLimits, Resources
from forge.domain.paths import PathPolicy, resolve_write_policy
from forge.domain.plan import Citation, Plan, PlanStep, RepositoryIdentity
from forge.domain.run_spec import RunSpec2, VerificationContract
from forge_protocol import (
    Brief,
    ExecutionPolicy,
    McpServerPolicy,
    RunIdentity,
    approved_input_digest,
    canonical_json,
)

BASE = "a" * 40
IDENTITY = RunIdentity(forge_run_id="run-1", attempt=0, generation=0)


def citation(**changes):
    return Citation(
        **{
            "repository": RepositoryIdentity(
                provider="github", connection_id="installation-9", repository_id="123"
            ),
            "source_sha": BASE,
            "path": "src/example.py",
            "start_line": 2,
            "end_line": 7,
            **changes,
        }
    )


def spec(**changes):
    return RunSpec2(
        **{
            "task_title": "Keep the full implementation plan",
            "task_description": "Use the approved source snapshot.",
            "plan": Plan(
                summary="Update the parser and its regression.",
                steps=(
                    PlanStep(
                        step_id="parse",
                        title="Correct parsing",
                        instructions="Retain the complete input and reject trailing garbage.",
                        files=("src/example.py",),
                        citations=(citation(),),
                    ),
                    PlanStep(step_id="verify", title="Verify", instructions="Run parser tests."),
                ),
            ),
            "base_sha": BASE,
            "driver": "claude_code",
            "model": "test-model",
            "target_branch": "main",
            "verification": VerificationContract(required_checks=("unit", "lint")),
            "write_policy": resolve_write_policy("no_dependencies"),
            "limits": BudgetLimits(
                usd=Decimal("10"), calls=20, tokens=10000, wallclock_seconds=600
            ),
            "closing_reserve": Resources(usd=Decimal("2"), calls=2, tokens=1000),
            "planning_allowance": Resources(usd=Decimal("1"), calls=3, tokens=2000),
            **changes,
        }
    )


def wire_document(raw):
    frozen = spec()
    approval = ApprovedInput.from_spec(frozen).model_dump()
    approval["spec_digest"] = sha256(raw.encode("utf-8")).hexdigest()
    approval["approved_digest"] = approved_input_digest(
        **{key: value for key, value in approval.items() if key != "approved_digest"}
    )
    return {**IDENTITY.model_dump(), **approval, "spec_json": raw}


def test_citations_bind_native_identity_and_refuse_ambiguous_ranges():
    original = citation()
    assert citation(source_sha="b" * 40).digest != original.digest
    assert (
        citation(repository=replace_repository(connection_id="installation-10")).digest
        != original.digest
    )
    for changes in (
        {"start_line": 0},
        {"start_line": True},
        {"end_line": 1},
        {"path": "a/../b"},
        {"path": "/etc/passwd"},
        {"path": "a\\b"},
        {"path": ".git/config"},
        {"source_sha": "main"},
        {"repository": {"provider": "github", "connection_id": "x", "repository_id": "org/repo"}},
    ):
        with pytest.raises(ValidationError):
            citation(**changes)
    with pytest.raises(ValidationError):
        original.start_line = 10


def replace_repository(**changes):
    return RepositoryIdentity(**{**citation().repository.model_dump(), **changes})


def test_frozen_spec_roundtrip_and_set_order_have_one_identity():
    paths = {"private/b", "private/a"}
    policy = PathPolicy("custom", paths, (".github/",), sensitive_paths={"ci/b", "ci/a"})
    frozen = spec(write_policy=policy)
    paths.add("added-after-freeze")
    reordered = spec(
        write_policy=replace(
            policy, denied_paths=frozenset(reversed(sorted(paths - {"added-after-freeze"})))
        )
    )
    assert frozen.canonical_bytes == reordered.canonical_bytes
    assert RunSpec2.model_validate_json(frozen.spec_json) == frozen
    assert json.loads(frozen.spec_json)["schema"] == "forge.run-spec/2"
    assert "added-after-freeze" not in frozen.spec_json
    with pytest.raises(FrozenInstanceError):
        policy.name = "changed"
    with pytest.raises(ValidationError):
        frozen.model = "changed"
    for field, value in (("model", "another-model"), ("base_sha", "b" * 40), ("revision", 2)):
        changed = json.loads(frozen.spec_json)
        changed[field] = value
        assert RunSpec2.model_validate_json(json.dumps(changed)).digest != frozen.digest


def test_digest_is_stable_under_document_key_order():
    frozen = spec()
    document = json.loads(frozen.spec_json)
    for keys in permutations(("schema", "model", "driver", "plan", "limits", "revision")):
        reordered = {key: document[key] for key in keys}
        reordered.update({key: value for key, value in document.items() if key not in keys})
        assert RunSpec2.model_validate_json(json.dumps(reordered)).digest == frozen.digest


@pytest.mark.parametrize("axis", ["usd", "calls", "tokens"])
def test_planning_and_closing_caps_cannot_exceed_total_budget(axis):
    frozen = spec()
    limit = getattr(frozen.limits, axis)
    excess = frozen.planning_allowance.model_dump()
    excess[axis] = limit
    with pytest.raises(ValidationError, match="inside total"):
        spec(planning_allowance=Resources(**excess))
    excess[axis] = limit + 1
    with pytest.raises(ValidationError, match="negative"):
        spec(closing_reserve=Resources(**excess))


def test_brief_transports_the_exact_frozen_spec_and_six_field_approval():
    frozen = spec()
    approved = ApprovedInput.from_spec(frozen)
    brief = approved.to_brief(IDENTITY, frozen)
    assert brief.spec_json.encode("utf-8") == frozen.canonical_bytes
    assert brief.execution_policy == frozen.execution_policy == ExecutionPolicy()
    assert set(approved.model_dump()) == {
        "brief_text",
        "approved_digest",
        "plan_digest",
        "spec_digest",
        "base_sha",
        "revision",
    }
    assert approved.approved_digest == approved_input_digest(
        **approved.model_dump(exclude={"approved_digest"})
    )
    assert "Run parser tests." in brief.brief_text and "#L2-L7" in brief.brief_text
    assert Brief.model_validate_json(canonical_json(brief)) == brief
    with pytest.raises(ValueError, match="does not match"):
        approved.to_brief(IDENTITY, spec(model="changed-model"))
    for missing in (None, "", False):
        with pytest.raises(ValidationError):
            Brief.model_validate(
                {**IDENTITY.model_dump(), **approved.model_dump(), "spec_json": missing}
            )
    with pytest.raises(ValidationError):
        Brief.model_validate({**IDENTITY.model_dump(), **approved.model_dump()})


@pytest.mark.parametrize(
    "change",
    [
        "whitespace",
        "duplicate",
        "nonfinite",
        "schema",
        "policy",
        "plan",
        "base",
        "revision",
        "surrogate",
        "unknown_policy_field",
        "deeply_nested",
        "oversized",
    ],
)
def test_noncanonical_or_tampered_spec_is_refused(change):
    frozen = spec()
    document = json.loads(frozen.spec_json)
    if change == "whitespace":
        raw = json.dumps(document, sort_keys=True)
    elif change == "duplicate":
        raw = frozen.spec_json[:-1] + ',"schema":"forge.run-spec/2"}'
    elif change == "nonfinite":
        raw = frozen.spec_json[:-1] + ',"unbound":NaN}'
    elif change == "deeply_nested":
        raw = '{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}"
    elif change == "oversized":
        raw = '{"huge":"' + "я" * (600 * 1024) + '"}'
    else:
        if change == "schema":
            document["schema"] = "forge.run-spec/999"
        elif change == "policy":
            document["execution_policy"]["schema"] = "forge.execution-policy/999"
        elif change == "plan":
            document["plan"]["summary"] = "Different approved plan"
        elif change == "base":
            document["base_sha"] = "b" * 40
        elif change == "revision":
            document["revision"] = True
        elif change == "surrogate":
            raw = frozen.spec_json.replace("test-model", "\\ud800")
        else:
            document["execution_policy"]["ambient_env"] = ["MODEL_KEY"]
        if change != "surrogate":
            raw = canonical_json(document).decode("utf-8")
    with pytest.raises(ValidationError):
        Brief.model_validate(wire_document(raw))
    with pytest.raises(ValidationError, match="digest mismatch"):
        Brief.model_validate(
            {
                **ApprovedInput.from_spec(frozen).to_brief(IDENTITY, frozen).model_dump(),
                "spec_json": frozen.spec_json + " ",
            }
        )


def test_policy_is_explicit_and_refuses_ambient_or_wildcard_authority():
    server = dict(
        name="docs",
        transport="http",
        url="https://docs.example.invalid/mcp",
        tools=("lookup", "search"),
    )
    policy = ExecutionPolicy(mcp_servers=(McpServerPolicy(**server),))
    frozen = spec(execution_policy=policy)
    assert frozen.digest != spec().digest
    assert ApprovedInput.from_spec(frozen).to_brief(IDENTITY, frozen).execution_policy == policy
    for changes in (
        {"tools": ("*",)},
        {"tools": ("search", "lookup")},
        {"tools": ("lookup", "lookup")},
        {"url": "https://user:password@docs.example.invalid/mcp"},
        {"url": "https://docs.example.invalid/mcp?token=value"},
        {"env": {"OIDC_TOKEN": "ambient"}},
        {"headers": {"Authorization": "Bearer ambient"}},
        {"command": "/bin/sh"},
        {"tools": ("server__tool",)},
    ):
        with pytest.raises(ValidationError):
            McpServerPolicy(**{**server, **changes})
    with pytest.raises(ValidationError):
        ExecutionPolicy.model_validate({"schema": "forge.execution-policy/2"})
    with pytest.raises(ValidationError):
        ExecutionPolicy(mcp_servers=policy.mcp_servers * 2)
    with pytest.raises(ValidationError):
        McpServerPolicy(name="docs", transport="stdio", tools=("lookup",), command="npx")
    assert (
        McpServerPolicy(
            name="docs",
            transport="stdio",
            tools=("lookup",),
            command="/opt/mcp/docs",
            args=("--stdio",),
        ).transport
        == "stdio"
    )
