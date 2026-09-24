"""The GitLab CE production-entry trace (issue #268 / R36-09, review AT-10).

Forge began as a GitLab CE product; until now the strongest entry traces
were GitHub-shaped. This module is the GitLab-shaped production-entry
trace at the SAME discipline as the GitHub PEs: the REAL
:class:`forge.runs.service.RunService` (the GitLab lane) whose every
provider I/O travels REAL HTTP from the REAL
:class:`forge.gitlab.client.GitLabClient` to the fake native server's
GITLAB mode — a separate process whose pipelines, dispatch ledger with
VARIABLES, branches, MRs and issue notes survive worker death by
construction.

Traces (AT-10 → CE-1..CE-4):

- **CE-1** — the native entry: a seeded issue, `/implement` through the
  real service (the webhook's exact call), the evidence-backed plan
  comment ON THE NATIVE SERVER (digest + gate + the frozen Implementation
  block), then the approver's `/go` — whose ci_harness dispatch is
  recorded by the NATIVE SERVER as a real pipeline with its FORGE_*
  variables and its ``forge-agent-claude-code`` job.
- **CE-2 (the main arc)** — the controlled RUNNER LOSS: the first
  runner's lane (a REAL subprocess) pauses mid-turn with a REAL
  capture+upload of its WIP over HTTP; the CI job is CANCELLED on the
  native server; a RESTARTED worker (fresh session factory, same rows)
  classifies the loss (blocked, zero forge-side model calls — no repair);
  the operator's `/retry` — admitted BECAUSE the durable checkpoint
  exists — re-dispatches on a second runner; the resumed lane (REAL
  subprocess, required restore) rebuilds the exact generation; the
  SHIPPED collector captures the generation's diff; the artifacts upload
  natively; the reconciler downloads them over HTTP, publishes through
  the trusted publisher (a REAL commit on the native API) and opens the
  Draft MR. Verification: a green pipeline on a STALE sha never
  verifies the run; the pipeline on the CURRENT candidate does — and the
  MR stays a Draft (merge is human).
- **CE-3** — the old attempt's late callback (its stale pipeline handle,
  its job now claiming success) CANNOT publish after the resume: the
  candidate is recorded as superseded with zero native writes.
- **CE-4** — a failed REQUIRED restoration starts no model turn: a
  rotted checkpoint blob halts the resumed lane before the vendor exists,
  with zero publication.

Honesty note (what this trace does NOT claim): the GitLab dispatch seam
(``CITharnessBackend.start``) does not yet carry the lane-resume contract
(``FORGE_LANE_RESUME`` / lane-control credentials) the GitHub lane
dispatches (R32-04) — so CE-2 drives the RESUMED runner's lane
subprocess directly with the resume environment the CI job will receive
when that parity lands, exactly like the provider-neutral PE-1 trace
does. Everything the control plane owns (checkpoint authority, resume
command, resume-spec) is REAL here.

Assertions are on ARTIFACTS — the native server's dispatch ledger and
notes, branch heads, MR documents, DB row identities, file bytes — never
on status strings alone.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from forge.adaptive.checkpoint_channel import work_scoped_token
from forge.durable import FlowRun, FlowStatus, LLMCall

from .conftest import (
    GL_BASE_BRANCH,
    GL_BASE_SHA,
    GL_ISSUE_DESC,
    GL_ISSUE_IID,
    GL_ISSUE_TITLE,
    GL_PROJECT_ID,
    GL_REQUIRED_JOB,
    PE_HARNESS_MODEL,
    PE_LANE_SECRET,
    PE_LEGACY_DEADLINE,
    make_gitlab_service,
    start_control_plane,
)
from .test_production_entry import (
    create_control_schema,
    make_checkout,
    record_resume_command,
    run_collector,
    run_lane,
    run_vendor_once,
    upload_wip_checkpoint,
)

pytestmark = pytest.mark.production_entry

#: The first runner's pre-pause WIP (the pause captures exactly this).
FIRST_RUNNER_ACTIONS = [
    {"op": "write", "path": "src/app.py", "content": "print('paused wip')\n"},
    {"op": "write", "path": "notes/new-file.md", "content": "first-runner edit\n"},
    {"op": "delete", "path": "run.sh"},
]

#: The resumed turn's own edits ON TOP of the restored WIP.
RESUMED_TURN_ACTIONS = [
    {"op": "write", "path": "src/app.py", "content": "print('resumed turn')\n"},
    {"op": "write", "path": "notes/resumed.md", "content": "second-runner edit\n"},
]


async def get_run(session_factory, run_id: str) -> FlowRun:
    async with session_factory() as session:
        return await session.get(FlowRun, run_id)


async def llm_calls(session_factory, run_id: str) -> list[LLMCall]:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LLMCall).where(LLMCall.flow_run_id == run_id).order_by(LLMCall.id)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


def _clone(source: Path, target: Path) -> None:
    """A fresh clone of the same base — exactly what a second runner
    checks out (same commit oid, independent working copy)."""
    subprocess.run(
        ["git", "clone", "-q", str(source), str(target)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    exclude = target / ".git" / "info" / "exclude"
    exclude.write_text(
        exclude.read_text() + "\n.forge/\n.codegraph/\n__pycache__/\n*.pyc\n.venv/\nforge-output/\n"
    )


def _compose_meta(attempt_base: str) -> bytes:
    """``.forge/candidate.meta.json`` — the exact contract the CI lane's
    template uploads beside the candidate diff (ADR-0016 §1)."""
    return json.dumps(
        {
            "attempt_base": attempt_base,
            "driver": "claude-code",
            "model": PE_HARNESS_MODEL,
            "exit": "completed",
            "usage": None,
        }
    ).encode("utf-8")


# ----------------------------------------------------------------------
# CE-1 (AT-10) — native issue → evidence-backed plan → approved dispatch
# ----------------------------------------------------------------------


class TestCE1NativeIssueToApprovedDispatch:
    async def test_plan_gate_and_harness_dispatch_are_native_artifacts(
        self, pe_db, gitlab_native, gitlab_client
    ):
        factory = pe_db.worker_factory()
        service = make_gitlab_service(factory, gitlab_client)
        gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)

        run_id = await service.start_run(
            GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
        )

        # The plan comment landed NATIVELY and carries the evidence-backed
        # gate: the digest, the /go instruction, the approver, and the
        # frozen Implementation block (the approved execution shape).
        (plan_body,) = [body for body in gitlab_native.notes() if "/go" in body]
        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert run.plan_digest and run.plan_digest in plan_body
        assert f"`@forge /go {run_id}`" in plan_body
        assert "@alice" in plan_body
        assert "## Implementation" in plan_body
        assert f"- Harness: **claude-code** · model {PE_HARNESS_MODEL}" in plan_body
        # The plan summary is folded into the run's evidence (AT-10's
        # "evidence-backed plan" is a durable artifact, not comment text).
        plan_evidence = (run.evidence or {}).get("plan") or {}
        assert plan_evidence["digest"] == run.plan_digest
        assert plan_evidence["summary"]

        # The approver's /go consumes the gate and dispatches the harness:
        # ONE native pipeline on the factory branch, its FORGE_* variables
        # (the frozen RunSpec's execution shape) recorded by the server,
        # and its forge-agent job minted RUNNING.
        await service.handle_command_note(
            GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
        )

        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        branch = f"factory/{GL_ISSUE_IID}/{run_id[:8]}"
        (dispatch,) = gitlab_native.dispatches()
        assert dispatch["ref"] == branch
        variables = {v["key"]: v["value"] for v in dispatch["variables"]}
        assert variables["FORGE_RUN_ID"] == run_id
        assert variables["FORGE_ISSUE_IID"] == str(GL_ISSUE_IID)
        assert variables["FORGE_ISSUE_TITLE"] == GL_ISSUE_TITLE
        assert variables["FORGE_HARNESS_MODEL"] == PE_HARNESS_MODEL
        assert variables["FORGE_HARNESS_DRIVER"] == "claude-code"
        assert variables["FORGE_ATTEMPT_BASE"] == run.base_sha == GL_BASE_SHA
        assert "Implementation plan" in variables["FORGE_PLAN"]

        (job,) = gitlab_native.jobs(dispatch["pipeline_id"])
        assert job["name"].startswith("forge-agent")
        assert job["status"] == "running"

        # The run's durable handle names the NATIVE identities (the
        # reconciler restarts from exactly these), and the operator was
        # answered natively (taken-into-work with the live pipeline link).
        harness = (run.evidence or {})["harness"]
        assert harness["pipeline_id"] == dispatch["pipeline_id"]
        assert harness["job_id"] == job["id"]
        assert harness["driver"] == "claude-code"
        assert any("taken into work" in body for body in gitlab_native.notes())
        # The REAL client never fell off the modeled API surface.
        assert gitlab_native.unknown_paths() == []


# ----------------------------------------------------------------------
# The shared CE lab: CE-2 (the main arc) and CE-3 (the old-attempt
# callback) assert independent facets of ONE expensive run.
# ----------------------------------------------------------------------


@pytest.fixture()
async def ce_lab(tmp_path: Path, pe_db, gitlab_native, gitlab_client, monkeypatch):
    """The full CE arc, driven once; every step through native surfaces."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    store_dir = tmp_path / "ce-store"
    monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(store_dir))
    monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
    monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ce-control.db'}"
    engine = create_async_engine(db_url)
    await create_control_schema(engine)
    control_factory = async_sessionmaker(engine, expire_on_commit=False)
    control = await start_control_plane(db_url)
    try:
        # The repo snapshot: a REAL git checkout at its frozen base, with
        # the same base oid aligned on the native server.
        original, base_oid = make_checkout(tmp_path, "ce-workspace")
        gitlab_native.seed_commit(GL_BASE_BRANCH, base_oid, "frozen base")
        for name in ("README.md", "src/app.py", "run.sh"):
            gitlab_native.seed_file(name, (original / name).read_text())
        gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)

        # --- issue → plan → approved dispatch (worker #1) ----------------
        factory_a = pe_db.worker_factory()
        service_a = make_gitlab_service(factory_a, gitlab_client)
        run_id = await service_a.start_run(
            GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
        )
        await service_a.handle_command_note(
            GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
        )
        (dispatch_1,) = gitlab_native.dispatches()
        branch = dispatch_1["ref"]
        job_1 = gitlab_native.jobs(dispatch_1["pipeline_id"])[0]
        assert job_1["status"] == "running"

        # --- the FIRST runner's lane: WIP, then the pause (a REAL
        # capture+upload over HTTP — the committed checkpoint) ------------
        eventlog = tmp_path / "ce-vendor-events.jsonl"
        run_vendor_once(original, FIRST_RUNNER_ACTIONS, eventlog)
        token = work_scoped_token(PE_LANE_SECRET, run_id)
        checkpoint_id = await upload_wip_checkpoint(
            control.base_url, original, run_id, token, base_oid
        )
        assert await record_resume_command(control_factory, run_id) is True

        # --- KILL the first runner: the CI job is cancelled ON THE NATIVE
        # SERVER (never by touching lab containers) ----------------------
        gitlab_native.cancel_job(job_1["id"])

        # --- the worker RESTARTS: a fresh session factory over the same
        # durable rows classifies the loss -------------------------------
        await pe_db.dispose()  # worker #1's engine is gone — its process died
        factory_b = pe_db.worker_factory()
        service_b = make_gitlab_service(factory_b, gitlab_client)
        await service_b.evaluate_waiting_harness()
        blocked = await get_run(factory_b, run_id)
        assert blocked.status == FlowStatus.BLOCKED.value
        assert "canceled" in (blocked.status_reason or "")
        # No forge-side model call ever ran: the harness lane's failure is
        # classified, never LLM-repaired (ADR-0015).
        assert await llm_calls(factory_b, run_id) == []

        # --- the operator's /retry: admitted BECAUSE the durable
        # checkpoint exists; a second runner is dispatched natively ------
        await service_b.handle_retry_note(
            GL_PROJECT_ID,
            f"@forge /retry {run_id}",
            "alice",
            GL_ISSUE_IID,
            delivery_id="ce-retry-1",
        )
        dispatch_2 = gitlab_native.dispatches()[1]
        assert dispatch_2["ref"] == branch  # the work continues in place
        job_2 = gitlab_native.jobs(dispatch_2["pipeline_id"])[0]
        assert job_2["status"] == "running"

        # --- the SECOND runner: a fresh clone of the same base, the REAL
        # lane subprocess with a REQUIRED restore ------------------------
        resumed = tmp_path / "ce-resumed"
        _clone(original, resumed)
        lane = run_lane(
            resumed,
            work_id=run_id,
            resume="1",
            control_url=control.base_url,
            token=token,
            attempt_base=base_oid,
            actions=RESUMED_TURN_ACTIONS,
            eventlog=eventlog,
        )
        assert lane.returncode == 0, lane.stderr

        # --- the SHIPPED collector captures the generation's diff; the CI
        # job uploads it as artifacts and goes green ---------------------
        outcome = run_collector(resumed, work_id=run_id, attempt_base=base_oid)
        assert outcome.returncode == 0, outcome.stderr
        reported = json.loads(outcome.stdout)
        diff = Path(reported["diff_path"]).read_bytes()
        gitlab_native.seed_artifact(job_2["id"], ".forge/candidate.diff", diff)
        gitlab_native.seed_artifact(
            job_2["id"], ".forge/candidate.meta.json", _compose_meta(base_oid)
        )
        gitlab_native.mark_job(job_2["id"], "success")

        # --- the reconciler adopts: download over HTTP, publish via the
        # trusted publisher, open the Draft MR ---------------------------
        await service_b.evaluate_waiting_harness()
        published = await get_run(factory_b, run_id)
        assert published.status == FlowStatus.WAITING_CI.value
        candidate_sha = published.candidate_shas[-1]

        # --- verification: a STALE green pipeline never verifies; the
        # CURRENT candidate's pipeline does ------------------------------
        gitlab_native.seed_pipeline(
            ref=branch,
            sha=base_oid,  # the pre-work base — stale by construction
            jobs=[{"name": GL_REQUIRED_JOB, "status": "success"}],
        )
        await service_b.evaluate_waiting_ci()
        assert (await get_run(factory_b, run_id)).status == FlowStatus.WAITING_CI.value

        current_pipeline = gitlab_native.seed_pipeline(
            ref=branch,
            sha=candidate_sha,
            jobs=[{"name": GL_REQUIRED_JOB, "status": "success"}],
        )
        await service_b.evaluate_waiting_ci()
        ready = await get_run(factory_b, run_id)

        yield SimpleNamespace(
            run_id=run_id,
            branch=branch,
            base_oid=base_oid,
            checkpoint_id=checkpoint_id,
            candidate_sha=candidate_sha,
            current_pipeline_id=current_pipeline["id"],
            diff=diff,
            blocked_reason=blocked.status_reason,
            factory=factory_b,
            service=service_b,
            dispatch_1=dispatch_1,
            dispatch_2=dispatch_2,
            job_1=job_1,
            job_2=job_2,
            eventlog=eventlog,
            ready_run=ready,
            original=original,
            resumed=resumed,
        )
    finally:
        control.stop()
        await engine.dispose()


class TestCE2RunnerLossResumesExactWipIntoDraftMR:
    async def test_the_resumed_generation_is_the_published_candidate(self, ce_lab, gitlab_native):
        """The Draft MR carries EXACTLY the restored WIP + the resumed
        turn — changed, new and deleted files from the active generation,
        published as ONE native commit with the right source identity."""
        lab = ce_lab
        # The candidate diff (what the artifacts carried, what the
        # publisher committed) is the FINAL generation state: the resumed
        # turn's edit of app.py, the RESTORED first-runner file, the
        # resumed turn's new file, and the first turn's deletion.
        assert b"print('resumed turn')" in lab.diff
        assert b"notes/new-file.md" in lab.diff and b"first-runner edit" in lab.diff
        assert b"notes/resumed.md" in lab.diff and b"second-runner edit" in lab.diff
        assert b"deleted file mode" in lab.diff and b"run.sh" in lab.diff
        assert b"print('paused wip')" not in lab.diff  # superseded in-generation, not additive

        # The native branch head IS the published candidate (the writer's
        # commit moved it), and the Draft MR is bound to the same identity.
        assert gitlab_native.branch_head(lab.branch) == lab.candidate_sha
        (mr,) = gitlab_native.merge_requests().values()
        assert mr["title"] == f"Draft: {GL_ISSUE_TITLE}"
        assert mr["source_branch"] == lab.branch
        assert mr["target_branch"] == GL_BASE_BRANCH
        assert lab.candidate_sha in mr["description"]
        run = lab.ready_run
        assert run.plan_digest in mr["description"]

        # The published evidence names the attempt base and the entry count.
        published = (run.evidence or {})["published_candidate"]
        assert published["sha"] == lab.candidate_sha
        assert published["attempt_base"] == lab.base_oid
        assert published["entries"] == 4  # changed + 2 new + deleted

        # The usage receipt: exactly ONE harness episode on the ledger.
        calls = await llm_calls(lab.factory, lab.run_id)
        assert [call.role for call in calls] == ["implementer"]
        assert calls[0].provider == "ci_harness"

    def test_the_pause_and_retry_are_native_and_durable(self, ce_lab, gitlab_native):
        """The runner loss is a NATIVE cancel; the /retry was admitted by
        the durable checkpoint; the second dispatch carried the bounded
        repair context — and no forge-side model call ever ran for the
        repair (harness lanes never enter the LLM repair loop)."""
        lab = ce_lab
        # The kill is recorded natively: the job was cancelled through the
        # CI API, the pipeline went canceled with it.
        assert [cancel["job_id"] for cancel in gitlab_native.job_cancels()] == [lab.job_1["id"]]
        # The /retry ack + the second dispatch both landed natively, on the
        # SAME branch, with the repair context in the brief.
        assert any("retried by @alice" in body for body in gitlab_native.notes())
        variables = {v["key"]: v["value"] for v in lab.dispatch_2["variables"]}
        assert variables["FORGE_RUN_ID"] == lab.run_id
        assert variables["FORGE_ATTEMPT_BASE"] == lab.base_oid
        assert "Repair context" in variables["FORGE_PLAN"]
        # The committed checkpoint is what the resume restored from: the
        # generation pointer in the second runner's checkout names it.
        assert lab.checkpoint_id
        pointer = json.loads((lab.resumed / ".forge" / "workspace-generation").read_text())
        assert pointer["work_id"] == lab.run_id
        assert pointer["checkpoint_id"] == lab.checkpoint_id

    def test_independent_pipeline_checks_the_current_candidate(self, ce_lab, gitlab_native):
        """The run went ready ONLY on the CURRENT candidate's pipeline: the
        verification evidence is bound to the candidate sha and the seeded
        current pipeline id — the stale base-sha pipeline (also green, also
        carrying the required job) never produced a verdict."""
        lab = ce_lab
        run = lab.ready_run
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "passed"
        assert verification["tested_oid"] == lab.candidate_sha
        assert verification["producer"] == "gitlab-pipeline"
        pipeline_evidence = (run.evidence or {})["pipeline"]
        assert pipeline_evidence["id"] == lab.current_pipeline_id
        assert pipeline_evidence["sha"] == lab.candidate_sha
        # The ready evidence comment reached the NATIVE issue, naming the
        # verified candidate and the (still-Draft) MR.
        evidence_notes = [body for body in gitlab_native.notes() if "ready" in body.lower()]
        assert evidence_notes and lab.candidate_sha[:8] in evidence_notes[-1]
        (mr,) = gitlab_native.merge_requests().values()
        assert mr["title"].startswith("Draft:")  # merge stays a human decision
        # The REAL client never fell off the modeled API surface.
        assert gitlab_native.unknown_paths() == []


class TestCE3OldAttemptCallbackCannotPublishAfterResume:
    async def test_stale_attempt_candidate_is_superseded_not_published(self, ce_lab, gitlab_native):
        """The FIRST runner's job comes back late claiming success with a
        well-formed candidate of its own. The stale attempt's callback
        (its journaled handle, polled by the dead worker's resurrected
        pass) CANNOT publish after the resume: the run is terminal, the
        candidate is recorded superseded, and the native surface shows
        zero new writes."""
        lab = ce_lab
        assert lab.ready_run.status == FlowStatus.READY_FOR_HUMAN.value
        head_before = gitlab_native.branch_head(lab.branch)
        mrs_before = dict(gitlab_native.merge_requests())

        # The OLD job now claims success — with artifacts whose attempt
        # base matches the CURRENT expectation (the strongest form of the
        # attack: everything about the artifact is well-formed; only the
        # attempt is dead).
        foreign_diff = (
            "diff --git a/notes/stale.md b/notes/stale.md\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/notes/stale.md\n"
            "@@ -0,0 +1 @@\n"
            "+stale attempt content\n"
        ).encode("utf-8")
        gitlab_native.seed_artifact(lab.job_1["id"], ".forge/candidate.diff", foreign_diff)
        gitlab_native.seed_artifact(
            lab.job_1["id"], ".forge/candidate.meta.json", _compose_meta(lab.candidate_sha)
        )
        gitlab_native.mark_job(lab.job_1["id"], "success")

        # The dead worker's resurrected pass: the OLD handle is still what
        # it journaled at the first dispatch. Restore it and poll once.
        stale_handle = json.dumps(
            {
                "harness": "claude-code",
                "pipeline_id": lab.dispatch_1["pipeline_id"],
                "job_id": lab.job_1["id"],
                "branch": lab.branch,
                "base_sha": lab.base_oid,
                "attempt_base": lab.base_oid,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        async with lab.factory() as session:
            run = await session.get(FlowRun, lab.run_id)
            evidence = dict(run.evidence or {})
            harness = dict(evidence["harness"])
            harness["handle"] = stale_handle
            evidence["harness"] = harness
            run.evidence = evidence
            await session.commit()

        await lab.service._evaluate_harness_one(lab.run_id, datetime.now(timezone.utc))

        run = await get_run(lab.factory, lab.run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value  # untouched
        superseded = (run.evidence or {})["superseded"]
        assert "ready_for_human" in superseded["reason"]
        # ZERO native writes: no second commit, no MR change.
        assert gitlab_native.branch_head(lab.branch) == head_before
        assert gitlab_native.merge_requests() == mrs_before
        # The stale content never landed anywhere.
        assert "stale attempt content" not in json.dumps(gitlab_native.state()["files"])


# ----------------------------------------------------------------------
# CE-4 (AT-10) — failed required restoration starts no model turn
# ----------------------------------------------------------------------


class TestCE4RequiredRestorationFailureStartsNoModelTurn:
    async def test_rotted_checkpoint_halts_the_resumed_lane_before_the_vendor(
        self, tmp_path: Path, monkeypatch, gitlab_native
    ):
        """The pause committed a checkpoint; a blob rots at the authority.
        The second runner's REQUIRED restore fails loudly: the vendor is
        never spawned (zero model turns), nothing is published, no
        generation is created."""
        import httpx
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        store_dir = tmp_path / "ce-store"
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(store_dir))
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'ce-control.db'}"
        engine = create_async_engine(db_url)
        await create_control_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        control = await start_control_plane(db_url)
        try:
            checkout, base_oid = make_checkout(tmp_path, "ce-workspace")
            eventlog = tmp_path / "ce-vendor-events.jsonl"
            run_vendor_once(checkout, FIRST_RUNNER_ACTIONS, eventlog)
            work_id = "ce-run-rot"
            token = work_scoped_token(PE_LANE_SECRET, work_id)
            await upload_wip_checkpoint(control.base_url, checkout, work_id, token, base_oid)
            assert await record_resume_command(factory, work_id) is True

            # ROT one referenced blob at the authority.
            served = httpx.get(
                control.url(f"/lane/checkpoints/{work_id}"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            ).json()
            first_blob = next(iter(served["blobs"]))
            blob_path = store_dir / first_blob[:2] / first_blob
            assert blob_path.is_file()
            blob_path.write_bytes(b"rotted bytes - not the addressed content\n")

            resumed = tmp_path / "ce-resumed"
            _clone(checkout, resumed)
            lane = run_lane(
                resumed,
                work_id=work_id,
                resume="1",
                control_url=control.base_url,
                token=token,
                attempt_base=base_oid,
                actions=[{"op": "write", "path": "evil.txt", "content": "must never happen\n"}],
                eventlog=eventlog,
            )
            assert lane.returncode != 0
            assert "wip_restore_failed" in lane.stderr

            # ZERO model turns: no vendor event follows the pause leg.
            kinds = [
                json.loads(line)["kind"]
                for line in eventlog.read_text().splitlines()
                if line.strip()
            ]
            assert kinds, "the pre-pause vendor leg must be on record"
            assert kinds[kinds.index("vendor_edits") + 1 :] == []
            # ZERO publication and no generation.
            assert not (resumed / "forge-output").exists()
            assert not (resumed / "evil.txt").exists()
            # And nothing was ever dispatched to the native surface.
            assert gitlab_native.dispatches() == []
        finally:
            control.stop()
            await engine.dispose()
