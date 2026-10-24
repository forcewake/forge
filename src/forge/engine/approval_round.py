"""A child correction re-proves native feedback instead of comparing augmented text."""

from forge.engine.rounds import RoundActions, native_note
from forge.port_values import TaskSnapshot
from forge.ports import ActorPort, Clock, RepoPort, ReviewPort
from forge.store.approval_values import ApprovalClaim
from forge.store.records import RunRecord
from forge.store.rounds import RoundStore


async def original_round_task(
    store: RoundStore,
    run: RunRecord,
    step_id: str,
    claim: ApprovalClaim,
    repositories: RepoPort,
    reviews: ReviewPort,
    actors: ActorPort,
    clock: Clock,
) -> TaskSnapshot:
    window, fixing, approving = await store.approval_window(run.id, step_id, claim)
    observed = await RoundActions(store, repositories, reviews, clock, lambda repo: actors).observe(
        approving, run, correction=False
    )
    if observed != window:
        from forge.store.records import StoreConflict

        raise StoreConflict("correction authority changed during approval")
    native_note(await reviews.list_feedback(window.review, None), fixing)
    return window.intent.task
