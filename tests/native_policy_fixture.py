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


async def native_file(body=b"exact target bytes", *, status=None, absent=False):
    import base64
    from hashlib import sha1
    import httpx

    wire = native.Wire()
    oid = sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
    wire.routes["repository/tree"] = (
        [] if absent else [{"path": "uv.lock", "id": oid, "mode": "100644", "type": "blob"}]
    )
    wire.routes["repository/blobs/" + oid] = (
        httpx.Response(status)
        if status
        else {
            "sha": oid,
            "size": len(body),
            "encoding": "base64",
            "content": base64.b64encode(body).decode(),
        }
    )
    wire.routes["repository/files/uv.lock"] = httpx.Response(status or 404)
    api, repositories, *_ = native.adapters(wire)
    try:
        return await repositories.read_blob(native.REPO, native.SHA, "uv.lock")
    finally:
        await api.transport.close()


async def native_checks(status="success", reason=None):
    wire = native.Wire()
    wire.routes["pipelines/700/variables"] = []
    wire.routes["pipelines/700"]["source"] = "push"
    wire.routes["pipelines/700/jobs"][0].update(status=status, failure_reason=reason)
    api, _, _, _, ci, _ = native.adapters(wire)
    try:
        return await ci.checks_for(native.REPO, native.SHA)
    finally:
        await api.transport.close()


def frozen_brief(**changes):
    from forge.domain.approved_input import ApprovedInput
    from forge_protocol import RunIdentity

    frozen = spec(**changes)
    return ApprovedInput.from_spec(frozen).to_brief(
        RunIdentity(forge_run_id="policy-run", attempt=0, generation=0), frozen
    )


def file_guard(root, **changes):
    from forge_lane.drivers.claude_policy import FilePolicy

    brief = frozen_brief(**changes)
    return FilePolicy(
        root, None, scope=brief.frozen_spec.write_policy, execution=brief.execution_policy
    )


def execution_refuses(**changes):
    from forge_protocol import ExecutionPolicy

    document = ExecutionPolicy().model_dump(mode="json")
    document.update(changes)
    with pytest.raises(ValidationError) as failure:
        ExecutionPolicy.model_validate_json(json.dumps(document))
    assert failure.value.errors()
    return failure.value


def spec_refuses(**changes):
    from forge.domain.run_spec import RunSpec2

    document = spec().model_dump(mode="json")
    document.update(changes)
    with pytest.raises(ValidationError) as failure:
        RunSpec2.model_validate_json(json.dumps(document))
    assert failure.value.errors()
    return failure.value


async def isolated_child(root, monkeypatch, **environment):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    case = await completed_case(root)
    assert case.result.report.status == "completed" and case.server.model_keys == 1
    options = case.sdk.clients[0].options
    assert case.sdk.clients[0].disconnected
    return case, options
