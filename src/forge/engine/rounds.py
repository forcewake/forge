"""Native review feedback stages fresh gated children on the existing Draft."""

from collections.abc import Callable
import re

from forge.command_values import CommandEnvelope, CommandLease, CommandResult
from forge.domain.change_types import Change, ChangeSet, Operation
from forge.domain.paths import normalize_repo_path, validate_changeset
from forge.engine.commands import Action, refusal
from forge.port_values import FeedbackNote, ReadUnavailable, RepoRef, ReviewRef
from forge.ports import ActorPort, Clock, RepoPort, ReviewPort
from forge.store.records import RunRecord, StoreConflict
from forge.store.rounds import RoundStore, RoundWindow, revision_intent

_COMMAND = re.compile(r"/(fix|ask|approve-revision)(?:[ \t]+([^\r\n]*))?[ \t]*")


def same_command(note: FeedbackNote, envelope: CommandEnvelope) -> bool:
    lines = note.body.splitlines()
    match = _COMMAND.fullmatch(lines[0] if lines else "")
    if match is None:
        return False
    argument = ((match[2] or "") + "\n" + "\n".join(lines[1:])).strip()
    return (match[1].replace("-", "_"), argument) == (
        envelope.command.kind,
        envelope.command.argument.strip(),
    )


def native_note(notes: tuple[FeedbackNote, ...], envelope: CommandEnvelope) -> FeedbackNote:
    matches = [
        note
        for note in notes
        if envelope.native_id in (note.ref.native_id, f"{note.ref.kind}:{note.ref.native_id}")
    ]
    if len(matches) != 1:
        raise StoreConflict("native_feedback_unavailable")
    note = matches[0]
    if (
        note.ref.subject != envelope.target
        or note.author != envelope.actor
        or note.author.is_bot
        or not same_command(note, envelope)
    ):
        raise StoreConflict("native_feedback_changed")
    return note


def confined_feedback(window: RoundWindow, text: str) -> bool:
    paths = re.findall(r"`([^`\r\n]+)`", text)
    if not paths:
        return True
    try:
        changes = tuple(Change(normalize_repo_path(path), Operation.CREATE, "") for path in paths)
        scope = ChangeSet(branch=window.branch, commit_message="Review correction", changes=changes)
        return not validate_changeset(
            scope,
            allowed_paths=list(window.spec.write_policy.allowed_globs),
            policy=window.spec.write_policy,
        )
    except (ValueError, TypeError):
        return False


class RoundActions:
    def __init__(
        self,
        store: RoundStore,
        repositories: RepoPort,
        reviews: ReviewPort,
        clock: Clock,
        actor_for: Callable[[RepoRef], ActorPort],
    ) -> None:
        self.store, self.repositories, self.reviews = store, repositories, reviews
        self.clock, self.actor_for = clock, actor_for

    def actions(self) -> dict[str, Action]:
        return {"fix": self.fix, "ask": self.ask, "approve_revision": self.approve_revision}

    async def observe(
        self,
        envelope: CommandEnvelope,
        run: RunRecord | None,
        *,
        correction: bool,
    ) -> RoundWindow:
        if run is None:
            raise StoreConflict("run_not_found")
        if not isinstance(envelope.target, ReviewRef):
            raise StoreConflict("run_scope_mismatch")
        if envelope.actor.is_bot or not envelope.command.argument.strip():
            raise StoreConflict("malformed_command")
        window = await self.store.window(run.id, envelope.target, correction=correction)
        actors = self.actor_for(envelope.target.repo)
        if not await actors.is_approver(envelope.actor, window.intent.task.subject):
            raise StoreConflict("actor_not_authorized")
        native_note(await self.reviews.list_feedback(window.review, None), envelope)
        found = await self.reviews.find_draft(window.review.repo, window.branch)
        if found is None or (found.repo.identity, found.number) != (
            window.review.repo.identity,
            window.review.number,
        ):
            raise StoreConflict("review_window_closed")
        if (
            await self.reviews.review_head(window.review) != window.base_sha
            or await self.repositories.head(window.review.repo, window.branch) != window.base_sha
        ):
            raise StoreConflict("branch_drift")
        return window

    async def fix(
        self,
        lease: CommandLease,
        envelope: CommandEnvelope,
        run: RunRecord | None,
    ) -> CommandResult:
        try:
            window = await self.observe(envelope, run, correction=True)
            if not confined_feedback(window, envelope.command.argument):
                return refusal("revision_out_of_scope", run)
            assert run is not None
            return await self.store.stage(
                lease,
                run.id,
                window,
                revision_intent(window, envelope.actor, envelope),
                now=self.clock.now(),
            )
        except StoreConflict as error:
            return refusal(str(error), run)
        except ReadUnavailable:
            return refusal("review_unavailable", run)

    async def ask(
        self,
        lease: CommandLease,
        envelope: CommandEnvelope,
        run: RunRecord | None,
    ) -> CommandResult:
        try:
            window = await self.observe(envelope, run, correction=False)
            assert run is not None
            return await self.store.clarify(lease, run.id, window, now=self.clock.now())
        except StoreConflict as error:
            return refusal(str(error), run)
        except ReadUnavailable:
            return refusal("review_unavailable", run)

    async def approve_revision(
        self,
        lease: CommandLease,
        envelope: CommandEnvelope,
        run: RunRecord | None,
    ) -> CommandResult:
        try:
            await self.observe(envelope, run, correction=False)
            assert run is not None and isinstance(envelope.target, ReviewRef)
            if envelope.command.argument.strip() != run.id:
                return refusal("malformed_command", run)
            original = await self.store.feedback(run.id, envelope.target)
            native_note(await self.reviews.list_feedback(envelope.target, None), original)
            return await self.store.approve(lease, run.id, envelope.target, now=self.clock.now())
        except StoreConflict as error:
            return refusal(str(error), run)
        except ReadUnavailable:
            return refusal("review_unavailable", run)
