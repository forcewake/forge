"""Read the current native display without deriving executable input from it."""

from forge_protocol import digest

from forge.port_values import Applied, CommentRef, Found
from forge.ports import SubjectPort
from forge.store.approval_values import PreparedPlan


def same_comment(left: CommentRef, right: CommentRef) -> bool:
    return (
        left.kind,
        left.native_id,
        left.subject.repo.identity,
        left.subject.kind,
        left.subject.number,
    ) == (
        right.kind,
        right.native_id,
        right.subject.repo.identity,
        right.subject.kind,
        right.subject.number,
    )


async def displayed_plan(
    subjects: SubjectPort, expected: CommentRef, prepared: PreparedPlan
) -> bool:
    current = await subjects.find_comment(expected.subject, prepared.marker)
    if current is None or not same_comment(current, expected):
        return False
    body = prepared.comment + "\n\n" + prepared.marker
    proof = await subjects.probe_comment_update(current, digest({"body": body}))
    return (
        isinstance(proof, Found)
        and isinstance(proof.value, Applied)
        and same_comment(proof.value.value, expected)
    )
