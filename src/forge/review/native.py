"""Only configured signed human-push facts may queue a read-only native review."""

from collections.abc import Callable
import re

from forge.command_values import CommandResult
from forge.ingress_values import IngressEvent
from forge.port_values import ReadUnavailable, RepoRef, ReviewRef
from forge.ports import ActorPort, Clock, RepoPort, ReviewPort
from forge.review.native_values import NativeReviewPolicy
from forge.store.native_review_admission import NativeReviewAdmission
from forge.store.records import StoreConflict
from forge.store.rounds import review_key


class ReactiveReviews:
    def __init__(
        self,
        admission: NativeReviewAdmission,
        repositories: RepoPort,
        reviews: ReviewPort,
        clock: Clock,
        actor_for: Callable[[RepoRef], ActorPort],
        policy_for: Callable[[RepoRef], NativeReviewPolicy | None],
    ) -> None:
        self.admission, self.repositories, self.reviews = admission, repositories, reviews
        self.clock, self.actor_for, self.policy_for = clock, actor_for, policy_for

    async def submit(self, event: IngressEvent) -> CommandResult:
        review, head, actor = event.target, event.head_sha, event.actor
        policy = self.policy_for(event.repo)
        if policy is None:
            return CommandResult(status="observed", reason="native_review_disabled")
        bot = self.actor_for(event.repo).bot_identity()
        if (
            actor.is_bot
            or actor.native_id == bot.native_id
            or actor.login.casefold() == bot.login.casefold()
            or re.fullmatch(r"(?:project|group)_[0-9]+_bot_.+", actor.login)
        ):
            return CommandResult(status="observed", reason="bot_authored")
        identity = event.repo.identity
        if (
            not isinstance(review, ReviewRef)
            or review.repo.identity != identity
            or head is None
            or (actor.provider, actor.connection_id) != (identity.provider, identity.connection_id)
            or (identity.provider, event.event, event.action)
            not in {
                ("github", "pull_request", "synchronize"),
                ("gitlab", "merge_request", "update"),
            }
            or not event.delivery_key.startswith(
                identity.provider + ":" + identity.connection_id + ":"
            )
        ):
            return CommandResult(status="refused", reason="native_push_fact_required")
        try:
            draft = await self.reviews.find_draft(review.repo, await self.branch(review))
            if (
                draft is None
                or review_key(draft) != review_key(review)
                or await self.reviews.review_head(review) != head
            ):
                return CommandResult(status="refused", reason="native_review_head_drift")
            result = await self.admission.admit(review, head, actor, policy, now=self.clock.now())
            await self.admission.activate_pending(now=self.clock.now())
            return result
        except StoreConflict as error:
            return CommandResult(status="refused", reason=str(error))
        except ReadUnavailable:
            return CommandResult(status="refused", reason="native_review_unavailable")

    async def branch(self, review: ReviewRef) -> str:
        from forge.store.native_review_admission import find_root
        from forge.store.transactions import transaction

        async with transaction(self.admission.factory) as session:
            return "forge/" + (await find_root(session, review)).id

    async def activate_pending(self, *, limit: int = 20) -> int:
        return await self.admission.activate_pending(now=self.clock.now(), limit=limit)
