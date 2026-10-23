"""Durable publication lessons retained from the retired two-writer saga.

ADR-0035 removes the multi-repository coordinator. These tests exercise the
shipped single-run publisher instead: the real GitLab HTTP client, actual
intent/action rows and a fresh database engine after each simulated death.
No replacement saga, consumer ordering or atomic multi-repository outcome is
claimed. The historical filename remains because its ledger row is retained.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from forge.durable import ActionLog, FlowRun, PublicationIntent
from forge.repository import Change, ChangeSet, ChangesetWriter, Operation, WriteOutcome
from forge.repository.writer import BranchDriftError
from forge.runs.candidate import bundle_from_changeset
from forge.runs.publisher import publish_candidate

from .conftest import GL_BASE_SHA, GL_ISSUE_IID, GL_PROJECT_ID

pytestmark = pytest.mark.production_entry


class ProcessDied(BaseException):
    """A failpoint bypassing ordinary provider-error recovery."""


async def _new_run(factory, *, project_id=GL_PROJECT_ID, base_sha=GL_BASE_SHA):
    run = FlowRun(
        id=uuid4().hex,
        project_id=project_id,
        issue_iid=GL_ISSUE_IID,
        provider="gitlab",
        base_sha=base_sha,
        status="accepted",
    )
    async with factory() as session:
        session.add(run)
        await session.commit()
    return run


def _changeset(run, branch):
    return ChangeSet(
        branch=branch,
        commit_message=f"forge: implement {run.issue_iid}",
        changes=[Change("forge-demo/candidate.md", Operation.CREATE, "candidate\n")],
    )


async def _publish(factory, client, run, branch, *, writer=None):
    return await publish_candidate(
        gitlab=client,
        session_factory=factory,
        writer=writer or ChangesetWriter(client, factory, run.project_id),
        run=run,
        bundle=bundle_from_changeset(_changeset(run, branch), attempt_base_oid=run.base_sha),
        branch=branch,
    )


async def _intent(factory, run_id):
    async with factory() as session:
        return (
            await session.scalars(
                select(PublicationIntent).where(PublicationIntent.run_id == run_id)
            )
        ).one()


def _commit_posts(native):
    return [
        entry
        for entry in native.state()["request_log"]
        if entry["method"] == "POST" and entry["path"] == "repository/commits"
    ]


class TestSingleRunDurablePublication:
    @pytest.mark.parametrize("boundary", ["before_dispatch", "after_provider", "after_journal"])
    async def test_death_at_actual_writer_boundaries_never_duplicates(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, boundary
    ):
        factory = pe_db.worker_factory()
        run = await _new_run(factory)
        branch = f"factory/{run.issue_iid}/{run.id[:8]}"
        writer = ChangesetWriter(gitlab_client, factory, run.project_id)
        seen = {}

        async def die_before_dispatch(*args, **kwargs):
            # The writer has committed both journal rows before reaching its
            # dispatch method. Read with another engine, not its transaction.
            fresh = pe_db.worker_factory()
            intent = await _intent(fresh, run.id)
            async with fresh() as session:
                action = (await session.scalars(select(ActionLog))).one()
            assert intent.status == "dispatched"
            assert intent.expected_parent_oid == GL_BASE_SHA
            assert action.status == "requested"
            seen["key"] = intent.operation_key
            raise ProcessDied

        original_succeed = writer._succeed

        async def die_at_outcome(*args, **kwargs):
            intent = await _intent(pe_db.worker_factory(), run.id)
            seen["key"] = intent.operation_key
            assert intent.status == "dispatched"
            assert len(_commit_posts(gitlab_native)) == 1
            if boundary == "after_journal":
                await original_succeed(*args, **kwargs)
            raise ProcessDied

        if boundary == "before_dispatch":
            monkeypatch.setattr(writer, "_dispatch", die_before_dispatch)
        else:
            monkeypatch.setattr(writer, "_succeed", die_at_outcome)
        with pytest.raises(ProcessDied):
            await _publish(factory, gitlab_client, run, branch, writer=writer)

        fresh = pe_db.worker_factory()
        persisted = await _intent(fresh, run.id)
        assert persisted.operation_key == seen["key"]
        if boundary == "before_dispatch":
            assert _commit_posts(gitlab_native) == []
            result = await _publish(fresh, gitlab_client, run, branch)
            assert result.settling and not result.ok
            assert (await _intent(fresh, run.id)).status == "probing"
            assert _commit_posts(gitlab_native) == []
        elif boundary == "after_provider":
            landed = gitlab_native.branch_head(branch)
            restarted = ChangesetWriter(gitlab_client, fresh, run.project_id)
            result = await restarted.apply(
                run.id,
                _changeset(run, branch),
                start_ref=run.base_sha,
                expected_head=run.base_sha,
            )
            assert result.outcome is WriteOutcome.COMMITTED and result.commit_sha == landed
            adopted = await _intent(fresh, run.id)
            assert adopted.status == "adopted"
            assert adopted.provider_object_id == landed
            assert len(_commit_posts(gitlab_native)) == 1
        else:
            assert persisted.status == "committed"
            # A completed dispatch is not reissued by a stale caller. The
            # frozen base guard refuses that caller rather than force-writing.
            restarted = ChangesetWriter(gitlab_client, fresh, run.project_id)
            with pytest.raises(BranchDriftError):
                await restarted.apply(
                    run.id,
                    _changeset(run, branch),
                    start_ref=run.base_sha,
                    expected_head=run.base_sha,
                )
            assert len(_commit_posts(gitlab_native)) == 1
        assert gitlab_native.unknown_paths() == []

    async def test_negative_probe_stays_unknown_without_a_blind_replay(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        factory = pe_db.worker_factory()
        run = await _new_run(factory)
        branch = f"factory/{run.issue_iid}/{run.id[:8]}"
        writer = ChangesetWriter(gitlab_client, factory, run.project_id)

        async def die(*args, **kwargs):
            raise ProcessDied

        monkeypatch.setattr(writer, "_dispatch", die)
        with pytest.raises(ProcessDied):
            await _publish(factory, gitlab_client, run, branch, writer=writer)
        for attempt in range(4):
            fresh = pe_db.worker_factory()
            result = await _publish(fresh, gitlab_client, run, branch)
            intent = await _intent(fresh, run.id)
            if attempt < 3:
                assert result.settling and intent.status == "probing"
                async with fresh() as session:
                    row = await session.get(PublicationIntent, intent.id)
                    row.next_probe_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    await session.commit()
            else:
                assert result.unknown_outcome and not result.ok
                assert intent.status == "unknown"
                assert intent.remote_result["reason"] == "settle_window_exhausted"
                assert "operator" in intent.remote_result["operator_instruction"]
        assert _commit_posts(gitlab_native) == []
        assert gitlab_native.branch_head(branch) == GL_BASE_SHA

    async def test_human_edit_after_intent_is_preserved_on_recovery(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        factory = pe_db.worker_factory()
        run = await _new_run(factory)
        branch = f"factory/{run.issue_iid}/{run.id[:8]}"
        writer = ChangesetWriter(gitlab_client, factory, run.project_id)

        async def die(*args, **kwargs):
            raise ProcessDied

        monkeypatch.setattr(writer, "_dispatch", die)
        with pytest.raises(ProcessDied):
            await _publish(factory, gitlab_client, run, branch, writer=writer)
        human = await gitlab_client.create_commit(
            run.project_id,
            branch,
            [{"action": "create", "file_path": "human.md", "content": "keep me\n"}],
            "human edit",
        )
        result = await _publish(pe_db.worker_factory(), gitlab_client, run, branch)
        assert not result.ok and result.reason.startswith("branch_drift:")
        assert gitlab_native.branch_head(branch) == human["id"]
        assert len(_commit_posts(gitlab_native)) == 1  # only the human's commit
        assert (await _intent(pe_db.worker_factory(), run.id)).status == "duplicated"
        assert (
            await gitlab_client.read_blob(run.project_id, "human.md", ref=branch)
        ).content == "keep me\n"
