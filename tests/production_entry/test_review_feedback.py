"""The Q39-13 / #332 production-entry traces — reviewer feedback as a
bounded revision on the CURRENT candidate.

The service-level suite (``tests/test_review_feedback.py``) proves the
rules; these traces prove the flow at the level a customer drives — the
REAL :class:`forge.runs.service.RunService` whose every provider I/O
travels REAL HTTP from the REAL :class:`forge.gitlab.client.GitLabClient`
to the fake native server's GITLAB mode, real durable databases, the
REAL note-command dispatch (``execute_run_command`` — the worker's own
executor for a ``run_command`` payload) and the REAL ``/approve-revision``
ingress (:class:`forge.adaptive.command_router.ControlCommandRouter`):

- **RF-1 (the review's exact scenario)** — issue → /go → the first
  runner's lane edits + the shipped collector → the artifacts upload
  natively → the reconciler publishes the candidate and opens the Draft
  MR (``waiting_ci``) → the reviewer's ``/fix`` comment ON THE DRAFT MR
  through the REAL note dispatch → ONE durable request bound to the
  CURRENT MR head → the human approves through the REAL
  ``/approve-revision`` → the correction re-dispatch carries the
  reviewer's text: the dispatched ``FORGE_PLAN`` BEGINS with the active
  revision's rendered brief (the correction + the head binding + the
  referenced diff path), ``FORGE_PLAN_DIGEST`` equals the correction
  revision's canonical digest, the brief envelope verifies over the
  ledger's own bytes, and the three-way executor digest holds (evidence
  == a recomputation from the native ledger's recorded variables == the
  consumed bytes). The MR stays a Draft — the bot never merges and never
  resolves anything.
- **RF-2 (the typed negative)** — a HUMAN EDIT lands between request and
  publication (the factory branch moves): the re-dispatch refuses the
  typed ``stale_head`` conflict, the request is durably marked, the
  operator note explains, and NO second pipeline is ever minted.

Honest seams (disclosed, the #313/#321 precedent): the revision WORLD
(revision 1's pointer) is staged through the app's own durable shape —
the live GitLab planner does not emit plan revisions; the fake native
server has no ``/discussions`` route, so the auxiliary discussions
surface degrades exactly as designed (ONE recorded unknown path, the
typed deleted-discussion check skipped, logged) — the resolution
accounting is proven at the service level where the surface is faked.
Everything the control plane owns is REAL here.

This module is env-clean once: the module-scoped scrub removes every
FORGE_*/GITLAB_*/GITHUB_* variable before the traces and restores them
after — nothing below depends on the surrounding developer environment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from forge.adaptive.revisions import (
    APPROVED_INPUT_KEY,
    REVISION_CONTENT_KEY,
    REVISION_EXECUTOR_DIGEST_KEY,
    ReviewFeedbackRefused,
    executor_input_digest,
    plan_digest,
    review_feedback_requests_of,
)
from forge.durable import FlowRun, FlowStatus
from forge.harnesses.brief_envelope import verify_brief_envelope

from .conftest import (
    GL_BASE_BRANCH,
    GL_ISSUE_DESC,
    GL_ISSUE_IID,
    GL_ISSUE_TITLE,
    GL_PROJECT_ID,
    PE_HARNESS_MODEL,
    PE_LANE_SECRET,
    PE_LEGACY_DEADLINE,
    gl_settings,
    make_gitlab_service,
)
from .test_production_entry import (
    make_checkout,
    run_collector,
    run_vendor_once,
)

pytestmark = pytest.mark.production_entry

#: The first runner's work: the rename the reviewer will CORRECT — the
#: entrypoint is checked in as `check`, the reviewer names the fix.
RUNNER_ACTIONS = [
    {"op": "write", "path": "src/app.py", "content": "def check(email):\n    return bool(email)\n"},
]
#: The reviewer's bounded correction, in scope (``src/**``).
FIX_NOTE = "/fix rename the helper in `src/app.py` to validate_email"

NOTE_SEQ = 8200
D_CONTRACT = "3" * 64
D_SNAPSHOT = "5" * 64


@pytest.fixture(autouse=True, scope="module")
def _env_clean_once():
    """Scrub the provider/forge environment ONCE for the whole module."""
    prefixes = ("FORGE_", "GITLAB_", "GITHUB_")
    saved = {key: value for key, value in os.environ.items() if key.startswith(prefixes)}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


async def get_run(factory, run_id: str) -> FlowRun:
    async with factory() as session:
        return await session.get(FlowRun, run_id)


def _step(step_id: str, objective: str, *, writes: str | None = None):
    from forge.adaptive.models import PlanStep

    return PlanStep(
        step_id=step_id,
        objective=objective,
        write_repository_id=writes,
        impact=["internal"],
        acceptance_refs=["AC-1"],
    )


def _revision(revision: int, parent: int | None, *, summary: str):
    from forge.adaptive.models import PlanRevision

    return PlanRevision(
        plan_id="plan-rf-pe",
        work_id="wp-rf-pe",
        revision=revision,
        parent_revision=parent,
        work_contract_digest=D_CONTRACT,
        snapshot_set_digest=D_SNAPSHOT,
        summary=summary,
        steps=[
            _step("S1", "Inspect the existing entrypoint."),
            _step("S2", "Implement the entrypoint.", writes="forge-pe/src"),
            _step("S3", "Check the result."),
        ],
    )


def _compose_meta(attempt_base: str) -> bytes:
    return json.dumps(
        {
            "attempt_base": attempt_base,
            "driver": "claude-code",
            "model": PE_HARNESS_MODEL,
            "exit": "completed",
            "usage": None,
        }
    ).encode("utf-8")


async def _seed_active_revision_one(factory, run_id: str) -> None:
    """Revision 1 becomes the durable ACTIVE plan (the #321 disclosure)."""
    first = _revision(1, None, summary="Land the validator entrypoint (check).")
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        evidence = dict(run.evidence or {})
        evidence["active_plan"] = {
            "schema": "forge.revision.active-plan/1",
            "work_id": first.work_id,
            "plan_id": first.plan_id,
            "active_revision": 1,
            "work_contract_digest": D_CONTRACT,
            "authorization_epoch": 4,
            "publication_epoch": 1,
            "plan_digest": plan_digest(first),
            "revised_from_digest": "",
            "activated_by_decision": "rd-rf-one",
            REVISION_CONTENT_KEY: first.model_dump(),
        }
        run.evidence = evidence
        await session.commit()


async def _deliver_review_note(gitlab_native, factory, gitlab_client, mr_iid: int, note_id: int):
    """The reviewer's MR note through the REAL run-command dispatch.

    ``execute_run_command`` is the worker's own executor for a
    ``run_command`` payload — it builds the REAL GitLab client from the
    settings and the REAL RunService, exactly as the gateway's scheduled
    step would.
    """
    from forge.config import ForgeConfig
    from forge.runs.service import execute_run_command

    settings = gl_settings(
        GITLAB_URL=gitlab_native.base_url,
        GITLAB_TOKEN="pe-gitlab-native-token",  # noqa: S106 — fixture value
    )
    await execute_run_command(
        settings,
        ForgeConfig(),
        factory,
        {
            "command": "review_feedback",
            "project_id": GL_PROJECT_ID,
            "issue_iid": GL_ISSUE_IID,
            "mr_iid": mr_iid,
            "note_id": str(note_id),
            "discussion_id": f"d-{note_id}",
            "note_text": FIX_NOTE,
            "author_username": "alice",
        },
    )


async def _approve_via_router(gitlab_client, factory, run_id: str, decision_id: str):
    """The REAL /approve-revision ingress, posting over real HTTP."""
    from forge.adaptive.command_router import ControlCommandRouter

    async def post_note(body: str):
        return await gitlab_client.create_issue_note(GL_PROJECT_ID, GL_ISSUE_IID, body)

    router = ControlCommandRouter(
        session_factory=factory, settings=gl_settings(), post_note=post_note
    )
    return await router.handle(
        {
            "command": "adaptive_control",
            "provider": "gitlab",
            "adaptive_verb": "approve-revision",
            "project_id": GL_PROJECT_ID,
            "issue_iid": GL_ISSUE_IID,
            "author_username": "alice",
            "note_text": f"/approve-revision {run_id} {decision_id}",
            "note_id": NOTE_SEQ + 900,
        }
    )


def _dispatch_variables(gitlab_native, index: int) -> dict:
    entry = gitlab_native.dispatches()[index]
    return {variable["key"]: variable["value"] for variable in entry["variables"]}


async def _publish_first_candidate(
    pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
) -> tuple[str, str, int]:
    """Issue → /go → the lane's work → the native publish (waiting_ci).

    Returns ``(run_id, branch, mr_iid)`` with the Draft MR open on the
    fake native server and the candidate committed on the branch.
    """
    store_dir = tmp_path / "rf-store"
    monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(store_dir))
    monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
    monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)

    factory = pe_db.worker_factory()
    service = make_gitlab_service(factory, gitlab_client)
    gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
    checkout, base_oid = make_checkout(tmp_path, "rf-workspace")
    gitlab_native.seed_commit(GL_BASE_BRANCH, base_oid, "frozen base")
    for name in ("README.md", "src/app.py", "run.sh"):
        gitlab_native.seed_file(name, (checkout / name).read_text())
    # The approved work scope the classification checks against.
    gitlab_native.seed_file(".forge.yml", "implement:\n  paths:\n    - src/**\n")

    run_id = await service.start_run(
        GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
    )
    await service.handle_command_note(
        GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
    )
    (dispatch_1,) = gitlab_native.dispatches()
    branch = dispatch_1["ref"]
    job_1 = gitlab_native.jobs(dispatch_1["pipeline_id"])[0]

    # The runner's lane: the work, the SHIPPED collector, the native
    # artifacts, the green job.
    run_vendor_once(checkout, RUNNER_ACTIONS, tmp_path / "rf-vendor-events.jsonl")
    outcome = run_collector(checkout, work_id=run_id, attempt_base=base_oid)
    assert outcome.returncode == 0, outcome.stderr
    reported = json.loads(outcome.stdout)
    diff = Path(reported["diff_path"]).read_bytes()
    gitlab_native.seed_artifact(job_1["id"], ".forge/candidate.diff", diff)
    gitlab_native.seed_artifact(job_1["id"], ".forge/candidate.meta.json", _compose_meta(base_oid))
    gitlab_native.mark_job(job_1["id"], "success")

    await service.evaluate_waiting_harness()
    published = await get_run(factory, run_id)
    assert published.status == FlowStatus.WAITING_CI.value
    assert published.mr_iid is not None
    return run_id, branch, published.mr_iid


class TestRF1TheReviewerCorrectionFlow:
    async def test_the_correction_becomes_the_next_dispatches_brief(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
    ):
        run_id, branch, mr_iid = await _publish_first_candidate(
            pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
        )
        factory = pe_db.worker_factory()
        service = make_gitlab_service(factory, gitlab_client)
        candidate_sha = (await get_run(factory, run_id)).candidate_shas[-1]
        await _seed_active_revision_one(factory, run_id)

        # --- the reviewer's comment ON THE DRAFT MR, through the REAL
        #     note dispatch --------------------------------------------
        dispatches_before = len(gitlab_native.dispatches())
        await _deliver_review_note(gitlab_native, factory, gitlab_client, mr_iid, note_id=NOTE_SEQ)
        run = await get_run(factory, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        [request] = requests.values()
        assert request.classification == "in-scope_correction"
        assert request.status == "staged"
        assert request.head_sha == candidate_sha  # bound to the CURRENT head
        assert request.referenced_paths == ("src/app.py",)
        # ONE durable request + exactly one operator reply on the MR
        assert list(requests) == [str(NOTE_SEQ)]
        mr_notes = [entry["body"] for entry in gitlab_native.state()["mr_notes"]]
        replies = [body for body in mr_notes if "/approve-revision" in body]
        assert len(replies) == 1
        # a replayed delivery records nothing new and replies nothing new
        await _deliver_review_note(gitlab_native, factory, gitlab_client, mr_iid, note_id=NOTE_SEQ)
        assert list(
            review_feedback_requests_of((await get_run(factory, run_id)).evidence or {})
        ) == [str(NOTE_SEQ)]
        mr_notes_after = [entry["body"] for entry in gitlab_native.state()["mr_notes"]]
        assert len([b for b in mr_notes_after if "/approve-revision" in b]) == 1

        # --- the human approval through the REAL ingress ---------------
        approved = await _approve_via_router(gitlab_client, factory, run_id, request.decision_id)
        assert approved["status"] == "applied", approved

        # --- the correction re-dispatch --------------------------------
        await service.evaluate_review_corrections()
        assert len(gitlab_native.dispatches()) == dispatches_before + 1
        corrected = _dispatch_variables(gitlab_native, -1)
        run = await get_run(factory, run_id)

        # THE #321 DIGEST PROOF: the dispatched FORGE_PLAN carries the
        # ACTIVE revision's TEXT — the reviewer's correction, the head
        # binding and the referenced diff path — never an obsolete source.
        assert "validate_email" in corrected["FORGE_PLAN"]
        assert "Reviewer correction" in corrected["FORGE_PLAN"]
        assert candidate_sha[:12] in corrected["FORGE_PLAN"]
        assert "src/app.py" in corrected["FORGE_PLAN"]
        # the superseded SPEC brief is history: dispatch 2 carries the
        # corrected revision's text, not dispatch 1's bytes
        first_variables = _dispatch_variables(gitlab_native, 0)
        assert corrected["FORGE_PLAN"] != first_variables["FORGE_PLAN"]
        evidence = run.evidence or {}
        approved_input = evidence[APPROVED_INPUT_KEY]
        assert approved_input["source"] == "revision"
        assert corrected["FORGE_PLAN"].startswith(approved_input["plan_text"])
        assert corrected["FORGE_PLAN_DIGEST"] == approved_input["plan_digest"]
        from forge.adaptive.models import PlanRevision

        active_content = PlanRevision.model_validate(evidence["active_plan"][REVISION_CONTENT_KEY])
        assert corrected["FORGE_PLAN_DIGEST"] == plan_digest(active_content)
        assert "Reviewer correction" in active_content.summary
        assert evidence["active_plan"]["activated_by_decision"] == request.decision_id

        # the brief envelope verifies over the LEDGER's own recorded bytes
        verify_brief_envelope(
            corrected["FORGE_BRIEF_ENVELOPE_DIGEST"],
            run_id=run_id,
            task_title=GL_ISSUE_TITLE,
            task_description=GL_ISSUE_DESC,
            plan_text=corrected["FORGE_PLAN"],
            spec_digest=corrected["FORGE_SPEC_DIGEST"],
        )
        # the THREE-WAY executor digest: evidence == the native ledger ==
        # the recomputation from the recorded variables
        identity = {
            "run_id": corrected["FORGE_RUN_ID"],
            "plan_digest": corrected["FORGE_PLAN_DIGEST"],
            "envelope_digest": corrected["FORGE_BRIEF_ENVELOPE_DIGEST"],
            "spec_digest": corrected["FORGE_SPEC_DIGEST"],
            "lane_resume_mode": corrected["FORGE_LANE_RESUME_MODE"],
        }
        executor_document = evidence[REVISION_EXECUTOR_DIGEST_KEY]
        assert executor_document["executor_input_digest"] == executor_input_digest(identity)

        # the request lifecycle closed; the lane re-dispatched in place
        assert review_feedback_requests_of(evidence)[str(NOTE_SEQ)].status == "dispatched"
        assert gitlab_native.dispatches()[-1]["ref"] == branch
        assert run.status == FlowStatus.WAITING_HARNESS.value

        # the bot NEVER merges and NEVER resolves: the MR stays a Draft
        state = gitlab_native.state()
        mr = state["merge_requests"][str(mr_iid)]
        assert mr["state"] == "opened" and mr["title"].startswith("Draft:")
        assert run.status != "ready_for_human"  # readiness waits for the checks

        # the auxiliary discussions surface degraded EXACTLY once per
        # delivery (the fake native has no /discussions route; each fresh
        # worker process marks the surface down on its first read) —
        # disclosed, not silent
        discussions_reads = [
            path for path in gitlab_native.unknown_paths() if "discussions" in path
        ]
        assert len(discussions_reads) == 2  # one per delivered note
        assert [p for p in gitlab_native.unknown_paths() if "discussions" not in p] == []


class TestRF2TheTypedStaleHeadConflict:
    async def test_a_human_edit_between_request_and_publication_conflicts_typed(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
    ):
        run_id, branch, mr_iid = await _publish_first_candidate(
            pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
        )
        factory = pe_db.worker_factory()
        service = make_gitlab_service(factory, gitlab_client)
        await _seed_active_revision_one(factory, run_id)
        await _deliver_review_note(
            gitlab_native, factory, gitlab_client, mr_iid, note_id=NOTE_SEQ + 10
        )
        request = review_feedback_requests_of((await get_run(factory, run_id)).evidence or {})[
            str(NOTE_SEQ + 10)
        ]
        assert (await _approve_via_router(gitlab_client, factory, run_id, request.decision_id))[
            "status"
        ] == "applied"

        # a HUMAN EDIT lands between request and publication: the factory
        # branch moves past the reviewed candidate.
        gitlab_native.seed_commit(branch, "e" * 40, "human edit on the branch")
        dispatches_before = len(gitlab_native.dispatches())
        await service.evaluate_review_corrections()

        run = await get_run(factory, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests[str(NOTE_SEQ + 10)].status == "stale_head"
        # nothing was dispatched — human edits are preserved, never
        # force-overwritten
        assert len(gitlab_native.dispatches()) == dispatches_before
        assert run.status == FlowStatus.WAITING_CI.value
        conflict_notes = [entry["body"] for entry in gitlab_native.state()["mr_notes"]]
        assert any("stale_head" in body for body in conflict_notes)
        # the typed refusal is the domain's own vocabulary
        from forge.adaptive.revisions import head_binding_guard

        try:
            head_binding_guard(request.head_sha, "e" * 40)
        except ReviewFeedbackRefused as exc:
            assert exc.code == "stale_head"
        else:  # pragma: no cover — the guard must refuse
            pytest.fail("the head binding guard accepted a moved head")
