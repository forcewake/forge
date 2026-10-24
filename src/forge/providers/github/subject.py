"""Native issue snapshots and bot-owned comment/label recovery observations."""

from datetime import datetime

from forge_protocol import digest
from pydantic import ValidationError

from forge.port_values import (
    Actor,
    Applied,
    CommentOutcome,
    CommentRef,
    Found,
    Inconclusive,
    LabelOutcome,
    LabelState,
    ProbeResult,
    ReadUnavailable,
    ReviewRef,
    SubjectRef,
    TaskSnapshot,
)
from forge.providers.github.client import API
from forge.providers.github.repo import (
    comment_bound,
    comment_resource,
    evidence,
    list_doc,
    malformed,
    marker_line,
    native_id,
    object_doc,
    request,
    text,
)

MAX_COMMENT_PAGES = 10


class GitHubSubject:
    def __init__(self, api: API, *, bot: Actor) -> None:
        if (
            bot.provider != "github"
            or not bot.is_bot
            or not bot.native_id.isascii()
            or not bot.native_id.isdecimal()
            or int(bot.native_id) <= 0
            or str(int(bot.native_id)) != bot.native_id
        ):
            raise ValueError("an explicit native GitHub bot identity is required")
        self.api, self.bot = api, bot

    def _scope(self, subject: SubjectRef | ReviewRef) -> None:
        if (
            subject.repo.identity.provider != "github"
            or subject.repo.identity.connection_id != self.bot.connection_id
        ):
            raise malformed("subject_connection_mismatch")

    async def get_task(self, subject: SubjectRef) -> TaskSnapshot:
        self._scope(subject)
        reply = await request(self.api, subject.repo, f"issues/{subject.number}")
        row = object_doc(reply.document)
        if (
            type(row.get("number")) is not int
            or row["number"] != subject.number
            or row.get("pull_request") is not None
        ):
            raise malformed("issue_identity_mismatch")
        native_id(row.get("id"))
        title = text(row.get("title"))
        raw_body = row.get("body")
        body = "" if raw_body is None else text(raw_body, empty=True)
        labels = tuple(text(object_doc(label).get("name")) for label in list_doc(row.get("labels")))
        if len(labels) != len(set(labels)):
            raise malformed("duplicate_issue_labels")
        updated = text(row.get("updated_at"))
        try:
            timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if timestamp.utcoffset() is None:
                raise ValueError("missing update timezone")
            return TaskSnapshot(
                subject=subject,
                title=title,
                description=body,
                labels=tuple(sorted(labels)),
                version=digest(
                    {
                        "native_id": row["id"],
                        "updated_at": updated,
                        "title": title,
                        "body": body,
                        "labels": sorted(labels),
                    }
                ),
            )
        except (ValidationError, ValueError):
            raise malformed("invalid_issue_snapshot") from None

    async def _comments(self, subject: SubjectRef | ReviewRef) -> list[dict[str, object]]:
        self._scope(subject)
        comments: list[dict[str, object]] = []
        seen: set[str] = set()
        for page in range(1, MAX_COMMENT_PAGES + 1):
            reply = await request(
                self.api,
                subject.repo,
                f"issues/{subject.number}/comments",
                params={"per_page": 100, "page": page},
            )
            rows = list_doc(reply.document)
            if len(rows) > 100:
                raise malformed("comment_page_too_large")
            for raw in rows:
                row = object_doc(raw)
                identifier = native_id(row.get("id"))
                if identifier in seen:
                    raise malformed("duplicate_comment_page")
                if not comment_bound(CommentRef(subject=subject, native_id=identifier), row):
                    raise malformed("comment_target_mismatch")
                seen.add(identifier)
                comments.append(row)
            if len(rows) < 100:
                return comments
        raise malformed("comment_scan_truncated")

    def _owned(self, row: dict[str, object]) -> bool:
        actor = object_doc(row.get("user"))
        return (
            native_id(actor.get("id")) == self.bot.native_id
            and text(actor.get("login")).casefold() == self.bot.login.casefold()
            and actor.get("type") == "Bot"
        )

    async def probe_comment(
        self, subject: SubjectRef | ReviewRef, marker: str
    ) -> ProbeResult[CommentOutcome]:
        try:
            matches = [
                row
                for row in await self._comments(subject)
                if marker_line(text(row.get("body"), empty=True), marker)
            ]
            if len(matches) != 1 or not self._owned(matches[0]):
                return Inconclusive(
                    reason="comment marker missing, foreign or ambiguous",
                    evidence=evidence("probe_comment"),
                )
            ref = CommentRef(subject=subject, native_id=native_id(matches[0].get("id")))
            proof = evidence("probe_comment", comment=ref.native_id)
            return Found[CommentOutcome](
                value=Applied[CommentRef](value=ref, evidence=proof), evidence=proof
            )
        except ReadUnavailable as error:
            return Inconclusive(reason="comment read unavailable", evidence=error.evidence)

    async def find_comment(self, subject: SubjectRef | ReviewRef, marker: str) -> CommentRef | None:
        matches = [
            row
            for row in await self._comments(subject)
            if marker_line(text(row.get("body"), empty=True), marker)
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ReadUnavailable("comment marker is ambiguous", evidence("find_comment"))
        if not self._owned(matches[0]):
            return None
        return CommentRef(subject=subject, native_id=native_id(matches[0].get("id")))

    async def probe_comment_update(
        self, comment: CommentRef, body_digest: str
    ) -> ProbeResult[CommentOutcome]:
        if comment.subject.repo.identity.connection_id != self.bot.connection_id:
            return Inconclusive(
                reason="comment connection mismatch", evidence=evidence("probe_comment_update")
            )
        if (
            not comment.native_id.isascii()
            or not comment.native_id.isdecimal()
            or int(comment.native_id) <= 0
        ):
            return Inconclusive(
                reason="invalid native comment identity", evidence=evidence("probe_comment_update")
            )
        try:
            reply = await request(self.api, comment.subject.repo, comment_resource(comment))
            row = object_doc(reply.document)
            if (
                not comment_bound(comment, row)
                or not self._owned(row)
                or digest({"body": text(row.get("body"), empty=True)}) != body_digest
            ):
                return Inconclusive(
                    reason="comment body, owner or target differs",
                    evidence=evidence("probe_comment_update", reply.request_id),
                )
            proof = evidence("probe_comment_update", reply.request_id, comment=comment.native_id)
            return Found[CommentOutcome](
                value=Applied[CommentRef](value=comment, evidence=proof), evidence=proof
            )
        except ReadUnavailable:
            raise

    async def probe_label(
        self, subject: SubjectRef, label: str, present: bool
    ) -> ProbeResult[LabelOutcome]:
        try:
            task = await self.get_task(subject)
            proof = evidence("probe_label")
            if (label in task.labels) != present:
                return Inconclusive(reason="label state differs from intent", evidence=proof)
            return Found[LabelOutcome](
                value=Applied[LabelState](
                    value=LabelState(subject=subject, label=label, present=present), evidence=proof
                ),
                evidence=proof,
            )
        except (ReadUnavailable, ValidationError) as error:
            return Inconclusive(
                reason="label read unavailable",
                evidence=error.evidence
                if isinstance(error, ReadUnavailable)
                else evidence("probe_label"),
            )
