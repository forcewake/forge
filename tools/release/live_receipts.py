"""Read native and normalized SQL evidence; never manufacture execution authority."""

import asyncio
import hashlib
import json
import time

from pydantic import TypeAdapter
from sqlalchemy import select

from forge_protocol import CandidateBundle, LaneDispatch, LaneReport, digest
from forge.domain.budget import Pool
from forge.domain.candidate import ValidatedCandidate
from forge.domain.verdicts import CheckVerdict, Committed
from forge.port_values import (
    Applied,
    CheckResult,
    CommentOutcome,
    DraftOutcome,
    Found,
    LaneHandle,
    ReviewVerdict,
)
from forge.store.approval import ApprovalStore
from forge.store.models_budget import EngineBudgetPool
from forge.store.models_execution import EngineCandidate, EngineEffect
from forge.store.models_lane import EngineLaneSession
from forge.store.models_runs import EngineApprovedInput, EngineGate, EngineRun
from forge.verification.checks import code_failure, verdict
from tools.release.live_pins import IMPLEMENTATION, ORACLE, ORACLE_PATH, literal_return


async def wait_run(sandbox, run_id: str, wanted: str, timeout: int):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await sandbox.server.get("api/v1/runs/" + run_id, headers=sandbox.operator)
        response.raise_for_status()
        row = response.json()
        expected = (
            sandbox.connection.provider,
            sandbox.connection.key,
            sandbox.repository.native_id,
        )
        if (row["provider"], row["connection_id"], row["repository_id"]) != expected:
            raise ValueError("operator run receipt escaped the selected sandbox")
        if row["state"] == wanted:
            return row
        if row["state"] in {"blocked", "failed", "cancelled", "ready_for_human"}:
            raise ValueError("release run stopped before " + wanted + ": " + row["state"])
        await asyncio.sleep(5)
    raise TimeoutError("release run did not reach " + wanted)


async def rows(runtime, model, run_id):
    async with runtime.factory() as session:
        return list(await session.scalars(select(model).where(model.run_id == run_id)))


def known(effect):
    if (
        effect.state not in {"committed", "adopted"}
        or effect.outcome_json is None
        or digest(json.loads(effect.payload_json)) != effect.payload_digest
    ):
        raise ValueError("release effect is unresolved or its immutable payload changed")
    return json.loads(effect.payload_json)


def check_metadata(check):
    return {
        key: getattr(check, key)
        for key in (
            "native_id",
            "native_run_id",
            "workflow_path",
            "attempt",
            "status",
            "failure_kind",
        )
    }


async def approval(runtime, sandbox, run_id, base):
    frozen = await ApprovalStore(runtime.factory).load(run_id)
    gates = await rows(runtime, EngineGate, run_id)
    approved = await rows(runtime, EngineApprovedInput, run_id)
    if (
        frozen is None
        or frozen.state != "consumed"
        or frozen.spec.base_sha != base
        or len(gates) != 1
        or gates[0].consumed_by != sandbox.actor.digest
        or gates[0].consumed_at is None
        or len(approved) != 1
    ):
        raise ValueError("live run lacks its exact consume-once native actor approval")
    if (
        approved[0].digest != frozen.approved.approved_digest
        or approved[0].spec_digest != frozen.spec.digest
        or approved[0].base_sha != base
    ):
        raise ValueError("live approved input differs from its frozen plan")
    return frozen


async def candidate(runtime, sandbox, row, effects, frozen, expected_base):
    bundle = CandidateBundle.model_validate_json(row.manifest_json)
    if (
        row.state != "published"
        or row.published_sha is None
        or row.digest != digest(bundle)
        or row.base_sha != expected_base
        or bundle.base_sha != expected_base
        or bundle.approved_digest != frozen.approved.approved_digest
    ):
        raise ValueError("published candidate differs from its immutable lane artifact")
    matches = [
        effect
        for effect in effects
        if effect.kind == "commit"
        and Committed.model_validate_json(effect.outcome_json).sha == row.published_sha
    ]
    if len(matches) != 1:
        raise ValueError("published candidate requires one native commit receipt")
    effect = matches[0]
    payload = known(effect)
    validated = TypeAdapter(ValidatedCandidate).validate_json(json.dumps(payload["candidate"]))
    if (
        tuple(entry.path for entry in validated.manifest) != (IMPLEMENTATION,)
        or payload["expected_head"] != expected_base
    ):
        raise ValueError("native commit changed scope or its exact parent")
    proof = await sandbox.native.repository.probe_commit(
        sandbox.native.ref, payload["branch"], payload["marker"], expected_base, validated
    )
    if (
        not isinstance(proof, Found)
        or not isinstance(proof.value, Committed)
        or proof.value.sha != row.published_sha
    ):
        raise ValueError("native commit tree, marker and parent do not prove this candidate")
    if (
        await sandbox.native.repository.read_blob(
            sandbox.native.ref, row.published_sha, ORACLE_PATH
        )
        != ORACLE
    ):
        raise ValueError("candidate changed its independent fixed oracle")
    return {
        "sha": row.published_sha,
        "base_sha": row.base_sha,
        "attempt": row.attempt,
        "generation": row.generation,
        "diff_sha256": bundle.diff_sha256,
    }


async def dispatches(runtime, sandbox, run_id, effects, candidates, frozen):
    sessions = sorted(await rows(runtime, EngineLaneSession, run_id), key=lambda item: item.attempt)
    dispatched = [item for item in effects if item.kind == "dispatch_lane"]
    if len(sessions) != len(candidates) or len(dispatched) != len(candidates):
        raise ValueError("release lane lacks one native dispatch/session per candidate")
    result = []
    for session, candidate_row in zip(sessions, candidates, strict=True):
        dispatch = LaneDispatch.model_validate_json(session.dispatch_json)
        receipt = json.loads(session.api_state_json)
        report = LaneReport.model_validate_json(json.dumps(receipt["report"]))
        bundle = CandidateBundle.model_validate_json(json.dumps(receipt["candidate"]))
        if (
            not session.nonce_consumed
            or session.first_job_json is None
            or report.status != "completed"
            or report.candidate_diff_sha256 != bundle.diff_sha256
            or bundle.diff_sha256 != candidate_row["diff_sha256"]
            or dispatch.execution_base_sha != candidate_row["base_sha"]
            or dispatch.approved_digest != frozen.approved.approved_digest
        ):
            raise ValueError("lane artifact/report lacks its exact native OIDC dispatch owner")
        matching = [
            effect
            for effect in dispatched
            if LaneDispatch.model_validate_json(known(effect)["dispatch_json"]) == dispatch
        ]
        if len(matching) != 1:
            raise ValueError("lane session does not match its immutable effect journal")
        outcome = json.loads(matching[0].outcome_json)
        handle = LaneHandle.model_validate_json(json.dumps(outcome["value"]))
        observed = await sandbox.native.ci.lane_status(handle)
        if observed.status != "succeeded":
            raise ValueError("the exact native lane execution did not succeed")
        first = json.loads(session.first_job_json)
        if sandbox.connection.provider == "github":
            if (first.get("provider_run_id"), first.get("provider_attempt")) != (
                handle.native_id,
                str(handle.attempt),
            ):
                raise ValueError("GitHub OIDC first execution differs from its native attempt")
        else:
            job = await sandbox.native.ci.api.read(
                sandbox.native.ref, "jobs/" + first["gitlab_job_id"]
            )
            if (
                str(job.document["pipeline"]["id"]) != handle.native_id
                or job.document["commit"]["id"] != dispatch.execution_base_sha
            ):
                raise ValueError("GitLab first OIDC job belongs to another native pipeline")
        result.append(
            {
                "native_id": handle.native_id,
                "attempt": dispatch.attempt,
                "generation": dispatch.generation,
                "execution_base_sha": dispatch.execution_base_sha,
                "nonce_digest": hashlib.sha256(dispatch.nonce.encode()).hexdigest(),
            }
        )
    return result


async def native_draft(sandbox, effects, run_id, sha):
    drafts = [effect for effect in effects if effect.kind == "ensure_draft"]
    if len(drafts) != 1:
        raise ValueError("release run requires exactly one Draft intent")
    payload = known(drafts[0])
    result = TypeAdapter(DraftOutcome).validate_json(drafts[0].outcome_json)
    branch = "forge/" + run_id
    if (
        not isinstance(result, Applied)
        or result.value.repo.identity != sandbox.native.ref.identity
        or payload["head_branch"] != branch
        or await sandbox.native.review.find_draft(sandbox.native.ref, branch) != result.value
    ):
        raise ValueError("native Draft is not uniquely owned by this factory branch")
    if (
        await sandbox.native.review.review_head(result.value) != sha
        or await sandbox.native.repository.head(sandbox.native.ref, branch) != sha
    ):
        raise ValueError("native Draft/factory head moved from the published candidate")
    document = await sandbox.native.review.document(result.value)
    if (sandbox.connection.provider == "github" and document.get("merged") is not False) or (
        sandbox.connection.provider == "gitlab" and document.get("merged_at") is not None
    ):
        raise ValueError("release Draft was merged")
    return result.value


async def verification(runtime, sandbox, row, frozen, *, failed=False):
    document = json.loads(row.validation_json)
    saved = CheckVerdict.model_validate_json(json.dumps(document["verification"]))
    checks = await sandbox.native.ci.checks_for(sandbox.native.ref, row.published_sha)
    current = verdict(
        checks,
        row.published_sha,
        frozen.spec.verification.required_checks,
        now=runtime.budget.clock.now(),
        max_age_seconds=3600,
        missing_is_final=True,
    )
    if failed:
        historical = tuple(
            CheckResult.model_validate_json(json.dumps(value))
            for value in json.loads(saved.evidence)["checks"]
        )
        if (
            saved.status != "failed"
            or current.status != "failed"
            or not code_failure(historical, saved)
            or not code_failure(checks, current)
        ):
            raise ValueError("repair was not admitted by native code-defect evidence")
        jobs = [
            check_metadata(check)
            for check in checks
            if check.name == "release-smoke-oracle"
            and check.status == "failed"
            and check.failure_kind == "code"
        ]
        if not jobs:
            raise ValueError("repair has no exact failed native independent oracle")
        return jobs
    if not saved.verified_for(
        row.published_sha,
        frozen.spec.verification.required_checks,
        now=runtime.budget.clock.now(),
        max_age_seconds=3600,
    ) or not current.verified_for(
        row.published_sha,
        frozen.spec.verification.required_checks,
        now=runtime.budget.clock.now(),
        max_age_seconds=3600,
    ):
        raise ValueError("release candidate lacks current independent required native CI")
    return [
        check.native_id
        for check in checks
        if check.name == "release-smoke-oracle"
        and check.role == "verification"
        and check.status == "passed"
    ]


async def closing(sandbox, effects, row, review):
    saved = ReviewVerdict.model_validate_json(json.dumps(json.loads(row.validation_json)["review"]))
    comments = [effect for effect in effects if effect.kind == "post_review"]
    if (
        saved.decision != "approve"
        or saved.candidate_sha != row.published_sha
        or len(comments) != 1
    ):
        raise ValueError("release requires one bounded closing review of this exact candidate")
    payload = known(comments[0])
    if json.loads(payload["body_json"])["candidate_sha"] != row.published_sha or payload[
        "review"
    ] != review.model_dump(mode="json"):
        raise ValueError("closing native comment differs from its candidate receipt")
    found = await sandbox.native.review.probe_review(review, payload["marker"])
    original = TypeAdapter(CommentOutcome).validate_json(comments[0].outcome_json)
    if (
        not isinstance(found, Found)
        or not isinstance(found.value, Applied)
        or not isinstance(original, Applied)
        or found.value.value != original.value
    ):
        raise ValueError("closing review is not present as the exact native bot COMMENT")


async def scenario_receipt(runtime, sandbox, run_id, issue, scenario, base):
    async with runtime.factory() as session:
        run = await session.get(EngineRun, run_id)
        if (
            run is None
            or run.state != "ready_for_human"
            or run.cancel_requested
            or (run.provider, run.connection_id, run.repository_id, run.subject_id)
            != (
                sandbox.connection.provider,
                sandbox.connection.key,
                sandbox.repository.native_id,
                str(issue),
            )
        ):
            raise ValueError("release SQL run is not ready on the exact native issue")
        attempt, generation = run.attempt, run.generation
    expected = 1 if scenario == "happy" else 2
    if (attempt, generation) != (expected, expected):
        raise ValueError("release scenario did not execute its required bounded attempts")
    frozen = await approval(runtime, sandbox, run_id, base)
    effects = await rows(runtime, EngineEffect, run_id)
    for effect in effects:
        known(effect)
    candidates = sorted(await rows(runtime, EngineCandidate, run_id), key=lambda item: item.attempt)
    if len(candidates) != expected:
        raise ValueError("release scenario lacks the exact accepted immutable candidates")
    proofs = []
    for row in candidates:
        proofs.append(
            await candidate(
                runtime, sandbox, row, effects, frozen, base if not proofs else proofs[-1]["sha"]
            )
        )
    draft = await native_draft(sandbox, effects, run_id, proofs[-1]["sha"])
    jobs = await verification(runtime, sandbox, candidates[-1], frozen)
    await closing(sandbox, effects, candidates[-1], draft)
    implementation = await sandbox.native.repository.read_blob(
        sandbox.native.ref, proofs[-1]["sha"], IMPLEMENTATION
    )
    literal_return(implementation, 42)
    result = {
        "scenario": scenario,
        "run_id": run_id,
        "issue": issue,
        "candidate_sha": proofs[-1]["sha"],
        "oracle_jobs": jobs,
        "attempt": attempt,
        "generation": generation,
        "draft": {"number": draft.number, "head_branch": "forge/" + run_id},
        "candidates": proofs,
        "lane_dispatches": await dispatches(runtime, sandbox, run_id, effects, proofs, frozen),
        "implementation_return": 42,
    }
    if scenario == "repair":
        literal_return(
            await sandbox.native.repository.read_blob(
                sandbox.native.ref, proofs[0]["sha"], IMPLEMENTATION
            ),
            41,
        )
        result["initial_failure"] = {
            "candidate_sha": proofs[0]["sha"],
            "native_jobs": await verification(runtime, sandbox, candidates[0], frozen, failed=True),
            "implementation_return": 41,
        }
    pools = await rows(runtime, EngineBudgetPool, run_id)
    if {pool.purpose for pool in pools} != {"implementation", "closing"}:
        raise ValueError("release lacks its independent bounded budget pools")
    result["budgets"] = {
        pool.purpose: Pool.model_validate_json(pool.state_json).balance.model_dump(mode="json")
        for pool in pools
    }
    return result
