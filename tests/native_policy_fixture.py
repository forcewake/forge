"""Trusted immutable policy, current runtime scope and native lane preflight fixtures."""

import json

import pytest
from pydantic import ValidationError

from forge.domain.run_spec import RunPolicy
from forge.domain.plan import Plan, PlanStep
from forge.engine.plan_values import PlanIntent
from forge.port_values import NeighborSnapshot, PlanningSnapshot, TaskSnapshot
from forge.runtime.settings import RuntimeSettings
from forge_protocol import canonical_json, approved_input_digest, digest
from tests import gitlab_engine_fixture as native
from tests.native_driver_fixture import completed_case
from tests.runtime_fixture import configuration
from tests.test_domain_approved_input import spec


def policy(**changes):
    frozen = spec(**changes)
    document = frozen.model_dump(mode="json")
    return RunPolicy.model_validate_json(
        canonical_json({key: document[key] for key in RunPolicy.model_fields})
    )


def intent(**changes):
    return PlanIntent(
        actor=native.HUMAN,
        task=TaskSnapshot(
            subject=native.SUBJECT,
            title="Approved task",
            description="Exact original text",
            version="native-v1",
        ),
        snapshot=PlanningSnapshot(primary=NeighborSnapshot(repo=native.REPO, sha=native.SHA)),
        policy=policy(**changes),
    )


def runtime_document(root):
    config, _ = configuration(root)
    return config.model_dump(mode="json", by_alias=True)


def runtime_refuses(root, mutate):
    document = runtime_document(root)
    mutate(document)
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate_json(json.dumps(document))


def policy_refuses(**changes):
    document = policy().model_dump(mode="json")
    document.update(changes)
    with pytest.raises(ValidationError):
        RunPolicy.model_validate_json(json.dumps(document))


async def switched_driver_refuses(root, provider="gitlab", other="copilot_cli"):
    def mutate(case):
        case.dispatch = case.dispatch.model_copy(update={"driver": other})

    case = await completed_case(root, provider=provider, nonce="policy-nonce", mutate=mutate)
    assert case.result.report.classification == "frozen_dispatch_mismatch"
    assert case.server.model_keys == 0 and not case.sdk.clients and not case.server.candidates
    return case


async def execution_policy_refuses(root, changes):
    def mutate(case):
        document = json.loads(case.brief.spec_json)
        document["execution_policy"].update(changes)
        raw = canonical_json(document).decode()
        spec_digest = digest(document)
        approval = approved_input_digest(
            brief_text=case.brief.brief_text,
            plan_digest=case.brief.plan_digest,
            spec_digest=spec_digest,
            base_sha=case.base,
            revision=case.brief.revision,
        )
        case.server.brief.update(spec_json=raw, spec_digest=spec_digest, approved_digest=approval)
        case.dispatch = case.dispatch.model_copy(update={"approved_digest": approval})

    case = await completed_case(root, mutate=mutate)
    assert case.result.report.classification == "invalid_brief"
    assert case.server.model_keys == 0 and not case.sdk.clients and not case.server.candidates
    return case


def planned(summary="Approved implementation"):
    return Plan(
        summary=summary,
        steps=(
            PlanStep(
                step_id="implement",
                title="Implement",
                instructions="Follow the exact approved task",
            ),
        ),
    )
