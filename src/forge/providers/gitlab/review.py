"""Same-project bot Draft MRs and exact native discussion feedback."""

from datetime import datetime

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
from forge.providers.gitlab.client import API
from forge.providers.gitlab.notes import discussion_id, discussion_notes, review_document
from forge.providers.gitlab.subject import GitLabSubject
from forge.providers.gitlab.values import (
    evidence,
    marker as marker_line,
    native_id,
    numeric,
    obj,
    sha,
    text,
    unavailable,
)


class GitLabReview:
    def __init__(self, api: API) -> None:
        self.api, self.subjects = api, GitLabSubject(api)

    async def document(self, review: ReviewRef) -> dict[str, object]:
        return await review_document(self.api, review)

    def bot_owned(self, repo: RepoRef, row: dict[str, object]) -> bool:
        author, bot = obj(row.get("author")), self.api.credentials(repo).bot
        return native_id(author.get("id")) == bot.native_id and author.get("username") == bot.login

    @staticmethod
    def validate(repo: RepoRef, row: dict[str, object]) -> ReviewRef:
        if (
            native_id(row.get("project_id")) != repo.identity.repository_id
            or native_id(row.get("source_project_id")) != repo.identity.repository_id
            or native_id(row.get("target_project_id")) != repo.identity.repository_id
        ):
            raise unavailable("merge_request_repository_mismatch")
        native_id(row.get("id"))
        return ReviewRef(repo=repo, number=int(native_id(row.get("iid"))))

    async def find_draft(self, repo: RepoRef, head_branch: str) -> ReviewRef | None:
        rows = await self.api.pages(
            repo,
            "merge_requests",
            params={"scope": "all", "state": "all", "source_branch": head_branch},
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise unavailable("ambiguous_merge_request")
        row = rows[0]
        ref = self.validate(repo, row)
        if (
            row.get("source_branch") != head_branch
            or row.get("state") != "opened"
            or row.get("draft") is not True
            or not self.bot_owned(repo, row)
        ):
            raise unavailable("merge_request_not_open_bot_draft")
        return ref

    async def review_head(self, review: ReviewRef) -> str:
        return sha((await self.document(review)).get("sha"))

    async def list_feedback(self, review: ReviewRef, after: str | None) -> tuple[FeedbackNote, ...]:
        notes = []
        for ident, row in await discussion_notes(self.api, review):
            if row.get("system") is not False:
                continue
            author = obj(row.get("author"))
            note_id = native_id(row.get("id"))
            try:
                created = datetime.fromisoformat(text(row.get("created_at")).replace("Z", "+00:00"))
                notes.append(
                    FeedbackNote(
                        ref=CommentRef(subject=review, native_id=note_id, kind="review_comment"),
                        author=Actor(
                            provider="gitlab",
                            connection_id=review.repo.identity.connection_id,
                            native_id=native_id(author.get("id")),
                            login=text(author.get("username")),
                            is_bot=author.get("bot") is True
                            or native_id(author.get("id"))
                            == self.api.credentials(review.repo).bot.native_id,
                        ),
                        body=text(row.get("body"), empty=True),
                        cursor=f"{created.isoformat()}|{ident}|{note_id}",
                        created_at=created,
                    )
                )
            except (ValueError, ValidationError):
                raise unavailable("invalid_feedback") from None
        ordered = tuple(sorted(notes, key=lambda note: (note.created_at, note.cursor)))
        if after is None:
            return ordered
        cursors = [note.cursor for note in ordered]
        if after not in cursors:
            raise unavailable("feedback_cursor_not_visible")
        return ordered[cursors.index(after) + 1 :]

    async def probe_draft(self, repo: RepoRef, head_branch: str) -> ProbeResult[DraftOutcome]:
        try:
            ref = await self.find_draft(repo, head_branch)
            if ref is None:
                return Inconclusive(
                    reason="draft visibility is not proof of absence",
                    evidence=evidence("probe_draft"),
                )
            proof = evidence("probe_draft", native_id=str(ref.number))
            return Found[DraftOutcome](
                value=Applied[ReviewRef](value=ref, evidence=proof), evidence=proof
            )
        except ReadUnavailable as error:
            return Inconclusive(reason="draft read unavailable", evidence=error.evidence)

    async def probe_review(self, review: ReviewRef, marker: str) -> ProbeResult[CommentOutcome]:
        try:
            await self.document(review)
            rows = [
                row
                for row in await self.subjects.notes(review)
                if marker_line(text(row.get("body"), empty=True), marker)
            ]
            return self.subjects.observation(review, rows, "probe_review", kind="review")
        except ReadUnavailable as error:
            return Inconclusive(reason="review read unavailable", evidence=error.evidence)

    async def probe_reply(self, note: FeedbackNote, marker: str) -> ProbeResult[CommentOutcome]:
        if not isinstance(note.ref.subject, ReviewRef):
            return Inconclusive(
                reason="reply requires review target", evidence=evidence("probe_reply")
            )
        try:
            ident = feedback_discussion(note)
            rows = [
                row
                for discussion, row in await discussion_notes(self.api, note.ref.subject)
                if discussion == ident
                and marker_line(text(row.get("body"), empty=True), marker)
                and marker_line(text(row.get("body"), empty=True), reply_marker(note))
            ]
            return self.subjects.observation(
                note.ref.subject, rows, "probe_reply", kind="review_comment"
            )
        except ReadUnavailable as error:
            return Inconclusive(reason="reply read unavailable", evidence=error.evidence)


def feedback_discussion(note: FeedbackNote) -> str:
    parts = note.cursor.split("|")
    if (
        len(parts) != 3
        or parts[2] != numeric(note.ref.native_id)
        or note.ref.kind != "review_comment"
    ):
        raise unavailable("invalid_feedback_native_binding")
    return discussion_id(parts[1])


def reply_marker(note: FeedbackNote) -> str:
    return f"<!-- forge:reply:{feedback_discussion(note)}:{note.ref.native_id} -->"
