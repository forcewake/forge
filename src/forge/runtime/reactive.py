"""Only configured native push facts activate separately bounded read-only work."""

from dataclasses import dataclass

from forge.engine.effects import Effects
from forge.engine.native_review import NativeReviewReconciler
from forge.port_values import RepoRef
from forge.ports import Clock
from forge.review.native import ReactiveReviews
from forge.review.native_values import NativeReviewPolicy
from forge.runtime.routing import Registry
from forge.runtime.settings import RuntimeSettings
from forge.store.native_review_admission import NativeReviewAdmission
from forge.store.native_reviews import NativeReviewStore
from forge.store.transactions import SessionFactory


@dataclass(frozen=True)
class ReactiveServices:
    reviews: ReactiveReviews
    reconciler: NativeReviewReconciler

    async def tick(self) -> int:
        recovered = await self.reconciler.tick(limit=20)
        return recovered + await self.reviews.activate_pending(limit=20)


def reactive_services(
    config: RuntimeSettings,
    factory: SessionFactory,
    ports: Registry,
    effects: Effects,
    clock: Clock,
) -> ReactiveServices:
    policies = {
        connection.ref(repository).identity: repository.native_review
        for connection in config.connections
        for repository in connection.repositories
    }

    def policy_for(repo: RepoRef) -> NativeReviewPolicy | None:
        ports.for_repo(repo)
        return policies[repo.identity]

    return ReactiveServices(
        ReactiveReviews(
            NativeReviewAdmission(factory), ports, ports, clock, ports.actor_for, policy_for
        ),
        NativeReviewReconciler(NativeReviewStore(factory), ports, ports, effects, clock),
    )
