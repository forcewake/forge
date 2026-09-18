"""GitHub Actions executor tests (E3b, ADR-0020).

Drives :class:`forge.execution.github_actions.GitHubActionsExecutor` over
:class:`tests.fixtures.fake_github.FakeGitHub` — the launch / poll / cancel /
reconcile_launch contract, correlation (2026 run-id response vs legacy
discovery), artifact collection and failure classification, no network.
"""

import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path


from forge.execution import github_actions as executor_module
from forge.execution.github_actions import (
    ActionsHandle,
    GitHubActionsExecutor,
    artifact_name_for,
)
from tests.fixtures.candidate import create_diff
from tests.fixtures.fake_github import FakeGitHub

BASE = Path(__file__).parent
OWNER, REPO = "acme", "acme-widget"
WORKFLOW = "forge-harness.github.yml"
BRANCH = "forge/42/abcd1234"
ATTEMPT_BASE = "1" * 40
FORGE_RUN_ID = "d" * 32
STARTED = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def make_settings(**overrides):
    class S:
        FORGE_HARNESS_TIMEOUT_SECONDS = 1800

    for key, value in overrides.items():
        setattr(S, key, value)
    return S()


def make_handle(**overrides) -> ActionsHandle:
    values = dict(
        provider="github",
        owner=OWNER,
        repo=REPO,
        workflow=WORKFLOW,
        run_id=0,
        branch=BRANCH,
        attempt_base=ATTEMPT_BASE,
        run_spec_digest="spec" * 16,
        driver="claude-code",
        forge_run_id=FORGE_RUN_ID,
        started_at=STARTED.isoformat(),
    )
    values.update(overrides)
    return ActionsHandle(**values)


def make_executor(fake: FakeGitHub, **settings) -> GitHubActionsExecutor:
    return GitHubActionsExecutor(fake, make_settings(**settings))


def candidate_files(exit_status: str = "completed", base: str = ATTEMPT_BASE) -> dict[str, bytes]:
    meta = {
        "attempt_base": base,
        "run_id": FORGE_RUN_ID,
        "driver": "claude-code",
        "model": "glm-5.3-flash[1m]",
        "exit": exit_status,
        "usage": {"input_tokens": 120, "output_tokens": 45},
    }
    return {
        "candidate.diff": create_diff("src/app.py", "print('implemented')\n").encode("utf-8"),
        "candidate.meta.json": json.dumps(meta).encode("utf-8"),
    }


# ----------------------------------------------------------------------
# Handle round-trip
# ----------------------------------------------------------------------


class TestHandle:
    def test_json_round_trip_preserves_fields(self):
        handle = make_handle(run_id=4242)

        parsed = ActionsHandle.from_json(handle.to_json())

        assert parsed == handle

    def test_artifact_name_follows_the_template_contract(self):
        assert artifact_name_for(FORGE_RUN_ID) == f"forge-candidate-{FORGE_RUN_ID}"


# ----------------------------------------------------------------------
# launch: dispatch correlation (research §1)
# ----------------------------------------------------------------------


class TestLaunch:
    async def test_dispatch_run_id_response_correlates_directly(self):
        fake = FakeGitHub()
        fake.dispatch_mode = "run_id"
        executor = make_executor(fake)

        handle = await executor.launch(make_handle(), inputs={"run_id": FORGE_RUN_ID})

        assert handle.run_id == 501  # the dispatch-returned Actions run id
        (dispatch,) = fake.dispatch_inputs
        assert dispatch["workflow"] == WORKFLOW
        assert dispatch["ref"] == BRANCH  # the factory branch is the ref
        assert dispatch["inputs"] == {
            "run_id": FORGE_RUN_ID,
            "attempt_base_oid": ATTEMPT_BASE,
            "driver": "claude-code",
            "model": "",
            "issue_number": "",
            # Empty default: legacy replay without a journaled plan note —
            # the lane then scans for the plan comment, unenforced (R05).
            "plan_note_id": "",
        }

    async def test_plan_note_id_rides_the_dispatch_inputs(self):
        """R05: the journaled id of the approved plan comment travels as the
        ``plan_note_id`` input — the lane binds its brief to EXACTLY that
        comment instead of heuristically scanning the thread."""
        fake = FakeGitHub()
        fake.dispatch_mode = "run_id"
        executor = make_executor(fake)

        handle = await executor.launch(
            make_handle(),
            inputs={"run_id": FORGE_RUN_ID, "plan_note_id": "1234"},
        )

        assert handle.run_id == 501
        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["plan_note_id"] == "1234"

    async def test_legacy_empty_response_falls_back_to_discovery(self):
        fake = FakeGitHub()
        fake.dispatch_mode = "legacy"
        fake.seed_actions_run(
            run_id=77,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="queued",
            created_at=datetime.now(timezone.utc),
        )
        executor = make_executor(fake)

        handle = await executor.launch(make_handle(), inputs={"run_id": FORGE_RUN_ID})

        assert handle.run_id == 77  # discovered via head_sha + created window
        assert len(fake.dispatch_inputs) == 1  # dispatched exactly once

    async def test_legacy_discovery_refuses_a_mismatched_head_sha(self):
        fake = FakeGitHub()
        fake.dispatch_mode = "legacy"
        fake.seed_actions_run(
            run_id=77,
            head_branch=BRANCH,
            head_sha="e" * 40,  # NOT the attempt base
            status="queued",
        )
        executor = make_executor(fake)

        handle = await executor.launch(make_handle())

        assert handle.run_id == 0  # ordering alone is never trusted (ADR-0020)


# ----------------------------------------------------------------------
# reconcile_launch: ADR-0005 re-entry, never a second dispatch
# ----------------------------------------------------------------------


class TestReconcileLaunch:
    async def test_uncorrelated_handle_is_re_discovered(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=88,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            created_at=STARTED,  # within the started_at window
        )
        executor = make_executor(fake)

        handle = await executor.reconcile_launch(make_handle())

        assert handle.run_id == 88
        assert fake.dispatch_inputs == []  # never dispatches again

    async def test_correlated_handle_is_only_verified(self):
        fake = FakeGitHub()
        fake.seed_actions_run(run_id=501, head_branch=BRANCH, head_sha=ATTEMPT_BASE)
        executor = make_executor(fake)

        handle = await executor.reconcile_launch(make_handle(run_id=501))

        assert handle.run_id == 501
        assert fake.calls_of("get_workflow_run") != []
        assert fake.calls_of("list_workflow_dispatch_runs") == []


# ----------------------------------------------------------------------
# poll: status machine over the run + artifacts
# ----------------------------------------------------------------------


class TestPoll:
    async def test_uncorrelated_handle_keeps_waiting(self):
        executor = make_executor(FakeGitHub())

        outcome = await executor.poll(make_handle())

        assert outcome.status == "running"

    async def test_active_run_is_running_before_the_deadline(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501, head_branch=BRANCH, head_sha=ATTEMPT_BASE, status="in_progress"
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501), now=STARTED + timedelta(seconds=60))

        assert outcome.status == "running"

    async def test_deadline_breach_is_harness_timeout(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501, head_branch=BRANCH, head_sha=ATTEMPT_BASE, status="in_progress"
        )
        executor = make_executor(fake, FORGE_HARNESS_TIMEOUT_SECONDS=600)

        outcome = await executor.poll(make_handle(run_id=501), now=STARTED + timedelta(seconds=601))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"
        assert outcome.reason == "harness_timeout"

    async def test_success_collects_the_candidate_bundle(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="success",
        )
        fake.seed_candidate_artifact(
            501,
            name=artifact_name_for(FORGE_RUN_ID),
            diff_text=create_diff("src/app.py", "print('implemented')\n"),
            meta={
                "attempt_base": ATTEMPT_BASE,
                "run_id": FORGE_RUN_ID,
                "driver": "claude-code",
                "model": "glm-5.3-flash[1m]",
                "exit": "completed",
                "usage": {"input_tokens": 120, "output_tokens": 45},
            },
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.status == "change_candidate"
        bundle = outcome.bundle
        assert bundle.attempt_base_oid == ATTEMPT_BASE
        assert bundle.driver_exit == "completed"
        assert [entry.path for entry in bundle.entries] == ["src/app.py"]
        assert bundle.usage is not None
        assert bundle.usage.input_tokens == 120
        assert bundle.usage.completeness == "aggregate"

    async def test_artifact_base_mismatch_is_rejected(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="success",
        )
        fake.seed_candidate_artifact(
            501,
            name=artifact_name_for(FORGE_RUN_ID),
            diff_text=create_diff("src/app.py", "x\n"),
            meta={"attempt_base": "e" * 40, "exit": "completed"},  # lies about the base
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert outcome.reason.startswith("harness_attempt_base_mismatch")

    async def test_missing_artifact_is_reported(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="success",
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert outcome.reason == "harness_artifact_missing"

    async def test_driver_failure_exit_is_never_adopted(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="success",
        )
        fake.seed_candidate_artifact(
            501,
            name=artifact_name_for(FORGE_RUN_ID),
            diff_text=create_diff("src/app.py", "half done\n"),
            meta={"attempt_base": ATTEMPT_BASE, "exit": "failed"},
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"
        assert outcome.reason.startswith("harness_driver_failed")

    async def test_empty_diff_is_no_changes(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="success",
        )
        fake.seed_candidate_artifact(
            501,
            name=artifact_name_for(FORGE_RUN_ID),
            diff_text="",
            meta={"attempt_base": ATTEMPT_BASE, "exit": "completed"},
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert outcome.reason == "harness_no_changes"

    async def test_cancelled_conclusion_is_infrastructure(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="cancelled",
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"

    async def test_timed_out_conclusion_is_harness_timeout(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="timed_out",
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert outcome.reason == "harness_timeout"

    async def test_startup_failure_is_infrastructure(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="startup_failure",
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"
        assert "startup_failure" in outcome.reason

    async def test_clean_failure_log_classifies_code(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="failure",
        )
        fake.seed_actions_jobs(501, [{"id": 9, "name": "harness", "conclusion": "failure"}])
        fake.seed_job_log(9, "claude: the agent exited with code 1\n")
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "code"
        assert "harness" in outcome.reason

    async def test_quota_pattern_in_log_classifies_infrastructure(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="failure",
        )
        fake.seed_actions_jobs(501, [{"id": 9, "name": "harness", "conclusion": "failure"}])
        fake.seed_job_log(9, "anthropic.AuthenticationError: invalid api key\n")
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"

    async def test_failure_with_no_job_evidence_is_infrastructure(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="failure",
        )
        # No jobs seeded: the evidence blames nobody — infrastructure.
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.failure_kind == "infrastructure"


# ----------------------------------------------------------------------
# cancel: best-effort, tolerant of a finished run (research §4)
# ----------------------------------------------------------------------


class TestCancel:
    async def test_cancel_requests_cancellation(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501, head_branch=BRANCH, head_sha=ATTEMPT_BASE, status="in_progress"
        )
        executor = make_executor(fake)

        await executor.cancel(make_handle(run_id=501))

        assert fake.cancelled_runs == [501]

    async def test_cancel_tolerates_an_already_completed_run(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="success",
        )
        executor = make_executor(fake)

        await executor.cancel(make_handle(run_id=501))  # 409 swallowed

        assert fake.cancelled_runs == []

    async def test_cancel_without_correlation_is_a_noop(self):
        executor = make_executor(FakeGitHub())

        await executor.cancel(make_handle(run_id=0))  # must not raise


# ----------------------------------------------------------------------
# Artifact zip extraction
# ----------------------------------------------------------------------


class TestExtraction:
    async def test_zip_without_the_contract_files_is_rejected(self):
        fake = FakeGitHub()
        fake.seed_actions_run(
            run_id=501,
            head_branch=BRANCH,
            head_sha=ATTEMPT_BASE,
            status="completed",
            conclusion="success",
        )
        fake.seed_actions_artifact(
            501, artifact_name_for(FORGE_RUN_ID), {"unrelated.txt": b"noise"}
        )
        executor = make_executor(fake)

        outcome = await executor.poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert "harness_artifact_invalid" in outcome.reason

    def _zip_bytes_with_prefix(self, prefix: str) -> bytes:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(f"{prefix}candidate.diff", create_diff("a.py", "x\n"))
            archive.writestr(
                f"{prefix}candidate.meta.json",
                json.dumps({"attempt_base": ATTEMPT_BASE, "exit": "completed"}),
            )
        return buffer.getvalue()

    async def test_zip_entries_match_with_or_without_the_forge_prefix(self):
        """upload-artifact@v4 stores paths under the LCA prefix — both shapes
        must extract (research §2)."""
        from forge.execution.github_actions import _extract_candidate

        for prefix in ("", ".forge/", "forge/"):
            diff_text, meta = _extract_candidate(self._zip_bytes_with_prefix(prefix))
            assert meta["exit"] == "completed"
            assert "+x" in diff_text


# ----------------------------------------------------------------------
# Artifact download validation (R16): caps, closed allowlist, meta schema
# gate, bounded retry then blocked
# ----------------------------------------------------------------------


def seed_success(fake: FakeGitHub) -> None:
    """A completed-success Actions run (the candidate-collection path)."""
    fake.seed_actions_run(
        run_id=501,
        head_branch=BRANCH,
        head_sha=ATTEMPT_BASE,
        status="completed",
        conclusion="success",
    )


def v2_meta(diff_text: str, **overrides) -> dict:
    """A schema-v2 meta whose manifest digest matches *diff_text*."""
    meta = {
        "schema_version": 2,
        "run_id": FORGE_RUN_ID,
        "attempt_id": "501:1",
        "attempt_base_oid": ATTEMPT_BASE,
        "driver": "claude-code",
        "model": "glm-5.3-flash[1m]",
        "exit": "completed",
        "manifest_digest": "sha256:" + hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
        "usage": {"input_tokens": 120, "output_tokens": 45, "completeness": "aggregate"},
    }
    meta.update(overrides)
    return meta


class TestArtifactValidation:
    @staticmethod
    def _contract_entries(diff_text: str, meta: dict) -> dict[str, bytes]:
        return {
            "candidate.diff": diff_text.encode("utf-8"),
            "candidate.meta.json": json.dumps(meta).encode("utf-8"),
        }

    async def _poll(self, monkeypatch, entries: dict[str, bytes], **caps: int):
        """Seed a successful run + artifact, shrink any validation cap, poll."""
        fake = FakeGitHub()
        seed_success(fake)
        fake.seed_actions_artifact(501, artifact_name_for(FORGE_RUN_ID), entries)
        for name, value in caps.items():
            monkeypatch.setattr(executor_module, name, value)
        return fake, await make_executor(fake).poll(make_handle(run_id=501))

    async def test_oversized_zip_is_blocked_after_exactly_one_retry(self, monkeypatch):
        diff = create_diff("src/app.py", "print('implemented')\n")
        fake, outcome = await self._poll(
            monkeypatch,
            self._contract_entries(diff, v2_meta(diff)),
            MAX_ARTIFACT_ZIP_BYTES=64,
        )

        assert outcome.status == "failed"
        # Blocked as infrastructure — the artifact delivery broke, which is
        # not the code's fault (never burns a repair cycle).
        assert outcome.failure_kind == "infrastructure"
        assert "harness_artifact_invalid" in outcome.reason
        # Bounded: exactly one retry, no infinite re-download loop.
        assert len(fake.calls_of("download_artifact_zip")) == 2

    async def test_oversized_uncompressed_content_is_rejected(self, monkeypatch):
        diff = create_diff("src/app.py", "print('implemented')\n")
        _, outcome = await self._poll(
            monkeypatch,
            self._contract_entries(diff, v2_meta(diff)),
            MAX_ARTIFACT_CONTENT_BYTES=32,
        )

        assert outcome.status == "failed"
        assert "uncompressed" in outcome.reason

    async def test_too_many_entries_is_rejected(self, monkeypatch):
        diff = create_diff("src/app.py", "x\n")
        entries = self._contract_entries(diff, v2_meta(diff))
        entries["extra.txt"] = b"noise"
        _, outcome = await self._poll(monkeypatch, entries, MAX_ARTIFACT_ENTRIES=2)

        assert "entry cap" in outcome.reason

    async def test_unexpected_extra_entry_is_rejected(self, monkeypatch):
        """The allowlist is closed: an entry outside the contract files
        fails the whole archive (the old matcher just ignored it)."""
        diff = create_diff("src/app.py", "x\n")
        entries = self._contract_entries(diff, v2_meta(diff))
        entries["notes.txt"] = b"noise"
        _, outcome = await self._poll(monkeypatch, entries)

        assert "unexpected entry 'notes.txt'" in outcome.reason

    async def test_duplicate_basename_is_rejected(self, monkeypatch):
        """A shadowing copy of candidate.diff must never let the matcher
        silently pick one of the two (the old next()-first-match flaw)."""
        diff = create_diff("src/app.py", "x\n")
        entries = self._contract_entries(diff, v2_meta(diff))
        entries["forge/candidate.diff"] = diff.encode("utf-8")
        _, outcome = await self._poll(monkeypatch, entries)

        assert "duplicate entry for 'candidate.diff'" in outcome.reason

    async def test_entry_escaping_the_archive_root_is_rejected(self, monkeypatch):
        diff = create_diff("src/app.py", "x\n")
        entries = self._contract_entries(diff, v2_meta(diff))
        entries["../evil.txt"] = b"payload"
        _, outcome = await self._poll(monkeypatch, entries)

        assert "unsafe entry path" in outcome.reason

    async def test_unknown_schema_version_is_rejected(self, monkeypatch):
        diff = create_diff("src/app.py", "x\n")
        meta = v2_meta(diff, schema_version=3)
        _, outcome = await self._poll(monkeypatch, self._contract_entries(diff, meta))

        assert outcome.status == "failed"
        assert "schema_version" in outcome.reason

    async def test_schema_version_junk_string_is_rejected(self, monkeypatch):
        diff = create_diff("src/app.py", "x\n")
        meta = v2_meta(diff, schema_version="latest")
        _, outcome = await self._poll(monkeypatch, self._contract_entries(diff, meta))

        assert "schema_version" in outcome.reason

    async def test_v2_meta_with_matching_digest_is_accepted(self, monkeypatch):
        """Schema v2 round trip: identity + digest + usage receipt pass, and
        the receipt rides into the bundle (R23)."""
        diff = create_diff("src/app.py", "print('implemented')\n")
        _, outcome = await self._poll(monkeypatch, self._contract_entries(diff, v2_meta(diff)))

        assert outcome.status == "change_candidate"
        assert outcome.bundle is not None
        assert outcome.bundle.usage is not None
        assert outcome.bundle.usage.input_tokens == 120
        assert outcome.bundle.usage.completeness == "aggregate"

    async def test_v2_meta_with_a_substituted_diff_is_rejected(self, monkeypatch):
        diff = create_diff("src/app.py", "x\n")
        meta = v2_meta(diff, manifest_digest="sha256:" + "0" * 64)
        _, outcome = await self._poll(monkeypatch, self._contract_entries(diff, meta))

        assert "manifest_digest mismatch" in outcome.reason

    async def test_v2_meta_without_a_digest_is_rejected(self, monkeypatch):
        diff = create_diff("src/app.py", "x\n")
        meta = v2_meta(diff)
        del meta["manifest_digest"]
        _, outcome = await self._poll(monkeypatch, self._contract_entries(diff, meta))

        assert "manifest_digest" in outcome.reason

    async def test_expired_artifact_is_blocked_without_a_download(self):
        fake = FakeGitHub()
        seed_success(fake)
        diff = create_diff("src/app.py", "x\n")
        fake.seed_actions_artifact(
            501, artifact_name_for(FORGE_RUN_ID), self._contract_entries(diff, v2_meta(diff))
        )
        fake.actions_artifacts[501][0]["expired"] = True

        outcome = await make_executor(fake).poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert outcome.failure_kind == "infrastructure"
        assert outcome.reason == "harness_candidate_expired"
        assert fake.calls_of("download_artifact_zip") == []

    async def test_declared_size_over_cap_blocks_before_download(self):
        fake = FakeGitHub()
        seed_success(fake)
        diff = create_diff("src/app.py", "x\n")
        fake.seed_actions_artifact(
            501, artifact_name_for(FORGE_RUN_ID), self._contract_entries(diff, v2_meta(diff))
        )
        fake.actions_artifacts[501][0]["size_in_bytes"] = 10**9

        outcome = await make_executor(fake).poll(make_handle(run_id=501))

        assert outcome.status == "failed"
        assert "over the cap" in outcome.reason
        assert fake.calls_of("download_artifact_zip") == []
