"""Regression tests ported from the d16f523 external review, plus the A14
composed scenario that needs the GitLab service leg.

The reviewer shipped red tests (``test_review_regressions.py`` in
FORGE_REVIEW_d16f523.zip, probes P01-P07 in ``reproduce_review_findings.py``)
written against the production entry points. They are ported here over the
CURRENT production surface — where the product moved (A09 replaced the
string-concatenated ``_CLAUDE_ALLOWED_TOOLS`` with a rule tuple serialized
at the render site), the port drives the new entry and asserts the same
contract. Each ported test names the finding it pins and the probe that
failed before the fix.

The A05 composed scenario (docs/reviews/2026-09-18-d16f523 A14) lives here
because it needs the GitLab service leg: the durable claim loop drives REAL
``execute_run_command`` legs (run creation + plan notes on the fake), two
workers, a batch whose unstarted leases die mid-handler. The conformance
kit imports and drives the same driver, recording the outcome into the
attestation ledger (:data:`forge.runs.conformance.COMPOSED_SCENARIOS`).
"""

import asyncio
import shlex
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.config import ForgeConfig, Settings
from forge.durable import FlowRun, StepRun
from forge.durable.claims import current_claim
from forge.models.base import Base
from forge.worker import steps as step_runtime
from forge.worker.steps import (
    claim_due_steps,
    reschedule_expired_leases,
    run_due_steps,
    schedule_command_step,
)
from tests.fixtures.fake_gitlab import FakeGitLab, FakeGitLabClientFactory

REPO_ROOT = Path(__file__).parent.parent


# ----------------------------------------------------------------------
# Ported: the reviewer's red tests, over the current production surface
# ----------------------------------------------------------------------


def test_github_upload_root_is_non_hidden_or_explicitly_allowed():
    """A08 (probe P01): upload-artifact v4 excludes hidden paths by
    default — a ``.forge-output`` artifact root ships NO candidate at all.
    The template must upload a NON-hidden root (or set
    ``include-hidden-files: true`` explicitly)."""
    path = REPO_ROOT / "ci" / "templates" / "forge-harness.github.yml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    uploads = [
        step
        for step in config["jobs"]["harness"]["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert uploads, "Expected a candidate-upload step."
    for step in uploads:
        values = step.get("with", {})
        explicitly_allowed = str(values.get("include-hidden-files", False)).lower() == "true"
        for raw in str(values.get("path", "")).splitlines():
            name = Path(raw.strip().rstrip("/")).name
            assert not name.startswith(".") or explicitly_allowed, (
                f"Hidden artifact root {raw!r} without include-hidden-files=true"
            )


def test_python_permission_entries_are_individual_rules():
    """A09 (probe P02): the review's test read the concatenated
    ``_CLAUDE_ALLOWED_TOOLS`` literal — the fix replaced it with a rule
    tuple serialized by an explicit ",".join at the RENDER site. The port
    drives that production render (``render_driver_script``) and parses
    the ``--allowedTools`` value the driver actually receives: every
    python interpreter rule must be an INDIVIDUAL comma-separated rule,
    never a glued fragment."""
    from forge.harness_entry import render_driver_script

    script = render_driver_script("claude-code", "glm-5.3-flash[1m]", "brief.md")
    allowed_line = next(
        line.strip().removesuffix("\\") for line in script.splitlines() if "--allowedTools" in line
    )
    tokens = shlex.split(allowed_line)
    assert tokens[0] == "--allowedTools"
    rules = {rule.strip() for rule in tokens[1].split(",")}
    assert "Bash(python3:*)" in rules
    assert "Bash(python:*)" in rules
    assert "Bash(.venv/bin/python:*)" in rules
    assert "Bash(./.venv/bin/python:*)" in rules
    # The P02 glue shape — two rules fused into one unmatchable token —
    # can never come back: no comma-joined rule contains a second "Bash(".
    fused = [rule for rule in rules if rule.count("Bash(") > 1]
    assert fused == []


@pytest.mark.parametrize("resolver_name", ["resolve_status_target", "resolve_retry_target"])
async def test_full_id_resolution_cannot_cross_project(resolver_name: str):
    """A04/A07 (probe P04): a full 32-char run id must enforce the SAME
    subject scope as prefix/bare forms — a run of project 202 is never
    resolved by a command addressed to project 101, even with the exact
    id in hand."""
    from forge.durable.models import FlowRun
    from forge.runs import revival

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        target = FlowRun(
            id="a" * 32,
            provider="gitlab",
            project_id=202,
            issue_iid=7,
            status="blocked",
            candidate_shas=["1" * 40],
        )
        async with session_factory() as session:
            session.add(target)
            await session.commit()

        resolver = getattr(revival, resolver_name)
        async with session_factory() as session:
            crossed = await resolver(
                session,
                provider="gitlab",
                project_id=101,
                issue_iid=7,
                requested=target.id,
                repo_full_name=None,
            )
            assert crossed is None, (
                "The full-ID path must enforce the same project scope as prefix/bare IDs."
            )
            # Positive control: the OWN subject resolves the same id.
            owned = await resolver(
                session,
                provider="gitlab",
                project_id=202,
                issue_iid=7,
                requested=target.id,
                repo_full_name=None,
            )
            assert owned is not None and owned.id == target.id
    finally:
        await engine.dispose()


# ----------------------------------------------------------------------
# A14 composed: leased batch expiry with two workers over the REAL
# GitLab command-execution leg (the durable claim loop, no helpers)
# ----------------------------------------------------------------------

GL_PROJECT_ID = 42
ISSUES = (1, 2, 3, 4, 5)


class _SpyPlanner:
    """Records (issue title, ambient claim owner) per planner call; the
    FIRST call blocks until released — the handler that outlives the
    batch's other leases (probe P05's exact ordering)."""

    def __init__(self, release: Any) -> None:
        self.release = release
        self.entries: list[tuple[str, str]] = []
        self.entered = asyncio.Event()

    async def plan(
        self,
        issue_title: str,
        issue_description: str,
        *,
        flow_run_id: str | None = None,
        path_scope: list[str] | None = None,
    ) -> str:
        claim = current_claim()
        owner = claim.owner if claim is not None else "<unbound>"
        self.entries.append((issue_title, owner))
        if len(self.entries) == 1:
            self.entered.set()
            await self.release.wait()
        return (
            "## Implementation plan\n\n"
            f"- **Issue:** {issue_title}\n"
            "- **Approach:** a single demo file\n"
        )


async def composed_a05_leased_batch_expiry_two_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A05 composed (probe P05): batch=5 claims, the first handler
    outlives the others' 120s leases, a reaper hands the expired steps
    back, a SECOND worker claims and runs them — and when the first
    worker finally reaches its stale claims, none of them re-executes.

    Every executed handler is the REAL ``execute_run_command`` leg
    (``start_run``: a FlowRun row + the plan note on the fake), so the
    invariant is asserted on ACTUAL effects: each subject's run is created
    exactly once and each issue carries exactly ONE plan note — the stale
    owner's duplicate would be a second comment on the fake, not just a
    counter. Failing-before: the old ``run_due_steps`` started queued
    steps with dead claims, double-posting plans under two owners.
    """
    release = asyncio.Event()
    spy = _SpyPlanner(release)

    def fake_agents(*args: Any, **kwargs: Any):
        from forge.runs.stubs import StubImplementer, StubReviewer

        return (spy, StubImplementer(), StubReviewer())

    monkeypatch.setattr("forge.runs.service.build_default_agents", fake_agents)

    fake = FakeGitLab()
    fake.seed_commit("main", "1" * 40, "initial")
    fake.seed_file("src/app.py", "print('hi')\n")
    for issue in ISSUES:
        fake.seed_issue(issue, f"Issue {issue}", "Make it real.")
    monkeypatch.setattr("forge.runs.service.GitLabClient", FakeGitLabClientFactory(shared=fake))

    settings = _driver_settings()
    with tempfile.TemporaryDirectory(prefix="forge-a05-") as tmp:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp}/a05.db",
            connect_args={"check_same_thread": False},
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            db = async_sessionmaker(engine, expire_on_commit=False)

            async with db() as session:
                async with session.begin():
                    for issue in ISSUES:
                        await schedule_command_step(
                            session,
                            {
                                "command": "start_run",
                                "project_id": GL_PROJECT_ID,
                                "issue_iid": issue,
                                "author_username": "alice",
                            },
                            source_event_id=uuid4().hex,
                        )

            # Worker A claims the WHOLE batch (the A05 shape) and blocks in
            # the first handler.
            forge_config = ForgeConfig()
            worker_a = asyncio.create_task(
                run_due_steps(db, settings, forge_config, owner="worker-a", limit=5)
            )
            await asyncio.wait_for(spy.entered.wait(), timeout=5)

            # Steps 2..5's leases die while they sit unstarted behind the
            # blocked first handler; the reaper hands them back.
            async with db() as session:
                claimed_ids = (
                    (
                        await session.execute(
                            select(StepRun.id)
                            .where(StepRun.status == "running")
                            .order_by(StepRun.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            assert len(claimed_ids) == 5
            past = datetime.now(timezone.utc) - timedelta(seconds=1)
            async with db() as session:
                async with session.begin():
                    await session.execute(
                        update(StepRun)
                        .where(StepRun.id.in_(claimed_ids[1:]))
                        .values(lease_expires_at=past)
                    )
            assert await reschedule_expired_leases(db) == 4

            # Worker B (the shipped one-claim-per-pass loop) executes every
            # reaped step under a FRESH claim — real run creation + plan
            # note on each issue.
            for _ in range(4):
                assert await run_due_steps(db, settings, forge_config, owner="worker-b") == 1

            release.set()
            await asyncio.wait_for(worker_a, timeout=5)

            # Each handler entered EXACTLY ONCE, under its CURRENT owner:
            # issue 1 by worker A (its lease never died), issues 2-5 by
            # worker B — worker A never executed a stale claim.
            assert sorted(spy.entries) == [("Issue 1", "worker-a")] + [
                (f"Issue {issue}", "worker-b") for issue in ISSUES[1:]
            ]

            # The ACTUAL effects: one run per subject, one plan note per
            # issue — a stale owner's second entry would duplicate both.
            async with db() as session:
                runs = (await session.execute(select(FlowRun))).scalars().all()
            assert sorted(int(run.issue_iid or 0) for run in runs) == sorted(ISSUES)
            assert len(runs) == len({run.issue_iid for run in runs})
            for issue in ISSUES:
                notes = [note for note in fake.notes if f"Issue {issue}" in note["body"]]
                assert len(notes) == 1, f"issue {issue} must carry exactly one plan note"
            async with db() as session:
                rows = (await session.execute(select(StepRun))).scalars().all()
            assert all(row.status == "succeeded" for row in rows)

            # And the shipped default stays the one-claim pass: nothing
            # new rots on leases (A05's structural fix).
            assert step_runtime.STEP_CLAIM_BATCH == 1
            assert await claim_due_steps(db, "worker-c") == []
        finally:
            await engine.dispose()


def _driver_settings() -> Settings:
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET=SecretStr("whsec"),  # noqa: S105
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )


class TestA05ComposedLeasedBatch:
    async def test_two_workers_never_double_execute_the_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        await composed_a05_leased_batch_expiry_two_workers(monkeypatch)
