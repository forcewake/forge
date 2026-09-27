"""R41-07 follow-up — the exactly-once Draft-MR window under CONCURRENT
creators (the CI failure run 36320912690, ``finite-builtin-initial-noredis``:
``ValueError: too many values to unpack`` — the arm's ``(mr,) =
gitlab_native.merge_requests().values()`` found TWO identical Draft MRs).

The failure was NOT a lease expiry: the shipped step executor already
heartbeats its lease through long awaits (``_heartbeat`` in
``forge.worker.steps.execute_claimed_step``), and the worker runs its
claims sequentially, so a reaped-and-reclaimed step cannot overlap its
zombie. The real hole was the PUBLISHER: two concurrent creators can
legitimately be inside ``RunService._create_draft_mr`` at once — the
run's own publication leg and the publication-intent recovery scanner
finishing an adopted commit's walk (``evaluate_publication_intents``
creates the Draft MR itself when the crashed leg left none journaled).
The reservation row lock was the only serializer and cannot close the
window: SQLite ignores ``FOR UPDATE``, and even a queued creator's
JOURNAL and PROVIDER checks can complete before the winner commits, so
the queued creator proceeds on stale reads and POSTs a second,
identical MR (the LIVE-found comment at the scanner — "two
create_merge_request calls for one intent" — documented the same shape
on 2026-09-20).

This module reproduces the interleaving DETERMINISTICALLY with injected
provider latency (the fake native server's ``__ctl/set_latency``) and
asserts the R11 fix: the create now carries a durable
PublicationIntent (stable operation key, committed BEFORE any provider
I/O), so concurrent creators collapse on the row — the minter creates,
the other adopts, exactly ONE native effect.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from forge.durable import FlowStatus
from forge.durable.models import ActionLog, FlowRun, PublicationIntent

from .conftest import (
    GL_ISSUE_DESC,
    GL_ISSUE_IID,
    GL_ISSUE_TITLE,
    GL_PROJECT_ID,
    gl_settings,
    make_gitlab_service,
)

pytestmark = pytest.mark.production_entry

#: The approved write scope (the builtin validation reads it natively).
CP_FORGE_YML = "implement:\n  paths:\n    - forge-demo/**\n"


async def _provider_posts(gitlab_native, path: str) -> int:
    """How many POSTs hit one project path (the native-effect count)."""
    return sum(
        1
        for entry in gitlab_native.state()["request_log"]
        if entry["method"] == "POST" and entry["path"] == path
    )


class TestConcurrentDraftMrCreators:
    async def test_the_scanner_racing_the_leg_creates_exactly_one_mr(
        self, pe_db, gitlab_native, gitlab_client
    ):
        """The CI interleaving, deterministically: the commit's slow
        response opens the scanner's adopt window; the scanner and the leg
        both enter the create with checks the other's in-flight create
        cannot answer; ONE Draft MR exists afterwards — the loser adopted
        the winner's effect through the durable create intent, never
        POSTed its own (pre-fix: two identical MRs, reproduced 2× here
        before the fix landed)."""
        factory = pe_db.worker_factory()
        settings = gl_settings(
            FORGE_IMPLEMENTER_BACKEND="builtin",
            FORGE_APPROVERS="alice",
            DATABASE_URL=pe_db.url,
            GITLAB_URL=gitlab_native.base_url,
            FORGE_REQUIRED_JOBS="verify",
        )
        service = make_gitlab_service(factory, gitlab_client, settings=settings)
        gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
        gitlab_native.seed_file(".forge.yml", CP_FORGE_YML)

        run_id = await service.start_run(
            GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
        )

        # The latency shapes the CI runner's interleaving:
        # - the candidate commit REGISTERS immediately but its response is
        #   slow (the R11 lost-response window) — the recovery scanner's
        #   tick lands inside it and ADOPTS the commit;
        # - the SECOND MR-list read (the one inside the scanner's own
        #   create transaction) is slow, so the scanner's journal/provider
        #   checks go stale while the leg's create is already in flight;
        # - every MR create is a slow in-flight request — the effect lands
        #   at the END, so a racing reader's list cannot see it.
        gitlab_native.set_latency(
            [
                {
                    "method": "POST",
                    "tail": ["repository", "commits"],
                    "phase": "response",
                    "ms": 5000,
                },
                {
                    "method": "GET",
                    "tail": ["merge_requests"],
                    "phase": "request",
                    "ms": 3000,
                    "nth": 2,
                },
                {
                    "method": "POST",
                    "tail": ["merge_requests"],
                    "phase": "request",
                    "ms": 2500,
                },
            ]
        )

        async def commit_dispatched() -> bool:
            async with factory() as session:
                row = (
                    await session.execute(
                        select(PublicationIntent.id).where(
                            PublicationIntent.run_id == run_id,
                            PublicationIntent.operation == "commit",
                            PublicationIntent.status == "dispatched",
                        )
                    )
                ).first()
                return row is not None

        async def scanner() -> None:
            # ONE recovery pass, fired inside the leg's commit-response
            # window — exactly when the reconciler's 0.3s tick lands on a
            # slow runner.
            while not await commit_dispatched():
                await asyncio.sleep(0.02)
            await asyncio.sleep(2.9)
            await service.evaluate_publication_intents()

        recovery = asyncio.create_task(scanner())
        try:
            # The leg itself — the same call the /go step executes.
            await service.handle_command_note(
                GL_PROJECT_ID,
                f"@forge /go {run_id}",
                "alice",
                GL_ISSUE_IID,
                author_user_id=11,
            )
        finally:
            await asyncio.wait_for(recovery, timeout=30)
            gitlab_native.set_latency([])

        # --- THE INVARIANT: exactly ONE native create effect -------------
        mrs = gitlab_native.merge_requests()
        assert len(mrs) == 1, f"duplicated Draft MRs: {sorted(mrs)}"
        assert await _provider_posts(gitlab_native, "merge_requests") == 1

        # The run completed its walk on the single effect.
        async with factory() as session:
            run = await session.get(FlowRun, run_id)
            assert run is not None
            assert run.status == FlowStatus.WAITING_CI.value
            assert int(run.mr_iid) == next(iter(mrs))
            # The durable create-once evidence: the create's publication
            # intent resolved terminally, exactly one for the identity.
            intents = (
                (
                    await session.execute(
                        select(PublicationIntent.status).where(
                            PublicationIntent.run_id == run_id,
                            PublicationIntent.operation == "create_merge_request",
                        )
                    )
                )
                .scalars()
                .all()
            )
            creates = (
                (
                    await session.execute(
                        select(ActionLog.status).where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == "create_merge_request",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert intents == ["committed"], intents
        assert creates.count("succeeded") >= 1  # the winner's journal row

    async def test_two_direct_concurrent_creators_collapse_to_one_mr(
        self, pe_db, gitlab_native, gitlab_client
    ):
        """The same collapse with the creators started SYMMETRICALLY — no
        scanner timing at all: two concurrent ``_create_draft_mr`` calls
        (the leg and its recovery driver are both legitimate callers) race
        on a fresh run+branch; the durable intent decides at the DB — one
        POST, one MR, both callers return the SAME iid."""
        factory = pe_db.worker_factory()
        settings = gl_settings(
            FORGE_IMPLEMENTER_BACKEND="builtin",
            FORGE_APPROVERS="alice",
            DATABASE_URL=pe_db.url,
            GITLAB_URL=gitlab_native.base_url,
        )
        service = make_gitlab_service(factory, gitlab_client, settings=settings)
        gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
        gitlab_native.seed_file(".forge.yml", CP_FORGE_YML)
        run_id = await service.start_run(
            GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
        )
        gitlab_native.seed_commit("factory/race/branch", "f" * 40, "seeded base")

        # Both creators race with the same identity; the MR create itself
        # is slow so the loser genuinely reaches its decision point while
        # the winner's effect is still in flight.
        gitlab_native.set_latency(
            [{"method": "POST", "tail": ["merge_requests"], "phase": "request", "ms": 1500}]
        )
        try:
            first, second = await asyncio.gather(
                service._create_draft_mr(GL_PROJECT_ID, run_id, "factory/race/branch", "a" * 40),
                service._create_draft_mr(GL_PROJECT_ID, run_id, "factory/race/branch", "a" * 40),
            )
        finally:
            gitlab_native.set_latency([])

        assert first == second  # one MR, one iid, both callers agree
        assert len(gitlab_native.merge_requests()) == 1
        assert await _provider_posts(gitlab_native, "merge_requests") == 1
