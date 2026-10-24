"""Fenced immutable model inputs, conservative outcomes and review cursor CAS."""

from dataclasses import dataclass
from datetime import datetime
import json

from forge_protocol import digest
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.states import Handler
from forge.port_values import Actor, Applied, CommentOutcome, ReviewRef, ReviewVerdict
from forge.review.native_values import NativeReviewInput, NativeReviewPolicy
from forge.store.models_execution import EngineEffect
from forge.store.models_review import EngineReactiveReview, EngineReviewCursor
from forge.store.models_runs import EngineRun
from forge.store.records import Lease, StoreConflict
from forge.store.rounds import review_key
from forge.store.transactions import SessionFactory, fence, stored_time, transaction


@dataclass(frozen=True)
class NativeReviewWork:
    request_id: str
    root_id: str
    review: ReviewRef
    base_sha: str
    head_sha: str
    actor: Actor
    policy: NativeReviewPolicy
    expires_at: datetime
    state: str
    result: ReviewVerdict | None


def comment_key(run_id: str) -> str:
    return f"{run_id}:native_review:comment"


def marker(work: NativeReviewWork) -> str:
    return f"forge:native-review:{work.request_id}:{work.head_sha}"


def comment_body(work: NativeReviewWork, result: ReviewVerdict) -> str:
    return f"Read-only review of `{work.base_sha}` → `{work.head_sha}`.\n\n" + result.body


async def request_row(session: AsyncSession, lease: Lease, now: datetime) -> EngineReactiveReview:
    await fence(session, lease, now)
    row = await session.scalar(
        select(EngineReactiveReview)
        .where(EngineReactiveReview.run_id == lease.run_id)
        .with_for_update()
    )
    if row is None or lease.handler != Handler.FEEDBACK_REVIEW or row.expires_at is None:
        raise StoreConflict("native review request authority is missing")
    return row


async def work_value(session: AsyncSession, row: EngineReactiveReview) -> NativeReviewWork:
    cursor = await session.get(EngineReviewCursor, row.root_review_ref)
    if cursor is None or cursor.root_run_id != row.root_run_id or row.expires_at is None:
        raise StoreConflict("native review cursor scope is corrupt")
    policy = NativeReviewPolicy.model_validate_json(row.policy_json)
    actor = Actor.model_validate_json(row.actor_json)
    review = ReviewRef.model_validate_json(cursor.review_json)
    root = await session.get(EngineRun, row.root_run_id)
    identity = review.repo.identity
    if (
        root is None
        or root.root_run_id != root.id
        or f"{identity.digest}:{review.number}" != row.root_review_ref
        or (root.provider, root.connection_id, root.repository_id)
        != (identity.provider, identity.connection_id, identity.repository_id)
        or (actor.provider, actor.connection_id) != (identity.provider, identity.connection_id)
        or actor.is_bot
    ):
        raise StoreConflict("native review request differs from its root scope")
    if (
        policy.canonical_bytes.decode() != row.policy_json
        or actor.canonical_bytes.decode() != row.actor_json
    ):
        raise StoreConflict("native review request is not canonical")
    return NativeReviewWork(
        row.id,
        row.root_run_id,
        review,
        row.base_sha,
        row.head_sha,
        actor,
        policy,
        stored_time(row.expires_at),
        row.state,
        ReviewVerdict.model_validate_json(row.result_json) if row.result_json else None,
    )


async def comment_proof(
    session: AsyncSession, row: EngineReactiveReview, work: NativeReviewWork
) -> None:
    if work.result is None or row.run_id is None:
        raise StoreConflict("native comment requires a durable model result")
    effect = await session.scalar(
        select(EngineEffect).where(EngineEffect.key == comment_key(row.run_id))
    )
    expected = {
        "subject": work.review.model_dump(mode="json"),
        "body": comment_body(work, work.result),
        "marker": marker(work),
    }
    if (
        effect is None
        or effect.run_id != row.run_id
        or effect.kind != "post_comment"
        or effect.state not in ("committed", "adopted")
        or effect.outcome_json is None
        or json.loads(effect.payload_json) != expected
        or effect.payload_digest != digest(expected)
    ):
        raise StoreConflict("native comment has no exact durable receipt")
    outcome: CommentOutcome = TypeAdapter(CommentOutcome).validate_json(effect.outcome_json)
    if (
        not isinstance(outcome, Applied)
        or outcome.value.kind != "issue_comment"
        or not isinstance(outcome.value.subject, ReviewRef)
        or review_key(outcome.value.subject) != review_key(work.review)
    ):
        raise StoreConflict("native comment belongs to another review")


class NativeReviewStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def load(self, lease: Lease, *, now: datetime) -> NativeReviewWork:
        async with transaction(self.factory) as session:
            return await work_value(session, await request_row(session, lease, now))

    async def inputs(self, lease: Lease, *, now: datetime) -> NativeReviewInput:
        async with transaction(self.factory) as session:
            row = await request_row(session, lease, now)
            work = await work_value(session, row)
            if row.input_json is None or row.state not in (
                "model_requested",
                "model_completed",
                "commented",
            ):
                raise StoreConflict("native model input was not durably requested")
            value = NativeReviewInput.model_validate_json(row.input_json)
            if value != NativeReviewInput(
                request_id=row.id,
                review=work.review,
                base_sha=row.base_sha,
                head_sha=row.head_sha,
                actor=work.actor,
                diff_sha256=value.diff_sha256,
                policy_digest=work.policy.digest,
            ):
                raise StoreConflict("native model input binding is corrupt")
            return value

    async def prepare_input(self, lease: Lease, value: NativeReviewInput, *, now: datetime) -> None:
        async with transaction(self.factory) as session:
            row = await request_row(session, lease, now)
            work = await work_value(session, row)
            if (
                value.request_id,
                value.review,
                value.base_sha,
                value.head_sha,
                value.actor,
                value.policy_digest,
            ) != (row.id, work.review, row.base_sha, row.head_sha, work.actor, work.policy.digest):
                raise StoreConflict("native input differs from its signed request")
            if row.input_json not in (None, value.canonical_bytes.decode()):
                raise StoreConflict("native immutable input changed")
            row.input_json = value.canonical_bytes.decode()

    async def start_model(self, lease: Lease, *, now: datetime) -> bool:
        async with transaction(self.factory) as session:
            row = await request_row(session, lease, now)
            if row.input_json is None:
                raise StoreConflict("native model input must commit before paid I/O")
            if row.state != "accepted":
                return False
            row.state = "model_requested"
            return True

    async def record(self, lease: Lease, result: ReviewVerdict, *, now: datetime) -> None:
        async with transaction(self.factory) as session:
            row = await request_row(session, lease, now)
            if row.input_json is None or result.candidate_sha != row.head_sha:
                raise StoreConflict("native model result names another head")
            if row.state not in ("model_requested", "model_completed"):
                raise StoreConflict("native model was not requested")
            if row.result_json not in (None, result.canonical_bytes.decode()):
                raise StoreConflict("native model result cannot be replaced")
            row.result_json, row.state = result.canonical_bytes.decode(), "model_completed"

    async def commented(self, lease: Lease, *, now: datetime) -> None:
        async with transaction(self.factory) as session:
            row = await request_row(session, lease, now)
            work = await work_value(session, row)
            if work.result is None:
                raise StoreConflict("native comment requires a durable model result")
            await comment_proof(session, row, work)
            row.state = "commented"

    async def advance(self, lease: Lease, *, observed_head: str, now: datetime) -> bool:
        async with transaction(self.factory) as session:
            row = await request_row(session, lease, now)
            cursor = await session.scalar(
                select(EngineReviewCursor)
                .where(EngineReviewCursor.root_review_ref == row.root_review_ref)
                .with_for_update()
            )
            if (
                row.state != "commented"
                or cursor is None
                or row.head_sha != observed_head
                or cursor.root_run_id != row.root_run_id
            ):
                return False
            if cursor.last_reviewed_sha == row.head_sha:
                return True
            if cursor.last_reviewed_sha != row.base_sha:
                return False
            cursor.last_reviewed_sha, cursor.version = row.head_sha, cursor.version + 1
            return True

    async def recoverable(self, limit: int) -> tuple[str, ...]:
        from forge.store.models_runs import EngineRun

        if not 1 <= limit <= 100:
            raise ValueError("native review recovery must be bounded")
        async with self.factory() as session:
            return tuple(
                await session.scalars(
                    select(EngineReactiveReview.id)
                    .join(EngineRun, EngineRun.id == EngineReactiveReview.run_id)
                    .where(
                        EngineReactiveReview.result_json.is_not(None),
                        EngineRun.state == "blocked",
                        EngineRun.cancel_requested.is_(False),
                        EngineRun.reason.in_(
                            ("native_review_comment_unknown", "native_review_read_unavailable")
                        ),
                    )
                    .order_by(EngineReactiveReview.created_at)
                    .limit(limit)
                )
            )

    async def historical(self, request_id: str) -> NativeReviewWork:
        async with self.factory() as session:
            row = await session.get(EngineReactiveReview, request_id)
            if row is None:
                raise StoreConflict("native review request is missing")
            return await work_value(session, row)

    async def adopt_comment(self, request_id: str, *, observed_head: str) -> bool:
        from forge.store.transactions import locked_run

        async with transaction(self.factory) as session:
            row = await session.get(EngineReactiveReview, request_id)
            if row is None or row.run_id is None:
                return False
            await locked_run(session, row.root_run_id)
            run = await locked_run(session, row.run_id)
            await session.refresh(row)
            if run.cancel_requested or run.state != "blocked":
                return False
            work = await work_value(session, row)
            await comment_proof(session, row, work)
            row.state = "commented"
            cursor = await session.get(EngineReviewCursor, row.root_review_ref)
            if (
                cursor is None
                or row.head_sha != observed_head
                or cursor.last_reviewed_sha not in (row.base_sha, row.head_sha)
            ):
                return False
            if cursor.last_reviewed_sha != row.head_sha:
                cursor.last_reviewed_sha, cursor.version = row.head_sha, cursor.version + 1
            run.state, run.reason, run.failed_handler = "ready_for_human", None, None
            run.version += 1
            return True
