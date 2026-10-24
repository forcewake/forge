"""The only external-write owner: committed intent, fenced dispatch, unfenced receipt."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import json
from typing import Any

from pydantic import TypeAdapter

from forge_protocol import LaneDispatch, RunIdentity, canonical_json, digest

from forge.domain.budget import Resources
from forge.domain.candidate import ValidatedCandidate
from forge.domain.verdicts import CommitOutcome, Committed, Conflict, Unknown
from forge.port_values import (
    Applied,
    BranchOutcome,
    CommentOutcome,
    CommentRef,
    DefinitelyAbsent,
    DispatchOutcome,
    DraftOutcome,
    EffectKind,
    FeedbackNote,
    Found,
    GatewayOutcome,
    Inconclusive,
    LabelOutcome,
    Outcome,
    ProbeResult,
    RepoRef,
    ReviewBody,
    ReviewRef,
    SubjectRef,
)
from forge.ports import BudgetGateway, CIPort, Clock, RepoPort, ReviewPort, SubjectPort, WritePort
from forge.command_values import CommandLease
from forge.store.command_effects import CommandEffectStore
from forge.store.effects import EffectStore
from forge.store.records import KNOWN_OUTCOMES, EffectRecord, Lease, ObservationState, StoreConflict

_CODECS: dict[EffectKind, TypeAdapter[Any]] = {
    EffectKind.COMMIT: TypeAdapter(CommitOutcome),
    EffectKind.ENSURE_BRANCH: TypeAdapter(BranchOutcome),
    EffectKind.ENSURE_DRAFT: TypeAdapter(DraftOutcome),
    EffectKind.POST_COMMENT: TypeAdapter(CommentOutcome),
    EffectKind.UPDATE_COMMENT: TypeAdapter(CommentOutcome),
    EffectKind.POST_REVIEW: TypeAdapter(CommentOutcome),
    EffectKind.REPLY: TypeAdapter(CommentOutcome),
    EffectKind.SET_LABEL: TypeAdapter(LabelOutcome),
    EffectKind.DISPATCH_LANE: TypeAdapter(DispatchOutcome),
    EffectKind.GW_MINT: TypeAdapter(GatewayOutcome),
    EffectKind.GW_UPDATE: TypeAdapter(GatewayOutcome),
    EffectKind.GW_BLOCK: TypeAdapter(GatewayOutcome),
}
_OUTCOME: TypeAdapter[Outcome] = TypeAdapter(Outcome)
_PROBE: TypeAdapter[ProbeResult[Outcome]] = TypeAdapter(ProbeResult[Outcome])
Hook = Callable[[str, EffectRecord], None]


class EffectProtocolError(StoreConflict):
    """Malformed persisted or adapter data cannot authorize a new write."""


def _decode(kind: EffectKind, value: object) -> Outcome:
    try:
        return _OUTCOME.validate_python(_CODECS[kind].validate_python(value))
    except ValueError as exc:
        raise EffectProtocolError("effect outcome does not match its kind") from exc


def _encoded(value: object) -> str:
    return canonical_json(TypeAdapter(object).dump_python(value, mode="json")).decode()


def _observed_state(value: Outcome) -> ObservationState:
    if isinstance(value, Unknown):
        return "unknown"
    if isinstance(value, Conflict):
        return "conflict"
    return "committed" if isinstance(value, (Committed, Applied)) else "failed"


class Effects:
    def __init__(
        self,
        store: EffectStore,
        writer: WritePort,
        repo: RepoPort,
        subject: SubjectPort,
        review: ReviewPort,
        ci: CIPort,
        gateway: BudgetGateway,
        clock: Clock,
        *,
        call_timeout: float = 60,
        hook: Hook | None = None,
    ) -> None:
        if not 0 < call_timeout <= 3600:
            raise ValueError("effect call timeout must be bounded")
        self.store, self._writer, self.repo, self.subject = store, writer, repo, subject
        self.review, self.ci, self._gateway, self.clock = review, ci, gateway, clock
        self.call_timeout, self.hook = call_timeout, hook

    def _point(self, point: str, row: EffectRecord) -> None:
        if self.hook is not None:
            self.hook(point, row)

    async def _perform(
        self,
        lease: Lease,
        kind: EffectKind,
        key: str,
        payload: dict[str, Any],
        call: Callable[[], Awaitable[Outcome]],
        *,
        repo_key: str | None = None,
        head_branch: str | None = None,
    ) -> Outcome:
        row = await self.store.request_once(
            lease,
            key=key,
            kind=kind.value,
            payload_json=canonical_json(payload).decode(),
            payload_digest=digest(payload),
            repo_key=repo_key,
            head_branch=head_branch,
        )
        self._point("after_t1", row)
        if row.state == "dispatched":
            row = await self.recover(row)
        if row.state in KNOWN_OUTCOMES or row.state == "unknown":
            return self._receipt(row)
        row = await self.store.mark_dispatched(row.id, lease, expected_version=row.version)
        self._point("after_t2", row)
        outcome = await self._call(row, call)
        self._point("after_call", row)
        saved = await self.store.record_observation(
            row.id,
            state=_observed_state(outcome),
            outcome_json=_encoded(outcome),
        )
        self._point("after_t3", saved)
        return self._receipt(saved)

    async def _call(self, row: EffectRecord, call: Callable[[], Awaitable[Outcome]]) -> Outcome:
        assert row.dispatch_deadline_at is not None
        remaining = min(
            self.call_timeout, (row.dispatch_deadline_at - self.clock.now()).total_seconds()
        )
        try:
            if remaining <= 0:
                return Unknown(evidence='{"reason":"dispatch_window_elapsed"}')
            async with asyncio.timeout(remaining):
                return _decode(EffectKind(row.kind), await call())
        except asyncio.CancelledError:
            await asyncio.shield(
                self.store.record_observation(
                    row.id,
                    state="unknown",
                    outcome_json=_encoded(Unknown(evidence='{"reason":"cancelled_call"}')),
                )
            )
            raise
        except Exception as exc:
            return Unknown(
                evidence=_encoded({"reason": "call_unobserved", "error_type": type(exc).__name__})
            )

    @staticmethod
    def _receipt(row: EffectRecord) -> Outcome:
        if row.outcome_json is None:
            raise EffectProtocolError("effect has no recorded outcome")
        kind = EffectKind(row.kind)
        return _decode(kind, _CODECS[kind].validate_json(row.outcome_json))

    async def recover(self, row: EffectRecord) -> EffectRecord:
        """Read-only reconciliation; only the original step may dispatch a requeued intent."""
        kind = EffectKind(row.kind)
        if row.state in KNOWN_OUTCOMES or row.state == "requested":
            return row
        if digest(json.loads(row.payload_json)) != row.payload_digest:
            raise EffectProtocolError("persisted effect payload digest mismatch")
        try:
            result = _PROBE.validate_python(await self._probe(kind, json.loads(row.payload_json)))
        except Exception as exc:
            result = Inconclusive(
                reason="probe_unavailable", evidence=_encoded({"error_type": type(exc).__name__})
            )
        if isinstance(result, Found):
            outcome = _decode(kind, result.value)
            if not isinstance(outcome, Unknown):
                return await self.store.resolve_probe(
                    row.id,
                    expected_version=row.version,
                    state="adopted",
                    evidence_json=_encoded(result),
                    outcome_json=_encoded(outcome),
                )
        if (
            isinstance(result, DefinitelyAbsent)
            and row.dispatch_deadline_at is not None
            and self.clock.now() >= row.dispatch_deadline_at
        ):
            return await self.store.resolve_probe(
                row.id,
                expected_version=row.version,
                state="requested",
                evidence_json=_encoded(result),
            )
        unknown = Unknown(evidence=_encoded(result))
        return await self.store.resolve_probe(
            row.id,
            expected_version=row.version,
            state="unknown",
            evidence_json=_encoded(result),
            outcome_json=_encoded(unknown),
        )

    async def _probe(self, kind: EffectKind, data: dict[str, Any]) -> object:
        repo = RepoRef.model_validate(data["repo"]) if "repo" in data else None
        if kind == EffectKind.COMMIT:
            assert repo is not None
            return await self.repo.probe_commit(
                repo,
                data["branch"],
                data["marker"],
                data["expected_head"],
                TypeAdapter(ValidatedCandidate).validate_json(_encoded(data["candidate"])),
            )
        if kind == EffectKind.ENSURE_BRANCH:
            assert repo is not None
            return await self.repo.probe_branch(repo, data["branch"], data["base"])
        if kind == EffectKind.ENSURE_DRAFT:
            assert repo is not None
            return await self.review.probe_draft(repo, data["head_branch"])
        if kind == EffectKind.POST_COMMENT:
            return await self.subject.probe_comment(
                TypeAdapter(SubjectRef | ReviewRef).validate_python(data["subject"]), data["marker"]
            )
        if kind == EffectKind.UPDATE_COMMENT:
            return await self.subject.probe_comment_update(
                CommentRef.model_validate(data["comment"]), data["body_digest"]
            )
        if kind == EffectKind.SET_LABEL:
            return await self.subject.probe_label(
                SubjectRef.model_validate(data["subject"]), data["label"], data["present"]
            )
        if kind == EffectKind.POST_REVIEW:
            return await self.review.probe_review(
                ReviewRef.model_validate(data["review"]), data["marker"]
            )
        if kind == EffectKind.REPLY:
            return await self.review.probe_reply(
                FeedbackNote.model_validate_json(data["note_json"]), data["marker"]
            )
        if kind == EffectKind.DISPATCH_LANE:
            assert repo is not None
            return await self.ci.probe_dispatch(repo, data["correlation_id"])
        if kind == EffectKind.GW_MINT:
            return await self._gateway.probe_mint(data["alias"])
        if kind == EffectKind.GW_UPDATE:
            return await self._gateway.probe_update(
                data["alias"], Resources.model_validate_json(data["allowance_json"])
            )
        if kind == EffectKind.GW_BLOCK:
            return await self._gateway.probe_block(data["alias"])
        raise EffectProtocolError("effect kind has no probe")

    async def commit(
        self,
        lease: Lease,
        key: str,
        repo: RepoRef,
        candidate: ValidatedCandidate,
        expected_head: str,
        marker: str,
    ) -> CommitOutcome:
        if not isinstance(candidate, ValidatedCandidate):
            raise TypeError("publication requires a ValidatedCandidate")
        payload = {
            "repo": repo.model_dump(mode="json"),
            "branch": candidate.changeset.branch,
            "expected_head": expected_head,
            "marker": marker,
            "candidate": TypeAdapter(ValidatedCandidate).dump_python(candidate, mode="json"),
        }
        return TypeAdapter(CommitOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.COMMIT,
                key,
                payload,
                lambda: self._writer.commit(repo, candidate, expected_head, marker),
            )
        )

    async def ensure_branch(
        self, lease: Lease, key: str, repo: RepoRef, branch: str, base: str
    ) -> BranchOutcome:
        payload = {"repo": repo.model_dump(mode="json"), "branch": branch, "base": base}
        return TypeAdapter(BranchOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.ENSURE_BRANCH,
                key,
                payload,
                lambda: self._writer.ensure_branch(repo, branch, base),
            )
        )

    async def ensure_draft(
        self, lease: Lease, root_run_id: str, repo: RepoRef, head_branch: str, title: str, body: str
    ) -> DraftOutcome:
        payload = {
            "repo": repo.model_dump(mode="json"),
            "head_branch": head_branch,
            "title": title,
            "body": body,
        }
        return TypeAdapter(DraftOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.ENSURE_DRAFT,
                f"{root_run_id}:ensure_draft",
                payload,
                lambda: self._writer.ensure_draft(repo, head_branch, title, body),
                repo_key=repo.identity.digest,
                head_branch=head_branch,
            )
        )

    async def post_comment(
        self, lease: Lease, key: str, subject: SubjectRef | ReviewRef, body: str, marker: str
    ) -> CommentOutcome:
        payload = {"subject": subject.model_dump(mode="json"), "body": body, "marker": marker}
        return TypeAdapter(CommentOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.POST_COMMENT,
                key,
                payload,
                lambda: self._writer.post_comment(subject, body, marker),
            )
        )

    async def update_comment(
        self, lease: Lease, key: str, comment: CommentRef, body: str
    ) -> CommentOutcome:
        payload = {
            "comment": comment.model_dump(mode="json"),
            "body": body,
            "body_digest": digest({"body": body}),
        }
        return TypeAdapter(CommentOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.UPDATE_COMMENT,
                key,
                payload,
                lambda: self._writer.update_comment(comment, body),
            )
        )

    async def set_label(
        self, lease: Lease, key: str, subject: SubjectRef, label: str, present: bool
    ) -> LabelOutcome:
        payload = {"subject": subject.model_dump(mode="json"), "label": label, "present": present}
        return TypeAdapter(LabelOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.SET_LABEL,
                key,
                payload,
                lambda: self._writer.set_label(subject, label, present),
            )
        )

    async def post_review(
        self, lease: Lease, key: str, review: ReviewRef, body: ReviewBody, marker: str
    ) -> CommentOutcome:
        payload = {
            "review": review.model_dump(mode="json"),
            "body_json": body.model_dump_json(),
            "marker": marker,
        }
        return TypeAdapter(CommentOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.POST_REVIEW,
                key,
                payload,
                lambda: self._writer.post_review(review, body, marker),
            )
        )

    async def reply(
        self, lease: Lease, key: str, note: FeedbackNote, body: str, marker: str
    ) -> CommentOutcome:
        payload = {"note_json": note.model_dump_json(), "body": body, "marker": marker}
        return TypeAdapter(CommentOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.REPLY,
                key,
                payload,
                lambda: self._writer.reply(note, body, marker),
            )
        )

    async def dispatch_lane(
        self, lease: Lease, key: str, repo: RepoRef, dispatch: LaneDispatch
    ) -> DispatchOutcome:
        if dispatch.nonce is None:
            raise ValueError("every engine dispatch requires an explicit correlation nonce")
        payload = {
            "repo": repo.model_dump(mode="json"),
            "dispatch_json": dispatch.model_dump_json(),
            "correlation_id": dispatch.nonce,
        }
        return TypeAdapter(DispatchOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.DISPATCH_LANE,
                key,
                payload,
                lambda: self._writer.dispatch_lane(repo, dispatch),
            )
        )

    async def gw_mint(
        self,
        lease: Lease,
        key: str,
        identity: RunIdentity,
        alias: str,
        model: str,
        allowance: Resources,
        expires_at: datetime,
    ) -> GatewayOutcome:
        payload = {
            "identity": identity.model_dump(mode="json"),
            "alias": alias,
            "model": model,
            "allowance_json": allowance.model_dump_json(),
            "expires_at": expires_at.isoformat(),
        }
        return TypeAdapter(GatewayOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.GW_MINT,
                key,
                payload,
                lambda: self._gateway.mint(identity, alias, model, allowance, expires_at),
            )
        )

    async def gw_update(
        self, lease: Lease, key: str, alias: str, allowance: Resources
    ) -> GatewayOutcome:
        payload = {"alias": alias, "allowance_json": allowance.model_dump_json()}
        return TypeAdapter(GatewayOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.GW_UPDATE,
                key,
                payload,
                lambda: self._gateway.update(alias, allowance),
            )
        )

    async def gw_block(self, lease: Lease, key: str, alias: str) -> GatewayOutcome:
        return TypeAdapter(GatewayOutcome).validate_python(
            await self._perform(
                lease,
                EffectKind.GW_BLOCK,
                key,
                {"alias": alias},
                lambda: self._gateway.block(alias),
            )
        )

    async def command_effect(
        self,
        authority: CommandEffectStore,
        lease: CommandLease,
        kind: EffectKind,
        key: str,
        payload_json: str,
    ) -> Outcome:
        if kind not in (EffectKind.POST_COMMENT, EffectKind.GW_BLOCK, EffectKind.GW_UPDATE):
            raise EffectProtocolError("command effect kind is not permitted")
        row = await authority.request_once(
            lease, key=key, kind=kind.value, payload_json=payload_json
        )
        self._point("after_t1", row)
        if row.state == "dispatched":
            row = await self.recover(row)
        if row.state in KNOWN_OUTCOMES or row.state == "unknown":
            return self._receipt(row)
        row = await authority.mark_dispatched(lease, row)
        self._point("after_t2", row)
        payload = json.loads(payload_json)

        async def call() -> Outcome:
            if kind == EffectKind.GW_BLOCK:
                return await self._gateway.block(payload["alias"])
            if kind == EffectKind.GW_UPDATE:
                return await self._gateway.update(
                    payload["alias"], Resources.model_validate_json(payload["allowance_json"])
                )
            target: SubjectRef | ReviewRef = TypeAdapter(SubjectRef | ReviewRef).validate_python(
                payload["subject"]
            )
            return await self._writer.post_comment(target, payload["body"], payload["marker"])

        outcome = await self._call(row, call)
        self._point("after_call", row)
        saved = await self.store.record_observation(
            row.id, state=_observed_state(outcome), outcome_json=_encoded(outcome)
        )
        self._point("after_t3", saved)
        return self._receipt(saved)
