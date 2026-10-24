"""Bind a read-only push reviewer to the durable requested head and current lease."""

from collections.abc import Callable
import hashlib

from forge_protocol import RunIdentity

from forge.budget.server_models import ServerModels
from forge.budget.service import alias_for
from forge.domain.states import Handler
from forge.port_values import ReviewVerdict
from forge.ports import Clock
from forge.review.native_reviewer import NativeTextReviewer
from forge.review.native_values import NativeReviewInput, ReadOnlyReviewer
from forge.review.reviewer import ReviewLimits
from forge.store.native_reviews import NativeReviewStore
from forge.store.records import Lease, StoreConflict


class ServerNativeReviewers:
    def __init__(
        self,
        models: ServerModels,
        inputs: NativeReviewStore,
        clock: Clock,
        *,
        limits: ReviewLimits = ReviewLimits(),
    ) -> None:
        self.models, self.inputs, self.clock, self.limits = models, inputs, clock, limits

    def __call__(self, current: Callable[[], Lease], alias: str) -> ReadOnlyReviewer:
        return _NativeStep(self, current, alias)


class _NativeStep:
    def __init__(
        self, service: ServerNativeReviewers, current: Callable[[], Lease], alias: str
    ) -> None:
        self.service, self.current, self.alias = service, current, alias

    async def review(self, diff: bytes, request: NativeReviewInput) -> ReviewVerdict:
        service, lease = self.service, self.current()
        identity = RunIdentity(
            forge_run_id=lease.run_id, attempt=lease.attempt, generation=lease.generation
        )
        if lease.handler != Handler.FEEDBACK_REVIEW or self.alias != alias_for(identity, "closing"):
            raise StoreConflict("native reviewer requires its exact read-only step grant")
        persisted = await service.inputs.inputs(lease, now=service.clock.now())
        work = await service.inputs.load(self.current(), now=service.clock.now())
        if (
            persisted != request
            or request.diff_sha256 != hashlib.sha256(diff).hexdigest()
            or request.policy_digest != work.policy.digest
            or work.state != "model_requested"
            or work.result is not None
            or work.expires_at <= service.clock.now()
        ):
            raise StoreConflict("native reviewer input differs from its durable requested head")
        model = service.models.client(
            self.current, alias=self.alias, model=work.policy.budget.model, purpose="closing"
        )
        return await NativeTextReviewer(
            work.policy, model, credential_alias=self.alias, limits=service.limits
        ).review(diff, request)
