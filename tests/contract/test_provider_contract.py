"""Provider-neutral contracts; connection fixtures can replace the in-memory adapter."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast

import pytest
from pydantic import HttpUrl
from forge_protocol import LaneDispatch, digest

from forge.domain.candidate import (
    CandidateBundle,
    ValidatedCandidate,
    CreateFile,
    FullReplacement,
    validate_candidate_bundle,
)
from forge.domain.plan import RepositoryIdentity
from forge.domain.verdicts import Committed, Conflict, Unknown
from forge.port_values import (
    Actor,
    Applied,
    CheckResult,
    CommentRef,
    FeedbackNote,
    ReviewRef,
    DefinitelyAbsent,
    Found,
    Inconclusive,
    Outcome,
    ReadUnavailable,
    Refused,
    RepoRef,
    ReviewBody,
    SubjectRef,
    TaskSnapshot,
)
from forge.ports import ActorPort, CIPort, RepoPort, ReviewPort, SubjectPort, WritePort
from forge.providers.fake import Blob, FakeProvider, Fault
from forge.providers.fake.state import ref_key

BRANCH = "forge/run-1"


@dataclass
class ProviderCase:
    repo: RepoRef
    base: str
    subject: SubjectRef
    subjects: SubjectPort
    repositories: RepoPort
    reviews: ReviewPort
    ci: CIPort
    actors: ActorPort
    writes: WritePort
    fake: FakeProvider


@pytest.fixture(params=("github", "gitlab"))
def case(request: pytest.FixtureRequest) -> ProviderCase:
    provider = cast(Literal["github", "gitlab"], request.param)
    native = FakeProvider(provider=provider)
    repo = RepoRef(
        identity=RepositoryIdentity(provider=provider, connection_id="test", repository_id="42"),
        full_name="owner/project",
    )
    base = native.seed_repository(
        repo, {"old.txt": Blob(b"original\r\n"), "keep.bin": Blob(b"\x00\xff")}
    )
    subject = SubjectRef(repo=repo, number=1)
    native.tasks[ref_key(subject)] = TaskSnapshot(
        subject=subject, title="Task", description="Exact source", version="1"
    )
    return ProviderCase(repo, base, subject, native, native, native, native, native, native, native)


def candidate(base: str, *, branch: str = BRANCH) -> ValidatedCandidate:
    return validate_candidate_bundle(
        CandidateBundle(
            attempt_base_oid=base,
            driver_exit="success",
            entries=(
                FullReplacement("old.txt", "changed\r\n"),
                CreateFile("tool.sh", "echo ok\n", mode="100755"),
            ),
        ),
        base_contents={"old.txt": "original\r\n"},
        branch=branch,
        commit_message="Exact candidate",
    )


async def arrange(case: ProviderCase) -> tuple[CommentRef, ReviewRef, FeedbackNote]:
    assert isinstance(await case.writes.ensure_branch(case.repo, BRANCH, case.base), Applied)
    comment = await case.writes.post_comment(case.subject, "plan", "plan-marker")
    draft = await case.writes.ensure_draft(case.repo, BRANCH, "Draft", "Body")
    assert isinstance(comment, Applied) and isinstance(draft, Applied)
    review = await case.writes.post_review(
        draft.value,
        ReviewBody(candidate_sha=case.base, body="note", decision="inconclusive"),
        "review-initial",
    )
    assert isinstance(review, Applied)
    notes = await case.reviews.list_feedback(draft.value, None)
    return comment.value, draft.value, notes[0]


@pytest.mark.asyncio
async def test_commit_is_atomic_cas_and_probe_recovers_complete_result(case: ProviderCase) -> None:
    assert isinstance(await case.writes.ensure_branch(case.repo, BRANCH, case.base), Applied)
    before = await case.repositories.read_blob(case.repo, case.base, "old.txt")
    outcome = await case.writes.commit(case.repo, candidate(case.base), case.base, "commit-marker")
    assert isinstance(outcome, Committed)
    assert await case.repositories.head(case.repo, BRANCH) == outcome.sha
    assert await case.repositories.head(case.repo, "main") == case.base
    assert (
        await case.repositories.read_blob(case.repo, case.base, "old.txt")
        == before
        == b"original\r\n"
    )
    assert await case.repositories.read_blob(case.repo, outcome.sha, "old.txt") == b"changed\r\n"
    assert await case.repositories.read_blob(case.repo, outcome.sha, "keep.bin") == b"\x00\xff"
    tree = await case.repositories.read_tree(case.repo, outcome.sha)
    assert next(entry for entry in tree.entries if entry.path == "tool.sh").mode == "100755"
    probe = await case.repositories.probe_commit(case.repo, BRANCH, "commit-marker", case.base)
    assert isinstance(probe, Found) and probe.value == outcome
    duplicate = await case.writes.commit(
        case.repo, candidate(case.base), case.base, "commit-marker"
    )
    assert isinstance(duplicate, Conflict) and duplicate.actual_head == outcome.sha
    assert await case.repositories.head(case.repo, BRANCH) == outcome.sha
    assert (
        await case.repositories.find_commit_by_marker(case.repo, BRANCH, "commit-marker")
        == outcome.sha
    )
    assert await case.repositories.read_blob(case.repo, outcome.sha, "absent.txt") is None
    with pytest.raises(ReadUnavailable):
        await case.repositories.read_blob(case.repo, "0" * 40, "old.txt")


@pytest.mark.asyncio
async def test_subject_review_and_ci_read_write_roundtrips(case: ProviderCase) -> None:
    comment, review, note = await arrange(case)
    assert await case.subjects.find_comment(case.subject, "plan-marker") == comment
    assert isinstance(await case.writes.update_comment(comment, "approved"), Applied)
    updated = await case.subjects.probe_comment_update(comment, digest({"body": "approved"}))
    assert (
        isinstance(updated, Found)
        and isinstance(updated.value, Applied)
        and updated.value.value == comment
    )
    assert isinstance(await case.writes.set_label(case.subject, "forge", True), Applied)
    assert "forge" in (await case.subjects.get_task(case.subject)).labels
    assert isinstance(await case.writes.set_label(case.subject, "forge", False), Applied)
    assert "forge" not in (await case.subjects.get_task(case.subject)).labels
    assert await case.reviews.find_draft(case.repo, BRANCH) == review
    assert await case.reviews.review_head(review) == case.base
    assert await case.reviews.list_feedback(review, note.cursor) == ()
    reply = await case.writes.reply(note, "answer", "reply-marker")
    reply_probe = await case.reviews.probe_reply(note, "reply-marker")
    assert (
        isinstance(reply, Applied) and isinstance(reply_probe, Found) and reply_probe.value == reply
    )
    dispatch = LaneDispatch(
        forge_run_id="run-1",
        attempt=0,
        generation=0,
        server_url=HttpUrl("https://forge.test"),
        provider=case.repo.identity.provider,
        repository=case.repo.full_name,
        ref=BRANCH,
        base_sha=case.base,
        approved_digest="a" * 64,
        driver="test",
        model="test",
        timeout_seconds=60,
        nonce="nonce-1",
    )
    lane = await case.writes.dispatch_lane(case.repo, dispatch)
    assert isinstance(lane, Applied)
    assert await case.ci.find_lane(case.repo, "nonce-1") == lane.value
    assert (await case.ci.lane_status(lane.value)).status == "queued"
    assert isinstance(await case.ci.probe_dispatch(case.repo, "nonce-1"), Found)
    assert await case.ci.checks_for(case.repo, case.base) == ()


WRITES = (
    "post_comment",
    "update_comment",
    "set_label",
    "ensure_branch",
    "commit",
    "ensure_draft",
    "post_review",
    "reply",
    "dispatch_lane",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", WRITES)
async def test_faults_never_turn_uncertainty_into_absence(case: ProviderCase, method: str) -> None:
    comment, review, note = await arrange(case)
    dispatch = LaneDispatch(
        forge_run_id="run-1",
        attempt=0,
        generation=0,
        server_url=HttpUrl("https://forge.test"),
        provider=case.repo.identity.provider,
        repository=case.repo.full_name,
        ref=BRANCH,
        base_sha=case.base,
        approved_digest="a" * 64,
        driver="test",
        model="test",
        timeout_seconds=60,
        nonce="nonce-lost",
    )
    calls: dict[str, Callable[[], Awaitable[Outcome]]] = {
        "post_comment": lambda: case.writes.post_comment(case.subject, "lost", "lost-marker"),
        "update_comment": lambda: case.writes.update_comment(comment, "lost"),
        "set_label": lambda: case.writes.set_label(case.subject, "lost", True),
        "ensure_branch": lambda: case.writes.ensure_branch(case.repo, "forge/lost", case.base),
        "commit": lambda: case.writes.commit(
            case.repo, candidate(case.base), case.base, "lost-marker"
        ),
        "ensure_draft": lambda: case.writes.ensure_draft(case.repo, BRANCH, "Draft", "Body"),
        "post_review": lambda: case.writes.post_review(
            review,
            ReviewBody(candidate_sha=case.base, body="lost", decision="inconclusive"),
            "lost-marker",
        ),
        "reply": lambda: case.writes.reply(note, "lost", "lost-marker"),
        "dispatch_lane": lambda: case.writes.dispatch_lane(case.repo, dispatch),
    }
    probes: dict[str, Callable[[], Awaitable[object]]] = {
        "post_comment": lambda: case.subjects.probe_comment(case.subject, "lost-marker"),
        "update_comment": lambda: case.subjects.probe_comment_update(
            comment, digest({"body": "lost"})
        ),
        "set_label": lambda: case.subjects.probe_label(case.subject, "lost", True),
        "ensure_branch": lambda: case.repositories.probe_branch(case.repo, "forge/lost", case.base),
        "commit": lambda: case.repositories.probe_commit(
            case.repo, BRANCH, "lost-marker", case.base
        ),
        "ensure_draft": lambda: case.reviews.probe_draft(case.repo, BRANCH),
        "post_review": lambda: case.reviews.probe_review(review, "lost-marker"),
        "reply": lambda: case.reviews.probe_reply(note, "lost-marker"),
        "dispatch_lane": lambda: case.ci.probe_dispatch(case.repo, "nonce-lost"),
    }
    case.fake.fail_next(method, Fault(unknown_after_success=True, delay_visibility=2))
    lost = await calls[method]()
    assert isinstance(lost, Unknown)
    mutations_before = len([name for name, _ in case.fake.calls if name == method])
    first, second, third = await probes[method](), await probes[method](), await probes[method]()
    assert isinstance(first, Inconclusive) and isinstance(second, Inconclusive)
    assert isinstance(third, Found) and not isinstance(third.value, Unknown)
    assert len([name for name, _ in case.fake.calls if name == method]) == mutations_before
    assert isinstance(await probes[method](), Found)
    case.fake.records.clear()
    assert isinstance(await probes[method](), Found)


@pytest.mark.asyncio
async def test_authority_and_independent_checks_do_not_gain_capabilities(
    case: ProviderCase,
) -> None:
    refused = await case.writes.ensure_branch(case.repo, "main", case.base)
    assert isinstance(refused, Refused)
    protected_commit = await case.writes.commit(
        case.repo, candidate(case.base, branch="main"), case.base, "no"
    )
    assert isinstance(protected_commit, Unknown)
    assert await case.repositories.head(case.repo, "main") == case.base
    bot = case.actors.bot_identity()
    case.fake.approvers.add((ref_key(case.subject), bot.native_id))
    assert not await case.actors.is_approver(bot, case.subject)
    human = Actor(
        provider=case.repo.identity.provider,
        connection_id="test",
        native_id="human",
        login="human",
        is_bot=False,
    )
    case.fake.approvers.add((ref_key(case.subject), human.native_id))
    assert await case.actors.is_approver(human, case.subject)
    outside = case.repo.model_copy(
        update={"identity": case.repo.identity.model_copy(update={"connection_id": "other"})}
    )
    with pytest.raises(ReadUnavailable):
        await case.repositories.head(outside, "main")
    lane_check = CheckResult(
        name="lane",
        tested_sha=case.base,
        native_id="1",
        attempt=1,
        role="lane",
        status="passed",
        observed_at=case.fake.now,
        evidence="fake:lane_only",
    )
    case.fake.checks[case.repo.identity.digest] = (
        lane_check,
        lane_check.model_copy(update={"tested_sha": "b" * 40, "role": "verification"}),
    )
    checks = await case.ci.checks_for(case.repo, case.base)
    assert checks == (lane_check,) and all(check.role != "verification" for check in checks)


@pytest.mark.asyncio
async def test_native_state_survives_lost_recovery_cache_and_http_failure(
    case: ProviderCase,
) -> None:
    comment, review, note = await arrange(case)
    result = await case.writes.commit(case.repo, candidate(case.base), case.base, "actual-marker")
    assert isinstance(result, Committed)
    case.fake.records.clear()
    recovered = await case.repositories.probe_commit(case.repo, BRANCH, "actual-marker", case.base)
    assert (
        isinstance(recovered, Found)
        and isinstance(recovered.value, Committed)
        and recovered.value.sha == result.sha
    )
    assert isinstance(await case.subjects.probe_comment(case.subject, "plan-marker"), Found)
    assert (
        await case.repositories.find_commit_by_marker(case.repo, BRANCH, "actual-marker")
        == result.sha
    )
    assert isinstance(await case.repositories.probe_branch(case.repo, BRANCH, case.base), Found)
    assert isinstance(await case.reviews.probe_draft(case.repo, BRANCH), Found)
    case.fake.fail_next("post_comment", Fault(http_5xx=True))
    failed = await case.writes.post_comment(case.subject, "not written", "http-failed")
    assert isinstance(failed, Unknown)
    assert isinstance(
        await case.subjects.probe_comment(case.subject, "http-failed"), DefinitelyAbsent
    )
    case.fake.fail_next("read_blob", Fault(http_5xx=True))
    with pytest.raises(ReadUnavailable):
        await case.repositories.read_blob(case.repo, case.base, "old.txt")
    case.fake.fail_next("probe_comment", Fault(http_5xx=True))
    assert isinstance(await case.subjects.probe_comment(case.subject, "http-failed"), Inconclusive)


@pytest.mark.asyncio
async def test_injected_conflict_preserves_competing_head(case: ProviderCase) -> None:
    assert isinstance(await case.writes.ensure_branch(case.repo, BRANCH, case.base), Applied)
    case.fake.fail_next("commit", Fault(conflict=True))
    result = await case.writes.commit(case.repo, candidate(case.base), case.base, "never-applied")
    assert isinstance(result, Conflict) and result.actual_head not in (None, case.base)
    assert result.actual_head is not None
    assert await case.repositories.head(case.repo, BRANCH) == result.actual_head
    assert (
        await case.repositories.read_blob(case.repo, result.actual_head, "old.txt")
        == b"original\r\n"
    )
    assert await case.repositories.read_blob(case.repo, result.actual_head, "tool.sh") is None
    probe = await case.repositories.probe_commit(case.repo, BRANCH, "never-applied", case.base)
    assert isinstance(probe, Found) and isinstance(probe.value, Conflict)
    assert probe.value.actual_head == result.actual_head
    assert await case.repositories.find_commit_by_marker(case.repo, BRANCH, "never-applied") is None


@pytest.mark.asyncio
async def test_duplicate_native_writes_are_not_hidden_by_the_fake(case: ProviderCase) -> None:
    first = await case.writes.post_comment(case.subject, "one", "same-marker")
    second = await case.writes.post_comment(case.subject, "two", "same-marker")
    assert (
        isinstance(first, Applied) and isinstance(second, Applied) and first.value != second.value
    )
    assert isinstance(await case.subjects.probe_comment(case.subject, "same-marker"), Inconclusive)
    with pytest.raises(ReadUnavailable):
        await case.subjects.find_comment(case.subject, "same-marker")
    assert isinstance(await case.writes.ensure_branch(case.repo, BRANCH, case.base), Applied)
    dispatch = LaneDispatch(
        forge_run_id="run-1",
        attempt=9,
        generation=0,
        server_url=HttpUrl("https://forge.test"),
        provider=case.repo.identity.provider,
        repository=case.repo.full_name,
        ref=BRANCH,
        base_sha=case.base,
        approved_digest="a" * 64,
        driver="test",
        model="test",
        timeout_seconds=60,
        nonce="same-nonce",
    )
    first_lane = await case.writes.dispatch_lane(case.repo, dispatch)
    second_lane = await case.writes.dispatch_lane(case.repo, dispatch)
    assert isinstance(first_lane, Applied) and isinstance(second_lane, Applied)
    assert first_lane.value.native_id != second_lane.value.native_id
    assert first_lane.value.attempt == second_lane.value.attempt == 1
    assert isinstance(await case.ci.probe_dispatch(case.repo, "same-nonce"), Inconclusive)
    assert (await case.ci.lane_status(first_lane.value)).handle == first_lane.value
    assert (await case.ci.lane_status(second_lane.value)).handle == second_lane.value


@pytest.mark.asyncio
async def test_engine_dispatch_requires_an_explicit_correlation_nonce(case: ProviderCase) -> None:
    assert isinstance(await case.writes.ensure_branch(case.repo, BRANCH, case.base), Applied)
    dispatch = LaneDispatch(
        forge_run_id="run-1",
        attempt=0,
        generation=0,
        server_url=HttpUrl("https://forge.test"),
        provider=case.repo.identity.provider,
        repository=case.repo.full_name,
        ref=BRANCH,
        base_sha=case.base,
        approved_digest="a" * 64,
        driver="test",
        model="test",
        timeout_seconds=60,
        nonce="provided",
    )
    no_nonce = dispatch.model_copy(update={"nonce": None})
    refused = await case.writes.dispatch_lane(case.repo, no_nonce)
    assert isinstance(refused, Refused) and refused.reason == "missing_correlation_nonce"
    assert case.fake.lanes == {}
