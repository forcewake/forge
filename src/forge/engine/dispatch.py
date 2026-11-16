"""Reserve and dispatch one exact approved lane, recovering only its durable nonce."""

from datetime import timedelta
import json
import math

from forge_protocol import LaneDispatch, RunIdentity

from forge.api.lane_auth import LaneAuthService
from forge.budget.service import BudgetService
from forge.domain.budget import BudgetExhausted
from forge.domain.states import Handler
from forge.domain.verdicts import Conflict, Unknown
from forge.engine.effects import Effects
from forge.engine.lane_values import AwaitLane, DispatchInput, LaneSettings, LaneWork
from forge.engine.plan import result_json
from forge.engine.runner import Park, StepContext, StepResult
from forge.port_values import Applied, Refused
from forge.ports import Clock, RepoPort
from forge.store.lane_work import ApprovedLane, LaneWorkStore
from forge.store.records import StepRequest, StoreConflict


def lane_key(identity: RunIdentity) -> str:
    return f"{identity.forge_run_id}:{identity.attempt}:{identity.generation}:lane"


# Checkout and process start happen after the server records the dispatch.
# The lane measures its wall clock from that later start, so the server waits
# this long past the approved limit before parking a lane that has not reported.
def startup_grace_seconds() -> int:
    """Lane install and checkout happen after the server records dispatch."""
    import os

    raw = os.environ.get("FORGE_LANE_STARTUP_GRACE_SECONDS", "600")
    try:
        value = int(raw)
    except ValueError:
        value = 600
    return min(3600, max(30, value))


STARTUP_GRACE_SECONDS = 600


class DispatchHandler:
    def __init__(
        self,
        store: LaneWorkStore,
        auth: LaneAuthService,
        budget: BudgetService,
        clock: Clock,
        settings: LaneSettings,
        repo: RepoPort,
    ) -> None:
        if str(settings.server_url).rstrip("/") != auth.public_url:
            raise ValueError("dispatch server URL differs from its authentication authority")
        self.store, self.auth, self.budget = store, auth, budget
        self.clock, self.settings, self.repo = clock, settings, repo

    async def __call__(self, context: StepContext[Effects]) -> StepResult:
        approved = await self.store.approved(context.lease)
        raw = json.loads(context.payload_json)
        supplied = DispatchInput.model_validate(
            {
                k: raw[k]
                for k in ("approved_digest", "revision", "revivals", "repair", "checkpoint_id")
                if k in raw
            }
        )
        if (supplied.approved_digest, supplied.revision) != (
            approved.brief.approved_digest,
            approved.brief.revision,
        ):
            raise StoreConflict("dispatch input differs from consumed approval")
        identity = RunIdentity(
            **approved.brief.model_dump(include={"forge_run_id", "attempt", "generation"})
        )
        if supplied.repair != approved.brief.repair:
            raise StoreConflict("dispatch repair differs from durable execution authority")
        if "settings" not in raw:
            return self.bootstrap(context, supplied, approved, identity)
        work = LaneWork.model_validate_json(context.payload_json)
        if self.clock.now() >= work.expires_at:
            return StepResult(result_json(reason="dispatch_expired"), park=Park("dispatch_expired"))
        if (
            approved.brief.repair is not None
            or supplied.checkpoint_id is not None
            or approved.root_run_id != identity.forge_run_id
        ) and await self.repo.head(
            approved.repo, f"forge/{approved.root_run_id}"
        ) != approved.brief.execution_base_sha:
            return StepResult(result_json(reason="branch_drift"), park=Park("branch_drift"))
        outcome = await self.reserve(context, work, approved)
        if outcome is not None:
            return outcome
        dispatch = await self.dispatch_value(context, work, approved, identity)
        if (
            dispatch.repair is None
            and dispatch.checkpoint_id is None
            and approved.root_run_id == identity.forge_run_id
        ):
            branch = await context.effects.ensure_branch(
                context.lease,
                f"{approved.root_run_id}:lane_branch",
                approved.repo,
                dispatch.ref,
                approved.brief.base_sha,
            )
            if isinstance(branch, Unknown):
                if context.step.attempts >= work.settings.discovery_attempts:
                    return await self.stop(context, approved, "lane_branch_unavailable")
                return StepResult(
                    result_json(waiting="lane_branch"),
                    defer_until=self.clock.now() + timedelta(seconds=work.settings.poll_seconds),
                )
            if not isinstance(branch, Applied):
                reason = (
                    "branch_drift" if isinstance(branch, Conflict) else "lane_branch_unavailable"
                )
                return await self.stop(context, approved, reason)
        if dispatch.provider == "github":
            await self.register(dispatch, work, approved)
        return await self.launch(context, work, approved, identity, dispatch)

    async def launch(
        self,
        context: StepContext[Effects],
        work: LaneWork,
        approved: ApprovedLane,
        identity: RunIdentity,
        dispatch: LaneDispatch,
    ) -> StepResult:
        effect_key = lane_key(identity) + ":dispatch"
        prior = await context.effects.store.get(effect_key)
        if prior is not None and prior.state in {"unknown", "dispatched"}:
            await context.effects.recover(prior)
        dispatched = await context.effects.dispatch_lane(
            context.lease, effect_key, approved.repo, dispatch
        )
        if isinstance(dispatched, Unknown):
            if context.step.attempts >= work.settings.discovery_attempts:
                return await self.stop(context, approved, "lane_discovery_exhausted")
            return StepResult(
                result_json(waiting="dispatch_probe"),
                defer_until=self.clock.now() + timedelta(seconds=work.settings.poll_seconds),
            )
        if isinstance(dispatched, Refused):
            return await self.stop(
                context, approved, "lane_dispatch_refused:" + dispatched.reason[:240]
            )
        if (
            dispatched.value.repo != approved.repo
            or dispatched.value.correlation_id != dispatch.nonce
        ):
            raise StoreConflict("native dispatch handle differs from its frozen request")
        if dispatch.provider == "gitlab":
            await self.register(dispatch, work, approved, pipeline_id=dispatched.value.native_id)
        grace = startup_grace_seconds()
        deadline = self.clock.now() + timedelta(
            seconds=approved.spec.limits.wallclock_seconds + grace
        )
        waiting = AwaitLane(
            **{**work.model_dump(), "expires_at": deadline}, handle=dispatched.value
        )
        polls = (
            math.ceil((approved.spec.limits.wallclock_seconds + grace) / work.settings.poll_seconds)
            + 2
        )
        successor = StepRequest(
            context.lease.run_id,
            Handler.AWAIT_LANE,
            lane_key(identity) + ":await",
            waiting.canonical_bytes.decode(),
            self.clock.now(),
            max_attempts=polls,
        )
        return StepResult(
            result_json(handle=dispatched.value.model_dump(mode="json")), successors=(successor,)
        )

    def bootstrap(
        self,
        context: StepContext[Effects],
        supplied: DispatchInput,
        approved: ApprovedLane,
        identity: RunIdentity,
    ) -> StepResult:
        work = LaneWork(
            **supplied.model_dump(),
            settings=self.settings,
            expires_at=self.clock.now() + timedelta(seconds=approved.spec.limits.wallclock_seconds),
        )
        successor = StepRequest(
            context.lease.run_id,
            Handler.DISPATCH,
            lane_key(identity) + ":start",
            work.canonical_bytes.decode(),
            self.clock.now(),
            max_attempts=work.settings.discovery_attempts,
        )
        return StepResult(result_json(prepared=identity.model_dump()), successors=(successor,))

    async def reserve(
        self, context: StepContext[Effects], work: LaneWork, approved: ApprovedLane
    ) -> StepResult | None:
        try:
            if approved.spec.metering == "copilot-scoped-unmetered":
                await self.budget.copilot(context.lease, approved.spec)
                return None
            outcome = await self.budget.prepare(
                context.lease,
                approved.spec,
                "implementation",
                work.settings.requested,
                work.settings.minimum,
                work.expires_at,
            )
        except BudgetExhausted:
            return StepResult(result_json(reason="budget_exhausted"), park=Park("budget_exhausted"))
        if isinstance(outcome, Applied):
            return None
        reason = "gateway_outcome_unknown" if isinstance(outcome, Unknown) else outcome.reason
        return StepResult(result_json(reason=reason), park=Park(reason[:300]))

    async def dispatch_value(
        self,
        context: StepContext[Effects],
        work: LaneWork,
        approved: ApprovedLane,
        identity: RunIdentity,
    ) -> LaneDispatch:
        pending = await self.store.pending(identity)
        original = pending.dispatch if pending is not None else None
        if original is None:
            prior = await context.effects.store.get(lane_key(identity) + ":dispatch")
            if prior is not None:
                original = LaneDispatch.model_validate_json(
                    json.loads(prior.payload_json)["dispatch_json"]
                )
        expected = LaneDispatch(
            **identity.model_dump(),
            server_url=work.settings.server_url,
            provider=approved.repo.identity.provider,
            repository=approved.repo.full_name,
            ref=f"forge/{approved.root_run_id}",
            base_sha=approved.brief.base_sha,
            approved_digest=approved.brief.approved_digest,
            driver=approved.spec.driver,
            model=approved.spec.model,
            timeout_seconds=approved.spec.limits.wallclock_seconds,
            nonce=original.nonce if original is not None else self.auth.new_nonce(),
            repair=approved.brief.repair,
            checkpoint_id=work.checkpoint_id,
        )
        if original is not None and original != expected:
            raise StoreConflict("recovered dispatch differs from exact frozen lane work")
        return expected

    async def register(
        self,
        dispatch: LaneDispatch,
        work: LaneWork,
        approved: ApprovedLane,
        pipeline_id: str | None = None,
    ) -> None:
        await self.auth.register_pending(
            dispatch,
            approved.brief,
            expires_at=work.expires_at,
            connection_id=approved.repo.identity.connection_id,
            github_workflow_ref=(
                f"{approved.repo.full_name}/{work.settings.github_workflow_path}@refs/heads/{dispatch.ref}"
                if dispatch.provider == "github"
                else None
            ),
            github_workflow_claim=work.settings.github_workflow_claim,
            gitlab_pipeline_id=pipeline_id,
        )

    async def stop(
        self, context: StepContext[Effects], approved: ApprovedLane, reason: str
    ) -> StepResult:
        settlement = (
            "unmetered"
            if approved.spec.metering == "copilot-scoped-unmetered"
            else await self.budget.finish(context.lease, "implementation")
        )
        return StepResult(result_json(reason=reason, settlement=settlement), park=Park(reason))
