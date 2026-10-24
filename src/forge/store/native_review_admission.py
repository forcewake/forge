"""Queue signed native heads and activate one real read-only review at a time."""

from datetime import datetime, timedelta
import json
from uuid import UUID, uuid5

from forge_protocol import digest
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import CommandResult
from forge.domain.states import Handler, State, TERMINAL
from forge.port_values import Actor, Applied, CommentOutcome, ReviewRef, ReviewVerdict
from forge.review.native_values import NativeReviewPolicy
from forge.store.models_execution import EngineCandidate, EngineEffect
from forge.store.models_review import EngineReactiveReview, EngineReviewCursor
from forge.store.models_runs import EngineRun
from forge.store.records import RunRequest, StepRequest, StoreConflict
from forge.store.rounds import review_key, window_in
from forge.store.runs import subject_key
from forge.store.steps import enqueue_in
from forge.store.transactions import SessionFactory, aware, locked_run, transaction


async def closing_seed(session: AsyncSession, root_id: str, review: ReviewRef) -> str:
    window = await window_in(session, root_id, review, correction=False)
    candidate = await session.scalar(
        select(EngineCandidate).where(
            EngineCandidate.run_id == window.parent_id,
            EngineCandidate.published_sha == window.base_sha,
            EngineCandidate.state == "published",
        )
    )
    if candidate is None:
        raise StoreConflict("native_review_has_no_reviewed_base")
    key = f"{candidate.run_id}:{candidate.attempt}:{candidate.generation}:review"
    effect = await session.scalar(select(EngineEffect).where(EngineEffect.key == key))
    if (
        effect is None
        or effect.kind != "post_review"
        or effect.run_id != candidate.run_id
        or effect.state not in ("committed", "adopted")
        or effect.outcome_json is None
        or digest(json.loads(effect.payload_json)) != effect.payload_digest
    ):
        raise StoreConflict("native_review_has_no_reviewed_base")
    result = ReviewVerdict.model_validate_json(
        json.dumps(json.loads(candidate.validation_json or "{}").get("review"))
    )
    payload = json.loads(effect.payload_json)
    outcome: CommentOutcome = TypeAdapter(CommentOutcome).validate_json(effect.outcome_json)
    if (
        result.candidate_sha != window.base_sha
        or json.loads(payload.get("body_json", "null"))
        != result.model_dump(mode="json", exclude={"evidence"})
        or review_key(ReviewRef.model_validate(payload.get("review"))) != review_key(review)
        or not isinstance(outcome, Applied)
        or outcome.value.kind != "review"
        or not isinstance(outcome.value.subject, ReviewRef)
        or review_key(outcome.value.subject) != review_key(review)
    ):
        raise StoreConflict("native_review_has_no_reviewed_base")
    return window.base_sha


async def find_root(session: AsyncSession, review: ReviewRef) -> EngineRun:
    effects = await session.scalars(
        select(EngineEffect).where(
            EngineEffect.kind == "ensure_draft",
            EngineEffect.repo_key == review.repo.identity.digest,
            EngineEffect.state.in_(("committed", "adopted")),
        )
    )
    roots = set()
    from forge.port_values import DraftOutcome

    for effect in effects:
        if effect.run_id is None or effect.outcome_json is None:
            continue
        outcome: DraftOutcome = TypeAdapter(DraftOutcome).validate_json(effect.outcome_json)
        if isinstance(outcome, Applied) and review_key(outcome.value) == review_key(review):
            roots.add(effect.run_id)
    if len(roots) != 1:
        raise StoreConflict("native_review_root_unproven")
    root = await locked_run(session, roots.pop())
    if root.root_run_id != root.id:
        raise StoreConflict("native_review_root_unproven")
    return root


class NativeReviewAdmission:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def admit(
        self,
        review: ReviewRef,
        head: str,
        actor: Actor,
        policy: NativeReviewPolicy,
        *,
        now: datetime,
    ) -> CommandResult:
        async with transaction(self.factory) as session:
            root = await find_root(session, review)
            key = review_key(review)
            cursor = await session.scalar(
                select(EngineReviewCursor)
                .where(EngineReviewCursor.root_review_ref == key)
                .with_for_update()
            )
            if cursor is None:
                cursor = EngineReviewCursor(
                    root_review_ref=key,
                    root_run_id=root.id,
                    review_json=review.canonical_bytes.decode(),
                    last_reviewed_sha=await closing_seed(session, root.id, review),
                )
                session.add(cursor)
                await session.flush()
            if (
                cursor.root_run_id != root.id
                or review_key(ReviewRef.model_validate_json(cursor.review_json)) != key
            ):
                raise StoreConflict("native_review_cursor_scope_mismatch")
            prior = await session.scalar(
                select(EngineReactiveReview).where(
                    EngineReactiveReview.root_review_ref == key,
                    EngineReactiveReview.head_sha == head,
                )
            )
            if prior is not None:
                return CommandResult(
                    status="observed", reason="native_review_duplicate", run_id=prior.run_id
                )
            if cursor.last_reviewed_sha == head:
                return CommandResult(
                    status="observed", reason="native_review_current", run_id=root.id
                )
            request_id = uuid5(UUID(root.id), "native-review:" + head).hex
            session.add(
                EngineReactiveReview(
                    id=request_id,
                    root_run_id=root.id,
                    root_review_ref=key,
                    base_sha=cursor.last_reviewed_sha,
                    head_sha=head,
                    actor_json=actor.canonical_bytes.decode(),
                    policy_json=policy.canonical_bytes.decode(),
                )
            )
            return CommandResult(
                status="accepted",
                reason="native_review_queued",
                data_json=json.dumps(
                    {"request_id": request_id}, sort_keys=True, separators=(",", ":")
                ),
            )

    async def activate_pending(self, *, now: datetime, limit: int = 20) -> int:
        if not 1 <= limit <= 100:
            raise ValueError("native review activation must be bounded")
        async with self.factory() as session:
            ids = list(
                await session.scalars(
                    select(EngineReactiveReview.id)
                    .where(
                        EngineReactiveReview.state == "accepted",
                        EngineReactiveReview.run_id.is_(None),
                    )
                    .order_by(EngineReactiveReview.created_at, EngineReactiveReview.id)
                    .limit(limit)
                )
            )
        count = 0
        for request_id in ids:
            count += await self.activate(request_id, now=now)
        return count

    async def activate(self, request_id: str, *, now: datetime) -> int:
        async with transaction(self.factory) as session:
            row = await session.get(EngineReactiveReview, request_id)
            if row is None:
                return 0
            await locked_run(session, row.root_run_id)
            await session.refresh(row)
            if row.run_id is not None or row.state != "accepted":
                return 0
            cursor = await session.get(EngineReviewCursor, row.root_review_ref)
            if cursor is None:
                raise StoreConflict("native_review_cursor_missing")
            review = ReviewRef.model_validate_json(cursor.review_json)
            policy = NativeReviewPolicy.model_validate_json(row.policy_json)
            repo = review.repo.identity
            request = RunRequest(
                row.id,
                repo.provider,
                repo.connection_id,
                repo.repository_id,
                "review",
                str(review.number),
                row.root_run_id,
                row.root_run_id,
            )
            key = subject_key(request)
            active = await session.scalar(
                select(EngineRun.id)
                .where(
                    EngineRun.subject_key == key,
                    EngineRun.state.not_in(tuple(state.value for state in TERMINAL)),
                )
                .limit(1)
            )
            if active is not None:
                return 0
            run = EngineRun(
                id=row.id,
                provider=repo.provider,
                connection_id=repo.connection_id,
                repository_id=repo.repository_id,
                subject_kind="review",
                subject_id=str(review.number),
                subject_key=key,
                root_run_id=row.root_run_id,
                parent_run_id=row.root_run_id,
                state=State.REVIEWING.value,
            )
            session.add(run)
            await session.flush()
            row.run_id, row.base_sha = run.id, cursor.last_reviewed_sha
            row.expires_at = aware(now) + timedelta(seconds=policy.seconds)
            await enqueue_in(
                session,
                run,
                StepRequest(
                    run.id,
                    Handler.FEEDBACK_REVIEW,
                    f"{run.id}:native_review",
                    json.dumps({"request_id": row.id}, separators=(",", ":")),
                    now,
                    deadline_at=row.expires_at,
                ),
            )
            return 1
