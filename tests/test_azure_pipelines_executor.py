"""Azure Pipelines executor + lane template tests (AZ-3, ADR-0024).

Drives :class:`forge.execution.azure_pipelines.AzurePipelinesExecutor`
over a fake :class:`~forge.integrations.azure.AzureDevOpsClient` surface —
the launch / poll / cancel / reconcile_launch contract, Runs-API
correlation vs the builds-API Server fallback (which must NEVER send a
``sourceVersion`` filter, research §6.6), artifact collection and
timeline-based failure classification, no network.

The lane template contract (``ci/templates/forge-lane.azure-pipelines.yml``)
is grep/asserted in the same spirit as ``tests/test_templates.py``: the
proposal-only invariants (no triggers, no persisted credentials, no
``System.AccessToken``, FORBIDDEN push, ``always()`` candidate steps) must
fail CI, not a live run.
"""

import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import yaml

from forge.execution.azure_pipelines import (
    AzurePipelinesExecutor,
    AzurePipelinesHandle,
    artifact_name_for,
)
from forge.integrations.azure import AzureDevOpsError, PipelineRun
from tests.fixtures.candidate import create_diff

BASE = Path(__file__).parent
PROJECT = "Fabrikam"
REPO_ID = "1a2b3c4d-0000-0000-0000-000000000001"
PIPELINE_ID = 207
RUN_ID = 99001
BRANCH = "refs/heads/forge/wi-42"
ATTEMPT_BASE = "9c4e2a7f1b3d8e5a60c2f47b19d3a8e07f5c6b21"
FORGE_RUN_ID = "run-7f3a"
WORK_ITEM_ID = "142"
MODEL = "glm-5.3-flash[1m]"
STARTED = datetime(2026, 9, 15, 13, 0, 0, tzinfo=timezone.utc)

TEMPLATE_PATH = BASE.parent / "ci" / "templates" / "forge-lane.azure-pipelines.yml"


def make_settings(**overrides):
    class S:
        FORGE_HARNESS_TIMEOUT_SECONDS = 1800

    for key, value in overrides.items():
        setattr(S, key, value)
    return S()


def make_handle(**overrides) -> AzurePipelinesHandle:
    values = dict(
        provider="azure_devops",
        project=PROJECT,
        repo_id=REPO_ID,
        pipeline_id=PIPELINE_ID,
        run_id=0,
        branch=BRANCH,
        attempt_base_sha=ATTEMPT_BASE,
        run_spec_digest="spec" * 16,
        driver="claude-code",
        model=MODEL,
        work_item_id=WORK_ITEM_ID,
        forge_run_id=FORGE_RUN_ID,
        started_at=STARTED.isoformat(),
    )
    values.update(overrides)
    return AzurePipelinesHandle(**values)


def candidate_files(
    exit_status: str = "completed", base: str = ATTEMPT_BASE, prefix: str = ""
) -> dict[str, bytes]:
    meta = {
        "attempt_base": base,
        "run_id": FORGE_RUN_ID,
        "driver": "claude-code",
        "model": MODEL,
        "exit": exit_status,
        "usage": {"input_tokens": 120, "output_tokens": 45},
    }
    return {
        f"{prefix}candidate.diff": create_diff("src/app.py", "print('implemented')\n").encode(
            "utf-8"
        ),
        f"{prefix}candidate.meta.json": json.dumps(meta).encode("utf-8"),
    }


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class FakeAzureDevOps:
    """The executor's client surface, in memory (no network).

    Mirrors the AZ-1 client method signatures exactly, so a signature
    drift between the executor and :class:`AzureDevOpsClient` fails here.
    """

    @property
    def org_url(self) -> str:
        return "https://dev.azure.test/fabrikam"

    def __init__(self) -> None:
        self.dispatches: list[dict[str, Any]] = []
        self.dispatch_run_id = RUN_ID  # 0 emulates the empty Server response
        self.runs: dict[int, PipelineRun] = {}
        self.builds: list[dict[str, Any]] = []
        self.build_queries: list[dict[str, Any]] = []
        self.artifacts: dict[int, dict[str, dict[str, bytes]]] = {}
        self.timelines: dict[int, dict[str, Any]] = {}
        self.task_logs: dict[int, str] = {}
        self.cancelled: list[int] = []
        self.get_run_calls: list[int] = []
        self.cancel_error: AzureDevOpsError | None = None

    async def run_pipeline(
        self,
        project: str,
        pipeline_id: int,
        *,
        ref_name: str,
        template_parameters: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
    ) -> PipelineRun:
        self.dispatches.append(
            {
                "project": project,
                "pipeline_id": pipeline_id,
                "ref_name": ref_name,
                "template_parameters": dict(template_parameters or {}),
                "variables": dict(variables or {}),
            }
        )
        return PipelineRun(run_id=self.dispatch_run_id, state="inProgress", result=None, url=None)

    def seed_run(self, run_id: int, state: str, result: str | None = None) -> None:
        self.runs[run_id] = PipelineRun(run_id=run_id, state=state, result=result, url=None)

    async def get_run(self, project: str, pipeline_id: int, run_id: int) -> PipelineRun:
        self.get_run_calls.append(run_id)
        run = self.runs.get(run_id)
        if run is None:
            raise AzureDevOpsError(404, f"run {run_id} not found")
        return run

    def seed_build(self, **fields: Any) -> None:
        build = {"id": RUN_ID, "sourceBranch": BRANCH, "parameters": "{}"}
        build.update(fields)
        self.builds.insert(0, build)  # queryOrder=queueTimeDescending

    async def list_builds_by_repository(
        self,
        project: str,
        repo_id: str,
        *,
        definitions: list[int] | None = None,
        min_time: datetime | None = None,
        top: int = 25,
    ) -> list[dict[str, Any]]:
        # ONLY documented params cross this seam — the executor cannot send
        # a sourceVersion filter because this signature has nowhere to put
        # one (research §6.6 correction #1).
        self.build_queries.append(
            {
                "project": project,
                "repo_id": repo_id,
                "definitions": definitions,
                "min_time": min_time,
                "top": top,
            }
        )
        return list(self.builds)

    def seed_artifact(self, run_id: int, name: str, files: dict[str, bytes]) -> None:
        self.artifacts.setdefault(run_id, {})[name] = files

    async def download_run_artifact(
        self, project: str, pipeline_id: int, run_id: int, artifact_name: str
    ) -> bytes:
        named = self.artifacts.get(run_id, {})
        files = named.get(artifact_name)
        if files is None:
            raise AzureDevOpsError(404, f"artifact {artifact_name!r} has no signedContent url")
        return zip_bytes(files)

    def seed_timeline(self, build_id: int, records: list[dict[str, Any]]) -> None:
        self.timelines[build_id] = {"id": "t" * 8, "records": records}

    async def get_timeline(self, project: str, build_id: int) -> dict[str, Any]:
        timeline = self.timelines.get(build_id)
        if timeline is None:
            raise AzureDevOpsError(404, f"no timeline for build {build_id}")
        return timeline

    def seed_task_log(self, build_id: int, log_id: int, text: str) -> None:
        self.task_logs[(build_id, log_id)] = text

    async def get_task_log(self, project: str, build_id: int, log_id: int) -> str:
        return self.task_logs.get((build_id, log_id), "")

    async def cancel_build(self, project: str, build_id: int) -> dict[str, Any]:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(build_id)
        return {"id": build_id, "status": "cancelling"}


def make_executor(fake: FakeAzureDevOps, **settings) -> AzurePipelinesExecutor:
    return AzurePipelinesExecutor(fake, make_settings(**settings))


def failed_task_record(log_id: int = 5) -> dict[str, Any]:
    return {
        "id": "33330000-0000-0000-0000-000000000033",
        "type": "Task",
        "identifier": "harness.ClaudeCodeDriver",
        "name": "Run harness driver",
        "state": "completed",
        "result": "failed",
        "errorCount": 1,
        "log": {"id": log_id, "type": "Container"},
    }


# ----------------------------------------------------------------------
# Handle round-trip
# ----------------------------------------------------------------------


class TestHandle:
    def test_json_round_trip_preserves_fields(self):
        handle = make_handle(run_id=RUN_ID)

        parsed = AzurePipelinesHandle.from_json(handle.to_json())

        assert parsed == handle

    def test_provider_is_azure_devops(self):
        assert make_handle().provider == "azure_devops"

    def test_artifact_name_follows_the_template_contract(self):
        assert artifact_name_for(FORGE_RUN_ID) == f"forge-candidate-{FORGE_RUN_ID}"


# ----------------------------------------------------------------------
# launch: Runs-API dispatch correlation (research §6.2)
# ----------------------------------------------------------------------


class TestLaunch:
    async def test_dispatch_body_carries_the_template_parameters(self):
        fake = FakeAzureDevOps()
        executor = make_executor(fake)

        handle = await executor.launch(make_handle())

        assert handle.run_id == RUN_ID  # the dispatch-returned run id
        (dispatch,) = fake.dispatches
        assert dispatch["project"] == PROJECT
        assert dispatch["pipeline_id"] == PIPELINE_ID
        assert dispatch["ref_name"] == BRANCH  # the factory branch is the ref
        assert dispatch["template_parameters"] == {
            "run_id": FORGE_RUN_ID,
            "attempt_base": ATTEMPT_BASE,
            "driver": "claude-code",
            "model": MODEL,
            "work_item_id": WORK_ITEM_ID,
        }

    async def test_template_parameters_cross_the_wire_as_strings(self):
        """REST templateParameters arrive at YAML evaluation as strings —
        the executor never sends native ints/bools (research §6.2)."""
        fake = FakeAzureDevOps()
        executor = make_executor(fake)

        await executor.launch(make_handle())

        (dispatch,) = fake.dispatches
        assert all(isinstance(value, str) for value in dispatch["template_parameters"].values())

    async def test_empty_run_id_response_falls_back_to_build_discovery(self):
        fake = FakeAzureDevOps()
        fake.dispatch_run_id = 0  # legacy/Server: no run id in the response
        fake.seed_build(
            id=77,
            sourceBranch=BRANCH,
            parameters=json.dumps({"run_id": FORGE_RUN_ID, "attempt_base": ATTEMPT_BASE}),
        )
        executor = make_executor(fake)

        handle = await executor.launch(make_handle())

        assert handle.run_id == 77  # discovered via branch + template parameters
        assert len(fake.dispatches) == 1  # dispatched exactly once


# ----------------------------------------------------------------------
# reconcile_launch: ADR-0005 re-entry, never a second dispatch
# ----------------------------------------------------------------------


class TestReconcileLaunch:
    async def test_uncorrelated_handle_is_re_discovered(self):
        fake = FakeAzureDevOps()
        fake.seed_build(
            id=88,
            templateParameters={"run_id": FORGE_RUN_ID},  # object form
        )
        executor = make_executor(fake)

        handle = await executor.reconcile_launch(make_handle())

        assert handle.run_id == 88
        assert fake.dispatches == []  # never dispatches again

    async def test_correlated_handle_is_only_verified(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="inProgress")
        executor = make_executor(fake)

        handle = await executor.reconcile_launch(make_handle(run_id=RUN_ID))

        assert handle.run_id == RUN_ID
        assert fake.get_run_calls == [RUN_ID]
        assert fake.build_queries == []

    async def test_discovery_never_sends_a_source_version_filter(self):
        """Ground truth (research §6.6): builds has NO sourceVersion query
        param — the discovery query must carry only documented params."""
        fake = FakeAzureDevOps()
        fake.seed_build(id=88)
        executor = make_executor(fake)

        await executor.reconcile_launch(make_handle())

        (query,) = fake.build_queries
        assert set(query) == {"project", "repo_id", "definitions", "min_time", "top"}
        assert query["definitions"] == [PIPELINE_ID]  # scoped to the lane
        assert query["repo_id"] == REPO_ID

    async def test_discovery_refuses_a_branch_mismatch(self):
        fake = FakeAzureDevOps()
        fake.seed_build(
            id=77,
            sourceBranch="refs/heads/forge/other",  # not our dispatch branch
            templateParameters={"run_id": FORGE_RUN_ID},
        )
        executor = make_executor(fake)

        handle = await executor.reconcile_launch(make_handle())

        assert handle.run_id == 0  # ordering alone is never trusted


# ----------------------------------------------------------------------
# poll: the state/result machine + artifacts
# ----------------------------------------------------------------------


class TestPoll:
    async def test_uncorrelated_handle_keeps_waiting(self):
        executor = make_executor(FakeAzureDevOps())

        outcome = await executor.poll(make_handle())

        assert outcome.status == "running"

    async def test_in_progress_run_is_running_before_the_deadline(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="inProgress")
        executor = make_executor(fake)

        outcome = await executor.poll(
            make_handle(run_id=RUN_ID), now=STARTED + timedelta(seconds=60)
        )

        assert outcome.status == "running"

    async def test_deadline_breach_is_harness_timeout(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="inProgress")
        executor = make_executor(fake, FORGE_HARNESS_TIMEOUT_SECONDS=600)

        outcome = await executor.poll(
            make_handle(run_id=RUN_ID), now=STARTED + timedelta(seconds=601)
        )

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"
        assert outcome.reason == "harness_timeout"

    async def test_completed_run_without_result_yet_keeps_waiting(self):
        """§6.5 caveat: state and result can lag independently — a
        terminal state with no result must not mis-classify."""
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result=None)
        executor = make_executor(fake)

        outcome = await executor.poll(
            make_handle(run_id=RUN_ID), now=STARTED + timedelta(seconds=60)
        )

        assert outcome.status == "running"

    async def test_success_collects_the_candidate_bundle(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="succeeded")
        fake.seed_artifact(RUN_ID, artifact_name_for(FORGE_RUN_ID), candidate_files())
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "change_candidate"
        bundle = outcome.bundle
        assert bundle is not None
        assert bundle.attempt_base_oid == ATTEMPT_BASE
        assert bundle.driver_exit == "completed"
        assert [entry.path for entry in bundle.entries] == ["src/app.py"]
        assert bundle.usage is not None
        assert bundle.usage.input_tokens == 120
        assert bundle.usage.completeness == "aggregate"

    async def test_succeeded_with_issues_still_collects_with_a_note(self):
        """``succeededWithIssues`` is a success-with-issues: the candidate
        is collected and the note travels in the outcome summary."""
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="succeededWithIssues")
        fake.seed_artifact(RUN_ID, artifact_name_for(FORGE_RUN_ID), candidate_files())
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "change_candidate"
        assert "succeededWithIssues" in outcome.summary

    async def test_artifact_entries_match_with_the_forge_prefix(self):
        """publish: stores the .forge/ paths — basename-tolerant extraction
        (same contract the Actions executor honors)."""
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="succeeded")
        fake.seed_artifact(
            RUN_ID, artifact_name_for(FORGE_RUN_ID), candidate_files(prefix=".forge/")
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "change_candidate"

    async def test_artifact_base_mismatch_is_rejected(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="succeeded")
        fake.seed_artifact(
            RUN_ID, artifact_name_for(FORGE_RUN_ID), candidate_files(base="e" * 40)
        )  # lies
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "failed"
        assert outcome.reason.startswith("harness_attempt_base_mismatch")

    async def test_missing_artifact_is_reported(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="succeeded")
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "failed"
        assert outcome.reason == "harness_artifact_missing"

    async def test_driver_failure_exit_is_never_adopted(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="succeeded")
        fake.seed_artifact(
            RUN_ID, artifact_name_for(FORGE_RUN_ID), candidate_files(exit_status="failed")
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"
        assert outcome.reason.startswith("harness_driver_failed")

    async def test_failed_bootstrap_classifies_infrastructure_never_code(self):
        """A18: a FAILED environment bootstrap recorded in the meta's
        additive ``bootstrap`` field is infrastructure/config — the
        environment never matched the approved execution profile — never
        a code-repair candidate, on the AzDO lane too."""
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="succeeded")
        meta = json.dumps(
            {
                "attempt_base": ATTEMPT_BASE,
                "exit": "failed",
                "bootstrap": "failed",
            }
        ).encode("utf-8")
        fake.seed_artifact(
            RUN_ID,
            artifact_name_for(FORGE_RUN_ID),
            {
                "candidate.diff": create_diff("src/app.py", "half done\n").encode("utf-8"),
                "candidate.meta.json": meta,
            },
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"
        assert outcome.reason.startswith("harness_bootstrap_failed")

    async def test_empty_diff_is_no_changes(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="succeeded")
        meta = json.dumps({"attempt_base": ATTEMPT_BASE, "exit": "completed"}).encode("utf-8")
        fake.seed_artifact(
            RUN_ID,
            artifact_name_for(FORGE_RUN_ID),
            {"candidate.diff": b"", "candidate.meta.json": meta},
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "failed"
        assert outcome.reason == "harness_no_changes"

    async def test_zip_without_the_contract_files_is_rejected(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="succeeded")
        fake.seed_artifact(RUN_ID, artifact_name_for(FORGE_RUN_ID), {"unrelated.txt": b"noise"})
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "failed"
        assert "harness_artifact_invalid" in outcome.reason

    async def test_canceled_result_is_infrastructure(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="canceled")
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"
        assert "canceled" in outcome.reason

    async def test_failed_run_with_a_clean_log_classifies_code(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="failed")
        fake.seed_timeline(RUN_ID, [failed_task_record()])
        fake.seed_task_log(RUN_ID, 5, "claude: the agent exited with code 1\n")
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"
        assert "harness task" in outcome.reason

    async def test_failed_run_with_a_quota_pattern_classifies_infrastructure(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="failed")
        fake.seed_timeline(RUN_ID, [failed_task_record()])
        fake.seed_task_log(RUN_ID, 5, "anthropic.AuthenticationError: invalid api key\n")
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"

    async def test_failed_run_without_task_evidence_is_infrastructure(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="failed")
        # No timeline seeded: the evidence blames nobody — infrastructure.
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"

    async def test_unknown_result_on_a_finished_run_is_infrastructure(self):
        fake = FakeAzureDevOps()
        fake.seed_run(RUN_ID, state="completed", result="unknown")
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=RUN_ID))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"


# ----------------------------------------------------------------------
# cancel: the Runs area has no cancel — PATCH the build (research §6.5)
# ----------------------------------------------------------------------


class TestCancel:
    async def test_cancel_requests_build_cancellation(self):
        fake = FakeAzureDevOps()
        executor = make_executor(fake)

        await executor.cancel(make_handle(run_id=RUN_ID))

        assert fake.cancelled == [RUN_ID]  # runId == buildId (research §6.3)

    async def test_cancel_tolerates_an_already_completed_build(self):
        fake = FakeAzureDevOps()
        fake.cancel_error = AzureDevOpsError(409, "already completed")
        executor = make_executor(fake)

        await executor.cancel(make_handle(run_id=RUN_ID))  # 409 swallowed

        assert fake.cancelled == []

    async def test_cancel_without_correlation_is_a_noop(self):
        executor = make_executor(FakeAzureDevOps())

        await executor.cancel(make_handle(run_id=0))  # must not raise


# ----------------------------------------------------------------------
# The lane template contract (proposal-only invariants, test_templates style)
# ----------------------------------------------------------------------


def load_template() -> dict:
    """Parse the lane YAML (pyyaml-safe: no ``on:`` key exists at all)."""
    template = yaml.safe_load(TEMPLATE_PATH.read_text())
    assert isinstance(template, dict)
    return template


def lane_steps() -> list[dict]:
    """The harness job's steps (the template's single job)."""
    template = load_template()
    (job,) = template["jobs"]
    assert job["job"] == "harness"
    return job["steps"]


class TestLaneTemplateContract:
    def test_dispatch_only_with_explicit_trigger_none(self):
        """B05: dispatch happens ONLY via the Runs API — and without an
        explicit ``trigger: none`` the IMPLIED CI trigger would queue the
        lane on every branch push outside forge's control (the required
        parameters then fail validation or queue a doomed run). pr: is
        ignored by Azure Repos (research ground truth) and stays absent."""
        template = load_template()
        text = TEMPLATE_PATH.read_text()

        assert template["trigger"] in (None, "none")
        assert "triggers" not in template
        assert True not in template  # pyyaml turns a bare `on:` into True
        assert not any(line.lstrip().startswith("pr:") for line in text.splitlines())

    def test_queue_time_parameters_match_the_executor(self):
        """What forge dispatches (launch's templateParameters) is exactly
        what the template declares, strings only (research §6.2)."""
        template = load_template()

        parameters = {p["name"]: p for p in template["parameters"]}
        assert set(parameters) == {
            "run_id",
            "attempt_base",
            "driver",
            "model",
            "work_item_id",
            "repair_context",
            # B04: the envelope binding trio (empty defaults — legacy
            # dispatches omit them; enforced renders require all three).
            "plan_note_id",
            "envelope_digest",
            "spec_digest",
        }
        assert all(spec["type"] == "string" for spec in parameters.values())

    def test_checkout_self_persists_no_credentials(self):
        text = TEMPLATE_PATH.read_text()

        assert "checkout: self" in text
        assert "persistCredentials: false" in text  # explicit, not defaulted

    def test_push_is_forbidden(self):
        text = TEMPLATE_PATH.read_text()

        assert "git remote set-url --push origin FORBIDDEN" in text

    def test_system_access_token_never_referenced(self):
        text = TEMPLATE_PATH.read_text()

        assert "System.AccessToken" not in text

    def test_detached_checkout_of_the_parameter_base(self):
        text = TEMPLATE_PATH.read_text()

        assert 'git checkout --detach "$FORGE_ATTEMPT_BASE"' in text
        # the env mapping must carry the parameter explicitly (live-found:
        # job variables are macro-expanded, never exported as env)
        assert "FORGE_ATTEMPT_BASE: ${{ parameters.attempt_base }}" in text
        assert 'git checkout --detach "$FORGE_ATTEMPT_BASE"' in text
        # The base comes from the queue-time parameter, echoed to an env var.
        assert "FORGE_ATTEMPT_BASE: ${{ parameters.attempt_base }}" in text

    def test_control_dir_excluded_from_the_index(self):
        text = TEMPLATE_PATH.read_text()

        assert 'echo ".forge/" >> .git/info/exclude' in text

    def test_candidate_steps_always_run(self):
        steps = lane_steps()

        candidate_steps = [
            step
            for step in steps
            if step.get("publish") is not None or "candidate.diff" in str(step.get("bash") or "")
        ]
        assert len(candidate_steps) >= 2  # diff capture + publish
        assert all(step.get("condition") == "always()" for step in candidate_steps)

    def test_candidate_diff_against_the_frozen_base(self):
        text = TEMPLATE_PATH.read_text()

        assert "git add -A" in text
        # The candidate is a TEXT patch (binary_not_supported, R08): the
        # emit step scrubs the driver's __pycache__/.pyc droppings BEFORE
        # staging, and the diff is emitted WITHOUT --binary (LIVE-found
        # 2026-09-20: a staged .pyc failed every candidate closed).
        assert 'git diff --cached --full-index "$FORGE_ATTEMPT_BASE"' in text
        assert "rm -rf __pycache__ .pytest_cache" in text
        assert "*.pyc" in text

    def test_meta_json_carries_the_contract_fields(self):
        text = TEMPLATE_PATH.read_text()

        assert ".forge/candidate.meta.json" in text
        for key in ("attempt_base", "run_id", "driver", "model", '"exit"', "usage"):
            assert key in text

    def test_artifact_name_matches_the_executor(self):
        steps = lane_steps()

        publish = next(step for step in steps if "publish" in step)
        assert publish["artifact"] == "forge-candidate-${{ parameters.run_id }}"
        # The staged contract directory (R16 closed allowlist), never the
        # whole .forge/ control directory (LIVE-found 2026-09-20).
        assert publish["publish"] == "forge-output"

    def test_driver_step_runs_the_entry_point_and_never_fails_the_job(self):
        text = TEMPLATE_PATH.read_text()
        steps = lane_steps()

        driver = next(step for step in steps if step.get("displayName") == "Run harness driver")
        assert "python -m forge.harness_entry" in driver["bash"]
        assert "exit 0" in driver["bash"]  # the audit trail is the artifact
        # The install ships a REAL released tag as the default — never a
        # <PLACEHOLDER> a raw copy would carry to the runner (the LIVE
        # class: pip attempted the literal ref and the lane died in
        # bootstrap). FORGE_LANE_REF (mapped with the compile-time
        # expression, not the literal-expanding $(macro)) overrides; the
        # tag default is refreshed deliberately per release (phase 3 of
        # docs/research/2026-09-22-script-rendering-architecture.md §7).
        assert "<PINNED_REF>" not in text
        assert (
            'pip install "forge @ git+https://github.com/forcewake/forge@${FORGE_LANE_REF:-v0.27.0}"'
            in text
        )
        assert "FORGE_LANE_REF: ${{ variables.FORGE_LANE_REF }}" in text

    def test_brief_transport_is_opt_in_via_the_owners_read_token(self):
        """With FORGE_AZDO_READ_TOKEN set the lane fetches its own brief
        (--render-brief-azure) BEFORE the driver; a failed fetch falls back
        to the pre-provisioned .forge/brief.md. The token is the repo
        owner's read-only PAT — never forge's (the PAT test above)."""
        text = TEMPLATE_PATH.read_text()
        steps = lane_steps()

        driver = next(step for step in steps if step.get("displayName") == "Run harness driver")
        assert "--render-brief-azure" in driver["bash"]
        assert "FORGE_AZDO_READ_TOKEN" in driver["bash"]  # the opt-in condition
        assert "falling back to .forge/brief.md" in driver["bash"]
        assert driver["bash"].index("--render-brief-azure") < driver["bash"].index(
            "python -m forge.harness_entry || echo"
        )
        # The WIT GETs are routed by mapped env, not hardcoded values —
        # the project rides on the predefined System.TeamProject variable.
        assert "FORGE_AZDO_READ_TOKEN: $(FORGE_AZDO_READ_TOKEN)" in text
        assert "FORGE_AZDO_ORG_URL: $(FORGE_AZDO_ORG_URL)" in text
        assert "FORGE_AZDO_PROJECT: $(System.TeamProject)" in text
        assert "FORGE_AZDO_BOT_NAME: $(FORGE_AZDO_BOT_NAME)" in text

    def test_harness_provider_keys_mapped_from_secret_variables(self):
        text = TEMPLATE_PATH.read_text()

        # C03: the driver env maps the REGISTRY surfaces through the
        # conditional LANE_* variables (an unselected provider's secret is
        # never delivered). grok's auth blob rides FORGE_GROK_AUTH.
        for name in (
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ZAI_API_KEY",
            "FORGE_GROK_AUTH",
            "COPILOT_GITHUB_TOKEN",
        ):
            assert f"{name}: $(LANE_" in text, name
        assert "XAI_API_KEY" not in text  # C03: grok-build takes FORGE_GROK_AUTH
        assert "FORGE_HARNESS_MCP: $(FORGE_HARNESS_MCP)" in text

    def test_lane_never_receives_forge_credentials(self):
        text = TEMPLATE_PATH.read_text()

        for forbidden in (
            "FORGE_AZDO_PAT",
            "FORGE_AZDO_WEBHOOK_PASSWORD",
            "FORGE_AZDO_WEBHOOK_USERNAME",
        ):
            assert forbidden not in text

    def test_pool_documented_with_a_hosted_default(self):
        template = load_template()
        text = TEMPLATE_PATH.read_text()

        job = template["jobs"][0]
        assert job["pool"]["vmImage"] == "ubuntu-latest"
        assert "repo owner" in text  # the pool choice is the owner's

    def test_onboarding_contract_in_the_header(self):
        text = TEMPLATE_PATH.read_text()

        assert "FORGE_AZDO_LANE_PIPELINE_ID" in text
        assert "templateParameters" in text
