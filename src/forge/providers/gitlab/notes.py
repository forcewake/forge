"""Resolve native discussion-note targets without embedding opaque IDs in note IDs."""

import re

from forge.port_values import CommentRef, ReviewRef
from forge.providers.gitlab.client import API
from forge.providers.gitlab.values import array, native_id, obj, text, unavailable


def discussion_id(value: object) -> str:
    ident = text(value)
    if re.fullmatch(r"[a-f0-9]{40}", ident) is None:
        raise unavailable("invalid_discussion_id")
    return ident


async def review_document(api: API, review: ReviewRef) -> dict[str, object]:
    row = obj((await api.read(review.repo, f"merge_requests/{review.number}")).document)
    if (
        native_id(row.get("iid")) != str(review.number)
        or native_id(row.get("project_id")) != review.repo.identity.repository_id
        or native_id(row.get("source_project_id")) != review.repo.identity.repository_id
        or native_id(row.get("target_project_id")) != review.repo.identity.repository_id
    ):
        raise unavailable("merge_request_identity_mismatch")
    native_id(row.get("id"))
    return row


async def discussion_notes(api: API, review: ReviewRef) -> list[tuple[str, dict[str, object]]]:
    parent = await review_document(api, review)
    rows = await api.pages(review.repo, f"merge_requests/{review.number}/discussions")
    seen_discussions: set[str] = set()
    seen_notes: set[str] = set()
    notes = []
    for row in rows:
        ident = discussion_id(row.get("id"))
        if ident in seen_discussions:
            raise unavailable("duplicate_discussion_page")
        seen_discussions.add(ident)
        for raw in array(row.get("notes")):
            note = obj(raw)
            note_id = native_id(note.get("id"))
            if (
                note_id in seen_notes
                or note.get("noteable_type") != "MergeRequest"
                or native_id(note.get("noteable_id")) != native_id(parent.get("id"))
                or native_id(note.get("project_id")) != review.repo.identity.repository_id
            ):
                raise unavailable("discussion_note_identity_mismatch")
            seen_notes.add(note_id)
            notes.append((ident, note))
    return notes


async def resolve_inline(api: API, ref: CommentRef) -> tuple[str, dict[str, object]]:
    if not isinstance(ref.subject, ReviewRef) or ref.kind != "review_comment":
        raise unavailable("inline_note_requires_review")
    rows = [
        (ident, note)
        for ident, note in await discussion_notes(api, ref.subject)
        if str(note["id"]) == ref.native_id
    ]
    if len(rows) != 1:
        raise unavailable("inline_note_not_uniquely_visible")
    ident, row = rows[0]
    return f"merge_requests/{ref.subject.number}/discussions/{ident}/notes/{ref.native_id}", row
