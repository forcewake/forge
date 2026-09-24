"""The GitLab dispatch-envelope parity trace (issue #288 / R37-07).

The recorded gap this module closes: the GitLab ``ci_harness`` dispatch
(``CITharnessBackend.start``) did not carry the lane-resume / lane-control
contract the GitHub lane dispatches (R32-04) — the CE-2 trace had to drive
the resumed runner's lane subprocess with the resume environment the CI
job WOULD receive "once the parity lands". It has landed: every pipeline
the REAL :class:`forge.runs.service.RunService` dispatches now carries the
envelope, recorded by the fake native server's dispatch ledger with its
VARIABLES.

Discipline (same as the sibling CE traces): the REAL GitLabClient over
real HTTP to the fake native server's GitLab mode; the control plane (the
same routers ``create_app`` mounts) over real HTTP sharing the workers'
database — the production shape, so the dispatch's GENERATION-SCOPED lane
token verifies against the run's live attempt generation and the dead
attempt's credential retires at the API; the lane runs as a REAL
subprocess driven with EXACTLY the env the dispatched variables describe.

Traces:

- **DP-1** — the initial dispatch carries the full envelope (fresh mode,
  control URL + attempt-scoped token) and NO control-plane root secret
  ever enters the variable set.
- **DP-2 (the main arc)** — pause (a real capture+upload under the
  DISPATCH-ISSUED token) → runner loss (the native cancel) → the
  restarted worker classifies → ``/retry`` re-dispatches with the EXACT
  resume contract (``FORGE_LANE_RESUME=1`` + the pinned checkpoint digest
  + a NEW attempt-scoped token); the dead attempt's token is refused by
  the control plane naming the superseded generation; the second runner's
  lane — driven by nothing but the dispatched variables — restores the
  exact WIP and completes; the delayed OLD pipeline's late success cannot
  displace the current candidate.
- **DP-3** — a redelivered ``/retry`` is one logical command.
- **DP-4** — a rotted required checkpoint halts the resumed lane before
  any vendor session exists (zero model turns), under the same dispatched
  envelope.

Assertions are on ARTIFACTS — the native dispatch ledger, the control
plane's HTTP answers, branch heads, the DB rows and the lane's own
generation pointer — never on status strings alone.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from forge.api_lane_control import lane_control_token
from forge.durable import FlowRun, FlowStatus, LLMCall

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
    start_control_plane,
)
from .test_production_entry import (
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
]

#: The resumed turn's own edits ON TOP of the restored WIP.
RESUMED_TURN_ACTIONS = [
    {"op": "write", "path": "src/app.py", "content": "print('resumed turn')\n"},
]


def _clone(source: Path, target: Path) -> None:
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
    return json.dumps(
        {
            "attempt_base": attempt_base,
            "driver": "claude-code",
            "model": PE_HARNESS_MODEL,
            "exit": "completed",
            "usage": None,
        }
    ).encode("utf-8")


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


def dispatched_variables(gitlab_native, index: int) -> dict[str, str]:
    """One dispatch's variables, as the NATIVE SERVER recorded them."""
    dispatch = gitlab_native.dispatches()[index]
    return {v["key"]: v["value"] for v in dispatch["variables"]}


async def _drive_to_first_dispatch(pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path):
    """issue → plan → /go: the initial (fresh) dispatch, through the real
    service and the real transport. Returns (service, run_id, env bits)."""
    monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "dp-store"))
    monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
    gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
    factory = pe_db.worker_factory()
    service = make_gitlab_service(
        factory,
        gitlab_client,
        settings=gl_settings(FORGE_LANE_CONTROL_SECRET=SecretStr(PE_LANE_SECRET)),
    )
    run_id = await service.start_run(
        GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
    )
    await service.handle_command_note(
        GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
    )
    return service, factory, run_id


# ----------------------------------------------------------------------
# DP-1 — the envelope on the initial dispatch
# ----------------------------------------------------------------------


class TestDP1FreshDispatchEnvelope:
    async def test_the_initial_dispatch_carries_the_envelope_and_no_root_secret(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
    ):
        control = await start_control_plane(pe_db.url)
        try:
            monkeypatch.setenv("FORGE_LANE_CONTROL_URL", control.base_url)
            service, factory, run_id = await _drive_to_first_dispatch(
                pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
            )

            run = await get_run(factory, run_id)
            assert run.status == FlowStatus.WAITING_HARNESS.value
            variables = dispatched_variables(gitlab_native, 0)
            assert variables["FORGE_RUN_ID"] == run_id
            # The WIP-continuity contract: FRESH — nothing restores.
            assert variables["FORGE_LANE_RESUME"] == ""
            assert variables["FORGE_LANE_RESUME_MODE"] == "fresh"
            assert variables["FORGE_RESUME_CHECKPOINT"] == ""
            assert variables["FORGE_ATTEMPT_GENERATION"] == "0"
            # The control dial-out pair: the deployment's URL + the
            # attempt-scoped HMAC computed at dispatch (generation 0).
            assert variables["FORGE_LANE_CONTROL_URL"] == control.base_url
            assert variables["FORGE_LANE_CONTROL_TOKEN"] == lane_control_token(
                PE_LANE_SECRET, run_id, generation=0
            )
            # NO control-plane root secret, no publication token, no
            # webhook secret ever enters the variable set.
            every_value = [v["value"] for d in gitlab_native.dispatches() for v in d["variables"]]
            for secret_value in (PE_LANE_SECRET, "glpat-test", "whsec"):
                assert secret_value not in every_value
            # The journaled envelope carries the CONTRACT (digest), never
            # the token value.
            envelope = (run.evidence or {})["harness"]["dispatch_envelope"]
            assert envelope["resume_mode"] == "fresh"
            assert len(envelope["digest"]) == 64
            assert variables["FORGE_LANE_CONTROL_TOKEN"] not in json.dumps(run.evidence)
            # The real client never fell off the modeled API surface.
            assert gitlab_native.unknown_paths() == []
        finally:
            control.stop()


# ----------------------------------------------------------------------
# DP-2 — pause → loss → /retry: the exact-resume dispatch + the retired
# credential + the lane driven by nothing but the dispatched envelope
# ----------------------------------------------------------------------


@pytest.fixture()
async def dp_lab(tmp_path: Path, pe_db, gitlab_native, gitlab_client, monkeypatch):
    """The full parity arc, driven once through native surfaces."""
    store_dir = tmp_path / "dp-store"
    monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(store_dir))
    monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
    monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
    # The production shape: the control plane shares the workers' database,
    # so the dispatch's GENERATION-SCOPED token verifies against the run's
    # live attempt generation.
    control = await start_control_plane(pe_db.url)
    try:
        monkeypatch.setenv("FORGE_LANE_CONTROL_URL", control.base_url)
        original, base_oid = make_checkout(tmp_path, "dp-workspace")
        gitlab_native.seed_commit(GL_BASE_BRANCH, base_oid, "frozen base")
        for name in ("README.md", "src/app.py"):
            gitlab_native.seed_file(name, (original / name).read_text())
        gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)

        # --- issue → plan → approved dispatch (worker #1) ----------------
        factory_a = pe_db.worker_factory()
        service_a = make_gitlab_service(
            factory_a,
            gitlab_client,
            settings=gl_settings(FORGE_LANE_CONTROL_SECRET=SecretStr(PE_LANE_SECRET)),
        )
        run_id = await service_a.start_run(
            GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
        )
        await service_a.handle_command_note(
            GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
        )
        dispatch_1 = gitlab_native.dispatches()[0]
        branch = dispatch_1["ref"]
        job_1 = gitlab_native.jobs(dispatch_1["pipeline_id"])[0]
        variables_1 = {v["key"]: v["value"] for v in dispatch_1["variables"]}
        token_1 = variables_1["FORGE_LANE_CONTROL_TOKEN"]

        # --- the FIRST runner: WIP, then the pause (a real capture+upload
        # under the DISPATCH-ISSUED token — exactly what the CI job holds) --
        eventlog = tmp_path / "dp-vendor-events.jsonl"
        run_vendor_once(original, FIRST_RUNNER_ACTIONS, eventlog)
        checkpoint_id = await upload_wip_checkpoint(
            control.base_url, original, run_id, token_1, base_oid
        )
        assert await record_resume_command(pe_db.worker_factory(), run_id) is True

        # --- KILL the first runner: the CI job is cancelled natively -----
        gitlab_native.cancel_job(job_1["id"])

        # --- the worker RESTARTS and classifies the loss ------------------
        await pe_db.dispose()
        factory_b = pe_db.worker_factory()
        service_b = make_gitlab_service(
            factory_b,
            gitlab_client,
            settings=gl_settings(FORGE_LANE_CONTROL_SECRET=SecretStr(PE_LANE_SECRET)),
        )
        await service_b.evaluate_waiting_harness()
        blocked = await get_run(factory_b, run_id)
        assert blocked.status == FlowStatus.BLOCKED.value

        # --- the operator's /retry → the exact-resume dispatch ------------
        await service_b.handle_retry_note(
            GL_PROJECT_ID,
            f"@forge /retry {run_id}",
            "alice",
            GL_ISSUE_IID,
            delivery_id="dp-retry-1",
        )
        dispatch_2 = gitlab_native.dispatches()[1]
        job_2 = gitlab_native.jobs(dispatch_2["pipeline_id"])[0]
        variables_2 = {v["key"]: v["value"] for v in dispatch_2["variables"]}

        yield SimpleNamespace(
            run_id=run_id,
            branch=branch,
            base_oid=base_oid,
            checkpoint_id=checkpoint_id,
            eventlog=eventlog,
            original=original,
            factory=factory_b,
            service=service_b,
            control=control,
            dispatch_1=dispatch_1,
            dispatch_2=dispatch_2,
            job_1=job_1,
            job_2=job_2,
            variables_1=variables_1,
            variables_2=variables_2,
            token_1=token_1,
            blocked_reason=blocked.status_reason,
        )
    finally:
        control.stop()


class TestDP2ExactResumeDispatch:
    def test_the_retry_dispatch_carries_the_exact_resume_contract(self, dp_lab):
        """The second dispatch: ``required`` + the EXACT pinned checkpoint
        digest + a NEW attempt-scoped token — the same envelope shape the
        GitHub reference dispatches, now on the GitLab pipeline."""
        lab = dp_lab
        variables = lab.variables_2
        assert lab.dispatch_2["ref"] == lab.branch  # the work continues in place
        assert variables["FORGE_RUN_ID"] == lab.run_id
        assert variables["FORGE_LANE_RESUME"] == "1"
        assert variables["FORGE_LANE_RESUME_MODE"] == "required"
        assert variables["FORGE_RESUME_CHECKPOINT"] == lab.checkpoint_id
        assert variables["FORGE_ATTEMPT_GENERATION"] == "1"
        assert variables["FORGE_CONTINUATION_DECISION_ID"]
        token_2 = variables["FORGE_LANE_CONTROL_TOKEN"]
        assert token_2 == lane_control_token(PE_LANE_SECRET, lab.run_id, generation=1)
        assert token_2 != lab.token_1  # the new attempt mints a new credential

    async def test_the_dead_attempts_credential_retires_at_the_api(self, dp_lab):
        """The first attempt's token — still held by whatever survived the
        cancelled job — is refused by the control plane naming the
        superseded generation; the CURRENT attempt's token is accepted."""
        lab = dp_lab
        stale = httpx.get(
            lab.control.url("/lane/controls"),
            params={"work_id": lab.run_id},
            headers={"Authorization": f"Bearer {lab.token_1}"},
            timeout=10.0,
        )
        assert stale.status_code == 403
        assert "superseded runner generation" in stale.text

        current = httpx.get(
            lab.control.url("/lane/controls"),
            params={"work_id": lab.run_id},
            headers={"Authorization": f"Bearer {lab.variables_2['FORGE_LANE_CONTROL_TOKEN']}"},
            timeout=10.0,
        )
        assert current.status_code == 200

    async def test_the_dispatched_envelope_drives_the_resumed_lane_and_publishes(
        self, dp_lab, gitlab_native
    ):
        """The second runner's REAL lane subprocess runs on NOTHING but the
        dispatched variables: the required restore (FORGE_LANE_RESUME=1)
        rebuilds the exact generation, the turn completes, the shipped
        collector captures restored+new edits and the reconciler publishes
        through the trusted publisher."""
        lab = dp_lab
        resumed = lab.original.parent / "dp-resumed"
        _clone(lab.original, resumed)
        lane = run_lane(
            resumed,
            work_id=lab.run_id,
            resume=lab.variables_2["FORGE_LANE_RESUME"],
            control_url=lab.variables_2["FORGE_LANE_CONTROL_URL"],
            token=lab.variables_2["FORGE_LANE_CONTROL_TOKEN"],
            attempt_base=lab.base_oid,
            actions=RESUMED_TURN_ACTIONS,
            eventlog=lab.eventlog,
        )
        assert lane.returncode == 0, lane.stderr
        # The restore rebuilt the EXACT committed generation: the pointer
        # names the dispatch's pinned checkpoint.
        pointer = json.loads((resumed / ".forge" / "workspace-generation").read_text())
        assert pointer["work_id"] == lab.run_id
        assert pointer["checkpoint_id"] == lab.checkpoint_id

        outcome = run_collector(resumed, work_id=lab.run_id, attempt_base=lab.base_oid)
        assert outcome.returncode == 0, outcome.stderr
        reported = json.loads(outcome.stdout)
        diff = Path(reported["diff_path"]).read_bytes()
        assert b"print('resumed turn')" in diff  # the resumed turn's edit
        assert b"first-runner edit" in diff  # the RESTORED first-runner WIP

        gitlab_native.seed_artifact(lab.job_2["id"], ".forge/candidate.diff", diff)
        gitlab_native.seed_artifact(
            lab.job_2["id"], ".forge/candidate.meta.json", _compose_meta(lab.base_oid)
        )
        gitlab_native.mark_job(lab.job_2["id"], "success")
        await lab.service.evaluate_waiting_harness()

        run = await get_run(lab.factory, lab.run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        candidate_sha = run.candidate_shas[-1]
        assert gitlab_native.branch_head(lab.branch) == candidate_sha
        (mr,) = gitlab_native.merge_requests().values()
        assert mr["title"] == f"Draft: {GL_ISSUE_TITLE}"  # merge stays human
        # Exactly ONE harness episode on the usage ledger (the resumed
        # attempt's turn), and the envelope journal names the contract the
        # current dispatch carried.
        calls = await llm_calls(lab.factory, lab.run_id)
        assert [call.role for call in calls] == ["implementer"]
        envelope = (run.evidence or {})["harness"]["dispatch_envelope"]
        assert envelope["resume_mode"] == "required"
        assert envelope["checkpoint_digest"] == lab.checkpoint_id

    async def test_a_delayed_old_pipeline_callback_cannot_displace_the_candidate(
        self, dp_lab, gitlab_native
    ):
        """The cancelled attempt's job comes back LATE claiming success
        with a well-formed candidate: the reconciler is bound to the
        CURRENT dispatch's pipeline (the durable handle), so the stale
        callback reconciles to nothing — no second commit, no MR change,
        the stale bytes never land."""
        lab = dp_lab
        await self.test_the_dispatched_envelope_drives_the_resumed_lane_and_publishes(
            lab, gitlab_native
        )
        head_before = gitlab_native.branch_head(lab.branch)
        mrs_before = dict(gitlab_native.merge_requests())
        run_before = await get_run(lab.factory, lab.run_id)
        candidates_before = list(run_before.candidate_shas or [])

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
            lab.job_1["id"], ".forge/candidate.meta.json", _compose_meta(lab.base_oid)
        )
        gitlab_native.mark_job(lab.job_1["id"], "success")

        # The dead worker's resurrected pass: restore the OLD handle (the
        # strongest form — everything about the artifacts is well-formed)
        # and poll once.
        from datetime import datetime, timezone

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
        assert run.status == FlowStatus.WAITING_CI.value  # untouched
        assert list(run.candidate_shas or []) == candidates_before
        assert gitlab_native.branch_head(lab.branch) == head_before
        assert gitlab_native.merge_requests() == mrs_before
        assert "stale attempt content" not in json.dumps(gitlab_native.state()["files"])


# ----------------------------------------------------------------------
# DP-3 — redelivery: one logical command
# ----------------------------------------------------------------------


class TestDP3Redelivery:
    async def test_a_redelivered_retry_dispatches_once(self, dp_lab, gitlab_native):
        lab = dp_lab
        dispatches_before = len(gitlab_native.dispatches())
        acks_before = len([n for n in gitlab_native.notes() if "retried by @alice" in n])

        await lab.service.handle_retry_note(
            GL_PROJECT_ID,
            f"@forge /retry {lab.run_id}",
            "alice",
            GL_ISSUE_IID,
            delivery_id="dp-retry-1",  # the SAME delivery id, redelivered
        )

        assert len(gitlab_native.dispatches()) == dispatches_before  # no 3rd pipeline
        assert len([n for n in gitlab_native.notes() if "retried by @alice" in n]) == acks_before


# ----------------------------------------------------------------------
# DP-4 — a corrupt required checkpoint: zero model turns
# ----------------------------------------------------------------------


class TestDP4CorruptRequiredCheckpoint:
    async def test_the_dispatched_required_envelope_halts_before_the_vendor(
        self, tmp_path: Path, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        """A required-resume dispatch whose pinned checkpoint ROTS at the
        authority: the second runner's lane — driven by the dispatched
        envelope — halts before any vendor session exists (zero model
        turns, zero publication, no generation pointer)."""
        control = await start_control_plane(pe_db.url)
        try:
            monkeypatch.setenv("FORGE_LANE_CONTROL_URL", control.base_url)
            store_dir = tmp_path / "dp4-store"
            monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(store_dir))
            monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
            monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
            checkout, base_oid = make_checkout(tmp_path, "dp4-workspace")
            gitlab_native.seed_commit(GL_BASE_BRANCH, base_oid, "frozen base")
            for name in ("README.md", "src/app.py"):
                gitlab_native.seed_file(name, (checkout / name).read_text())
            gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)

            factory = pe_db.worker_factory()
            service = make_gitlab_service(
                factory,
                gitlab_client,
                settings=gl_settings(FORGE_LANE_CONTROL_SECRET=SecretStr(PE_LANE_SECRET)),
            )
            run_id = await service.start_run(
                GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
            )
            await service.handle_command_note(
                GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
            )
            dispatch_1 = gitlab_native.dispatches()[0]
            job_1 = gitlab_native.jobs(dispatch_1["pipeline_id"])[0]
            token_1 = dispatched_variables(gitlab_native, 0)["FORGE_LANE_CONTROL_TOKEN"]

            eventlog = tmp_path / "dp4-vendor-events.jsonl"
            run_vendor_once(checkout, FIRST_RUNNER_ACTIONS, eventlog)
            checkpoint_id = await upload_wip_checkpoint(
                control.base_url, checkout, run_id, token_1, base_oid
            )
            assert await record_resume_command(pe_db.worker_factory(), run_id) is True

            gitlab_native.cancel_job(job_1["id"])
            await service.evaluate_waiting_harness()
            assert (await get_run(factory, run_id)).status == FlowStatus.BLOCKED.value

            # The /retry decision is made over the CLEAN checkpoint (the
            # exact committed one — the corruption below lands AFTER the
            # decision, exactly the real-world sequence: the rot is
            # discovered at restore time, and the refusal table's own
            # corrupt-checkpoint arm is a different, earlier failure).
            await service.handle_retry_note(
                GL_PROJECT_ID,
                f"@forge /retry {run_id}",
                "alice",
                GL_ISSUE_IID,
                delivery_id="dp4-retry-1",
            )
            variables = dispatched_variables(gitlab_native, 1)
            assert variables["FORGE_LANE_RESUME"] == "1"
            assert variables["FORGE_RESUME_CHECKPOINT"] == checkpoint_id

            # ROT one referenced blob at the authority — the pinned bytes
            # no longer reproduce their content address. (The read uses
            # the CURRENT attempt's dispatched token: the first attempt's
            # credential retired at the /retry's generation bump.)
            served = httpx.get(
                control.url(f"/lane/checkpoints/{run_id}"),
                headers={"Authorization": f"Bearer {variables['FORGE_LANE_CONTROL_TOKEN']}"},
                timeout=10.0,
            )
            assert served.status_code == 200
            first_blob = next(iter(served.json()["blobs"]))
            blob_path = store_dir / first_blob[:2] / first_blob
            assert blob_path.is_file()
            blob_path.write_bytes(b"rotted bytes - not the addressed content\n")

            # The second runner's lane: driven by nothing but the dispatched
            # envelope — the REQUIRED restore fails loudly, the vendor never
            # exists.
            resumed = tmp_path / "dp4-resumed"
            _clone(checkout, resumed)
            lane = run_lane(
                resumed,
                work_id=run_id,
                resume=variables["FORGE_LANE_RESUME"],
                control_url=variables["FORGE_LANE_CONTROL_URL"],
                token=variables["FORGE_LANE_CONTROL_TOKEN"],
                attempt_base=base_oid,
                actions=[{"op": "write", "path": "evil.txt", "content": "never\n"}],
                eventlog=eventlog,
            )
            assert lane.returncode != 0
            assert "wip_restore_failed" in lane.stderr

            kinds = [
                json.loads(line)["kind"]
                for line in eventlog.read_text().splitlines()
                if line.strip()
            ]
            assert kinds, "the pre-pause vendor leg must be on record"
            assert kinds[kinds.index("vendor_edits") + 1 :] == []  # ZERO new model turns
            assert not (resumed / "forge-output").exists()
            assert not (resumed / "evil.txt").exists()
        finally:
            control.stop()
