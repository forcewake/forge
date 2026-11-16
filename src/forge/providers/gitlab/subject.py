"""Native issue snapshots and bot-owned note/label recovery."""

from datetime import datetime
from typing import Literal

from forge_protocol import digest

from forge.port_values import (
    Applied,
    CommentOutcome,
    CommentRef,
    DefinitelyAbsent,
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
from forge.providers.gitlab.client import API
from forge.providers.gitlab.notes import resolve_inline
from forge.providers.gitlab.values import (
    array,
    evidence,
    marker as marker_line,
    native_id,
    numeric,
    obj,
    text,
    unavailable,
)


def note_resource(ref: CommentRef) -> str:
    ident = numeric(ref.native_id)
    collection = "issues" if isinstance(ref.subject, SubjectRef) else "merge_requests"
    if isinstance(ref.subject, SubjectRef) and ref.kind != "issue_comment":
        raise unavailable("review_note_requires_review")
    return f"{collection}/{ref.subject.number}/notes/{ident}"


class GitLabSubject:
    def __init__(self, api: API) -> None:
        self.api = api

    def owned(self, repo_ref: SubjectRef | ReviewRef, row: dict[str, object]) -> bool:
        bot = self.api.credentials(repo_ref.repo).bot
        author = obj(row.get("author"))
        return (
            native_id(author.get("id")) == bot.native_id
            and text(author.get("username")) == bot.login
            and row.get("system") is False
        )

    @staticmethod
    def bound(ref: CommentRef, row: dict[str, object]) -> bool:
        expected = "Issue" if isinstance(ref.subject, SubjectRef) else "MergeRequest"
        return (
            native_id(row.get("id")) == ref.native_id
            and type(row.get("noteable_iid")) is int
            and row["noteable_iid"] == ref.subject.number
            and row.get("noteable_type") == expected
            and native_id(row.get("project_id")) == ref.subject.repo.identity.repository_id
        )

    async def get_task(self, subject: SubjectRef) -> TaskSnapshot:
        row = obj((await self.api.read(subject.repo, f"issues/{subject.number}")).document)
        if native_id(row.get("project_id")) != subject.repo.identity.repository_id or native_id(
            row.get("iid")
        ) != str(subject.number):
            raise unavailable("issue_identity_mismatch")
        native_id(row.get("id"))
        title = text(row.get("title"))
        body = "" if row.get("description") is None else text(row.get("description"), empty=True)
        labels = tuple(text(value) for value in array(row.get("labels")))
        if len(labels) != len(set(labels)):
            raise unavailable("duplicate_labels")
        updated = text(row.get("updated_at"))
        try:
            if datetime.fromisoformat(updated.replace("Z", "+00:00")).utcoffset() is None:
                raise ValueError("timestamp is not aware")
        except ValueError:
            raise unavailable("invalid_issue_timestamp") from None
        return TaskSnapshot(
            subject=subject,
            title=title,
            description=body,
            labels=tuple(sorted(labels)),
            version=digest(
                {
                    "id": row["id"],
                    "updated_at": updated,
                    "title": title,
                    "body": body,
                    "labels": sorted(labels),
                }
            ),
        )

    async def notes(self, subject: SubjectRef | ReviewRef) -> list[dict[str, object]]:
        collection = "issues" if isinstance(subject, SubjectRef) else "merge_requests"
        rows = await self.api.pages(subject.repo, f"{collection}/{subject.number}/notes")
        seen = set()
        for row in rows:
            ident = native_id(row.get("id"))
            if ident in seen:
                raise unavailable("duplicate_note_page")
            seen.add(ident)
            if not self.bound(CommentRef(subject=subject, native_id=ident), row):
                raise unavailable("note_target_mismatch")
        return rows

    def observation(
        self,
        subject: SubjectRef | ReviewRef,
        rows: list[dict[str, object]],
        operation: str,
        *,
        kind: Literal["issue_comment", "review_comment", "review"] = "issue_comment",
    ) -> ProbeResult[CommentOutcome]:
        if len(rows) != 1 or not self.owned(subject, rows[0]):
            return Inconclusive(
                reason="note marker missing, foreign or ambiguous", evidence=evidence(operation)
            )
        ref = CommentRef(subject=subject, native_id=native_id(rows[0].get("id")), kind=kind)
        proof = evidence(operation, native_id=ref.native_id)
        return Found[CommentOutcome](
            value=Applied[CommentRef](value=ref, evidence=proof), evidence=proof
        )

    async def probe_comment(
        self, subject: SubjectRef | ReviewRef, marker: str
    ) -> ProbeResult[CommentOutcome]:
        try:
            rows = [
                row
                for row in await self.notes(subject)
                if marker_line(text(row.get("body"), empty=True), marker)
            ]
            if not rows:
                return DefinitelyAbsent(evidence=evidence("probe_comment"))
            return self.observation(subject, rows, "probe_comment")
        except ReadUnavailable as error:
            return Inconclusive(reason="note read unavailable", evidence=error.evidence)

    async def find_comment(self, subject: SubjectRef | ReviewRef, marker: str) -> CommentRef | None:
        rows = [
            row
            for row in await self.notes(subject)
            if marker_line(text(row.get("body"), empty=True), marker)
        ]
        if not rows:
            return None
        if len(rows) != 1:
            raise ReadUnavailable("note marker is ambiguous", evidence("find_comment"))
        if not self.owned(subject, rows[0]):
            return None
        return CommentRef(subject=subject, native_id=native_id(rows[0].get("id")))

    async def probe_comment_update(
        self, comment: CommentRef, body_digest: str
    ) -> ProbeResult[CommentOutcome]:
        try:
            if comment.kind == "review_comment":
                _, row = await resolve_inline(self.api, comment)
                bound = True
            else:
                row = obj(
                    (await self.api.read(comment.subject.repo, note_resource(comment))).document
                )
                bound = self.bound(comment, row)
            if (
                not bound
                or not self.owned(comment.subject, row)
                or digest({"body": text(row.get("body"), empty=True)}) != body_digest
            ):
                return Inconclusive(
                    reason="note owner, body or target changed",
                    evidence=evidence("probe_comment_update"),
                )
            return self.observation(
                comment.subject, [row], "probe_comment_update", kind=comment.kind
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
                return Inconclusive(reason="label state differs", evidence=proof)
            return Found[LabelOutcome](
                value=Applied[LabelState](
                    value=LabelState(subject=subject, label=label, present=present), evidence=proof
                ),
                evidence=proof,
            )
        except ReadUnavailable as error:
            return Inconclusive(reason="label read unavailable", evidence=error.evidence)
