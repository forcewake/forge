"""Validate exact native bytes once and publish through journaled effects."""

import hashlib
import json

from forge_protocol import CandidateBundle

from forge.domain.candidate import (
    PolicyViolation,
    ValidatedCandidate,
    validate_candidate_bundle,
)
from forge.domain.patch import CandidateError
from forge.domain.change_types import Change, ChangeSet, Operation
from forge.domain.git_candidate import candidate_paths, parse_git_candidate
from forge.domain.paths import validate_changeset
from forge.domain.states import Handler, State
from forge.domain.verdicts import Committed, Conflict
from forge.engine.delivery_values import DeliverySettings, DeliveryWork
from forge.engine.effects import Effects
from forge.engine.plan import result_json
from forge.engine.runner import Park, Retryable, StepContext, StepResult
from forge.port_values import Applied, ReadUnavailable
from forge.ports import BlobPort, Clock, RepoPort
from forge.store.delivery import DeliveryStore
from forge.store.lane_work import ApprovedLane, LaneWorkStore
from forge.store.records import StepRequest, StoreConflict

MAX_BASE_BYTES = 400_000


def publication_key(context: StepContext[Effects]) -> str:
    lease = context.lease
    return f"{lease.run_id}:{lease.attempt}:{lease.generation}:publish"


def stopped(reason: str) -> StepResult:
    return StepResult(result_json(reason=reason), park=Park(reason))


def commit_headline(title: str, *, correction: bool) -> str:
    return f"{'Correct' if correction else 'Implement'} {title}"


class PublishHandler:
    def __init__(
        self,
        lanes: LaneWorkStore,
        store: DeliveryStore,
        repo: RepoPort,
        blobs: BlobPort,
        clock: Clock,
        settings: DeliverySettings,
    ) -> None:
        self.lanes, self.store, self.repo, self.blobs = lanes, store, repo, blobs
        self.clock, self.settings = clock, settings

    async def __call__(self, context: StepContext[Effects]) -> StepResult:
        await self.store.begin_publication(context.lease, now=self.clock.now())
        approved = await self.lanes.approved(context.lease)
        candidate = await self.store.candidate(context.lease, now=self.clock.now())
        supplied = CandidateBundle.model_validate(json.loads(context.payload_json)["candidate"])
        if supplied != candidate or (
            candidate.base_sha,
            candidate.approved_digest,
            candidate.driver,
            candidate.model,
            candidate.forge_run_id,
            candidate.attempt,
            candidate.generation,
        ) != (
            approved.brief.execution_base_sha,
            approved.brief.approved_digest,
            approved.spec.driver,
            approved.spec.model,
            context.lease.run_id,
            context.lease.attempt,
            context.lease.generation,
        ):
            raise StoreConflict("publication candidate differs from accepted frozen execution")
        try:
            validated = await self.validate(candidate, approved)
        except (CandidateError, PolicyViolation) as error:
            reason = error.reason if isinstance(error, CandidateError) else "write_policy_refused"
            return stopped("candidate_" + reason)
        except ReadUnavailable:
            raise Retryable("publication_snapshot_unavailable") from None
        await self.store.validated(context.lease, validated, now=self.clock.now())
        key = publication_key(context) + ":commit"
        outcome = await context.effects.commit(
            context.lease,
            key,
            approved.repo,
            validated,
            candidate.base_sha,
            f"<!-- forge:{context.lease.run_id}:candidate:{candidate.diff_sha256} -->",
        )
        if not isinstance(outcome, Committed):
            return stopped(
                "branch_drift" if isinstance(outcome, Conflict) else "effect_unknown:commit"
            )
        branch = validated.changeset.branch
        if await self.repo.head(approved.repo, branch) != outcome.sha:
            return stopped("branch_drift")
        await self.store.published(context.lease, outcome.sha, key, now=self.clock.now())
        return await self.draft(context, approved, branch, outcome.sha)

    async def draft(
        self, context: StepContext[Effects], approved: ApprovedLane, branch: str, sha: str
    ) -> StepResult:
        root = await self.store.root_spec(approved.root_run_id)
        draft = await context.effects.ensure_draft(
            context.lease,
            approved.root_run_id,
            approved.repo,
            branch,
            root.task_title,
            f"Implementation of the approved plan.\n\n{root.plan.render()}",
        )
        if not isinstance(draft, Applied):
            return stopped("effect_unknown:ensure_draft")
        if draft.value.repo.identity != approved.repo.identity:
            raise StoreConflict("draft belongs to another repository")
        if await self.repo.head(approved.repo, branch) != sha:
            return stopped("branch_drift")
        work = DeliveryWork(
            candidate_sha=sha,
            review=draft.value,
            published_at=self.clock.now(),
            settings=self.settings,
        )
        successor = StepRequest(
            context.lease.run_id,
            Handler.VERIFY,
            publication_key(context) + ":verify",
            work.canonical_bytes.decode(),
            self.clock.now(),
            max_attempts=self.settings.verification_seconds // self.settings.poll_seconds + 2,
        )
        return StepResult(
            result_json(candidate_sha=sha, review=draft.value.model_dump(mode="json")),
            successors=(successor,),
            next_state=State.VERIFYING,
        )

    async def validate(
        self, candidate: CandidateBundle, approved: ApprovedLane
    ) -> ValidatedCandidate:
        diff = await self.blobs.get(candidate.diff_sha256)
        if (
            diff is None
            or len(diff) != candidate.diff_size_bytes
            or hashlib.sha256(diff).hexdigest() != candidate.diff_sha256
        ):
            raise CandidateError("corrupt_diff", "accepted diff blob is missing or corrupt")
        paths = candidate_paths(diff, base_sha=candidate.base_sha)
        scope = ChangeSet(
            branch=f"forge/{approved.root_run_id}",
            commit_message="Validate",
            changes=tuple(Change(path, Operation.CREATE, "") for path in paths),
        )
        violations = validate_changeset(
            scope,
            allowed_paths=list(approved.spec.write_policy.allowed_globs),
            policy=approved.spec.write_policy,
        )
        if violations:
            raise PolicyViolation(violations)
        tree = await self.repo.read_tree(approved.repo, candidate.base_sha)
        if tree.repo.identity != approved.repo.identity or tree.sha != candidate.base_sha:
            raise StoreConflict("publication snapshot differs from its native repository or SHA")
        entries = {entry.path: entry for entry in tree.entries}
        base: dict[str, bytes] = {}
        modes: dict[str, str] = {}
        for path in paths:
            for parent in ("/".join(path.split("/")[:n]) for n in range(1, len(path.split("/")))):
                if parent in entries and entries[parent].kind != "tree":
                    raise CandidateError(
                        "unsafe_parent", "candidate traverses a non-directory Git entry"
                    )
            if path in entries:
                entry = entries[path]
                if entry.kind != "blob" or entry.mode not in ("100644", "100755", "120000"):
                    raise CandidateError(
                        "unsupported_mode", "candidate targets a non-blob Git entry"
                    )
                raw = await self.repo.read_blob(approved.repo, candidate.base_sha, path)
                if raw is None:
                    raise CandidateError("stale_base", "authoritative base blob is absent")
                if len(raw) > MAX_BASE_BYTES:
                    raise CandidateError("file_too_large", "authoritative base exceeds its bound")
                header = b"blob " + str(len(raw)).encode() + b"\x00"
                oracle = (
                    hashlib.sha1(header + raw).hexdigest()
                    if len(entry.sha) == 40
                    else hashlib.sha256(header + raw).hexdigest()
                )
                if oracle != entry.sha:
                    raise CandidateError(
                        "stale_base", "authoritative base bytes differ from native Git identity"
                    )
                base[path], modes[path] = raw, entry.mode
        bundle = parse_git_candidate(diff, base_sha=candidate.base_sha, base_contents=base)
        return validate_candidate_bundle(
            bundle,
            base_contents=base,
            base_modes=modes,
            branch=scope.branch,
            commit_message=commit_headline(
                approved.spec.task_title, correction=approved.brief.repair is not None
            ),
            allowed_paths=approved.spec.write_policy.allowed_globs,
            policy=approved.spec.write_policy,
        )
