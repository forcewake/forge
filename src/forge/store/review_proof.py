"""Exact native closing-review receipts on the existing effect journal."""

import json

from forge_protocol import digest
from pydantic import TypeAdapter, ValidationError

from forge.port_values import Applied, CommentOutcome, ReviewBody, ReviewRef, ReviewVerdict
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.records import Lease


def review_proof(
    lease: Lease,
    review: ReviewRef,
    result: ReviewVerdict,
    effect: EngineEffect | None,
    owner: EngineStep | None,
) -> bool:
    if (
        effect is None
        or owner is None
        or effect.step_id != owner.id
        or effect.kind != "post_review"
        or effect.run_id != lease.run_id
        or effect.state not in ("committed", "adopted")
        or effect.outcome_json is None
        or owner.handler != "review"
        or owner.state != "succeeded"
        or (owner.run_id, owner.attempt, owner.generation, owner.cancellation_generation)
        != (lease.run_id, lease.attempt, lease.generation, lease.cancellation_generation)
    ):
        return False
    expected = {
        "review": review.model_dump(mode="json"),
        "body_json": ReviewBody(**result.model_dump(exclude={"evidence"})).model_dump_json(),
        "marker": f"<!-- forge:{lease.run_id}:review:{result.candidate_sha} -->",
    }
    try:
        outcome: CommentOutcome = TypeAdapter(CommentOutcome).validate_json(effect.outcome_json)
        return (
            json.loads(effect.payload_json) == expected
            and effect.payload_digest == digest(expected)
            and isinstance(outcome, Applied)
            and outcome.value.kind == "review"
            and isinstance(outcome.value.subject, ReviewRef)
            and outcome.value.subject.repo.identity == review.repo.identity
            and outcome.value.subject.number == review.number
        )
    except (ValueError, TypeError, ValidationError):
        return False
