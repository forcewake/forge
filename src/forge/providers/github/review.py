"""Bot-owned draft discovery and native, cursor-bound review feedback."""

from datetime import datetime
from typing import Literal, cast

from pydantic import ValidationError

from forge.port_values import (
    Actor,
    Applied,
    CommentOutcome,
    CommentRef,
    DraftOutcome,
    FeedbackNote,
    Found,
    Inconclusive,
    ProbeResult,
    ReadUnavailable,
    RepoRef,
    ReviewRef,
)
from forge.providers.github.client import API
from forge.providers.github.repo import (
    evidence,
    list_doc,
    malformed,
    marker_line,
    native_id,
    object_doc,
    object_sha,
    request,
    text,
)
from forge.providers.github.subject import GitHubSubject

MAX_PAGES = 10


class GitHubReview:
    def __init__(self, api: API, *, bot: Actor) -> None:
        self.api, self.owner = api, GitHubSubject(api, bot=bot)

    async def pages(
        self, repo: RepoRef, resource: str, *, params: dict[str, str | int] | None = None
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            reply = await request(
                self.api, repo, resource, params={**(params or {}), "per_page": 100, "page": page}
            )
            batch = list_doc(reply.document)
            if len(batch) > 100:
                raise malformed("review_page_too_large")
            for value in batch:
                row = object_doc(value)
                ident = native_id(row.get("id"))
                if ident in seen:
                    raise malformed("duplicate_review_page")
                seen.add(ident)
                rows.append(row)
            if len(batch) < 100:
                return rows
        raise malformed("review_scan_truncated")

    def validate(
        self, repo: RepoRef, row: dict[str, object], *, number: int | None = None
    ) -> ReviewRef:
        if repo.identity.connection_id != self.owner.bot.connection_id:
            raise malformed("review_connection_mismatch")
        head, base = object_doc(row.get("head")), object_doc(row.get("base"))
        native_id(row.get("number"))
        if (
            native_id(object_doc(head.get("repo")).get("id")) != repo.identity.repository_id
            or native_id(object_doc(base.get("repo")).get("id")) != repo.identity.repository_id
            or type(row.get("number")) is not int
            or (number is not None and row["number"] != number)
        ):
            raise malformed("review_repository_or_number_mismatch")
        native_id(row.get("id"))
        object_sha(head.get("sha"))
        return ReviewRef(repo=repo, number=cast(int, row["number"]))

    async def document(self, review: ReviewRef) -> dict[str, object]:
        row = object_doc((await request(self.api, review.repo, f"pulls/{review.number}")).document)
        self.validate(review.repo, row, number=review.number)
        return row

    async def draft_rows(self, repo: RepoRef, head_branch: str) -> list[dict[str, object]]:
        metadata = object_doc((await self.api.metadata(repo)).document)
        owner = text(metadata.get("full_name")).split("/")[0]
        rows = await self.pages(
            repo, "pulls", params={"state": "all", "head": owner + ":" + head_branch}
        )
        result = []
        for row in rows:
            self.validate(repo, row)
            if object_doc(row.get("head")).get("ref") != head_branch:
                raise malformed("review_head_filter_mismatch")
            result.append(row)
        return result

    async def find_draft(self, repo: RepoRef, head_branch: str) -> ReviewRef | None:
        rows = await self.draft_rows(repo, head_branch)
        if not rows:
            return None
        if (
            len(rows) != 1
            or rows[0].get("state") != "open"
            or rows[0].get("draft") is not True
            or not self.owner._owned(rows[0])
        ):
            raise malformed("draft_missing_foreign_closed_or_ambiguous")
        return self.validate(repo, rows[0])

    async def review_head(self, review: ReviewRef) -> str:
        return object_sha(object_doc((await self.document(review)).get("head")).get("sha"))

    @staticmethod
    def _note(review: ReviewRef, row: dict[str, object], kind: str) -> FeedbackNote:
        user = object_doc(row.get("user"))
        ident = native_id(row.get("id"))
        timestamp = text(row.get("created_at") if kind != "review" else row.get("submitted_at"))
        try:
            created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            return FeedbackNote(
                ref=CommentRef(
                    subject=review,
                    native_id=ident,
                    kind=cast(
                        Literal["issue_comment", "review_comment", "review"],
                        {"issue": "issue_comment", "inline": "review_comment", "review": "review"}[
                            kind
                        ],
                    ),
                ),
                author=Actor(
                    provider="github",
                    connection_id=review.repo.identity.connection_id,
                    native_id=native_id(user.get("id")),
                    login=text(user.get("login")),
                    is_bot=user.get("type") == "Bot",
                ),
                body=text(row.get("body"), empty=True),
                cursor=f"{created.isoformat()}|{kind}|{ident}",
                created_at=created,
            )
        except (ValueError, ValidationError):
            raise malformed("invalid_review_feedback") from None

    async def list_feedback(self, review: ReviewRef, after: str | None) -> tuple[FeedbackNote, ...]:
        await self.document(review)
        notes = []
        for kind, resource in (
            ("issue", f"issues/{review.number}/comments"),
            ("review", f"pulls/{review.number}/reviews"),
            ("inline", f"pulls/{review.number}/comments"),
        ):
            for row in await self.pages(review.repo, resource):
                if kind == "review" and row.get("state") == "PENDING":
                    continue
                notes.append(self._note(review, row, kind))
        ordered = tuple(sorted(notes, key=lambda note: (note.created_at, note.cursor)))
        if after is None:
            return ordered
        cursors = [note.cursor for note in ordered]
        if after not in cursors:
            raise malformed("feedback_cursor_not_visible")
        return ordered[cursors.index(after) + 1 :]

    async def probe_draft(self, repo: RepoRef, head_branch: str) -> ProbeResult[DraftOutcome]:
        try:
            ref = await self.find_draft(repo, head_branch)
            if ref is not None:
                proof = evidence("probe_draft", review=str(ref.number))
                return Found[DraftOutcome](
                    value=Applied[ReviewRef](value=ref, evidence=proof), evidence=proof
                )
            return Inconclusive(
                reason="draft visibility is not proof of absence", evidence=evidence("probe_draft")
            )
        except ReadUnavailable as error:
            return Inconclusive(reason="draft read unavailable", evidence=error.evidence)

    async def probe_review(self, review: ReviewRef, marker: str) -> ProbeResult[CommentOutcome]:
        try:
            await self.document(review)
            rows = [
                row
                for row in await self.pages(review.repo, f"pulls/{review.number}/reviews")
                if marker_line(text(row.get("body"), empty=True), marker)
            ]
            return self._probe_comment(review, rows, "probe_review", "review")
        except ReadUnavailable as error:
            return Inconclusive(reason="review read unavailable", evidence=error.evidence)

    def _probe_comment(
        self,
        review: ReviewRef,
        rows: list[dict[str, object]],
        operation: str,
        kind: Literal["issue_comment", "review_comment", "review"] = "issue_comment",
    ) -> ProbeResult[CommentOutcome]:
        if len(rows) != 1 or not self.owner._owned(rows[0]):
            return Inconclusive(
                reason="marker missing, foreign or ambiguous", evidence=evidence(operation)
            )
        ref = CommentRef(subject=review, native_id=native_id(rows[0].get("id")), kind=kind)
        proof = evidence(operation, comment=ref.native_id)
        return Found[CommentOutcome](
            value=Applied[CommentRef](value=ref, evidence=proof), evidence=proof
        )

    async def probe_reply(self, note: FeedbackNote, marker: str) -> ProbeResult[CommentOutcome]:
        if not isinstance(note.ref.subject, ReviewRef):
            return Inconclusive(
                reason="reply target is not a review", evidence=evidence("probe_reply")
            )
        review = note.ref.subject
        try:
            kind = feedback_kind(note)
            resource = (
                f"pulls/{review.number}/comments"
                if kind == "inline"
                else f"issues/{review.number}/comments"
            )
            rows = []
            for row in await self.pages(review.repo, resource):
                body = text(row.get("body"), empty=True)
                bound = (
                    row.get("in_reply_to_id") == int(note.ref.native_id)
                    if kind == "inline"
                    else marker_line(body, reply_marker(note))
                )
                if marker_line(body, marker) and bound:
                    rows.append(row)
            return self._probe_comment(
                review,
                rows,
                "probe_reply",
                "review_comment" if kind == "inline" else "issue_comment",
            )
        except ReadUnavailable as error:
            return Inconclusive(reason="reply read unavailable", evidence=error.evidence)


def feedback_kind(note: FeedbackNote) -> str:
    parts = note.cursor.split("|")
    if (
        len(parts) != 3
        or parts[1] not in ("issue", "review", "inline")
        or parts[2] != note.ref.native_id
    ):
        raise malformed("invalid_feedback_cursor")
    if (
        not note.ref.native_id.isascii()
        or not note.ref.native_id.isdecimal()
        or int(note.ref.native_id) <= 0
    ):
        raise malformed("invalid_feedback_native_id")
    expected = {"issue": "issue_comment", "inline": "review_comment", "review": "review"}[parts[1]]
    if note.ref.kind != expected:
        raise malformed("feedback_native_kind_mismatch")
    return parts[1]


def reply_marker(note: FeedbackNote) -> str:
    return f"<!-- forge:reply:{feedback_kind(note)}:{note.ref.native_id} -->"
