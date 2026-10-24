"""Fenced native candidate validation, publication and closing evidence."""

from datetime import datetime
import json

from forge_protocol import CandidateBundle, digest
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.candidate import ValidatedCandidate
from forge.domain.run_spec import RunSpec2
from forge.domain.states import Handler, State, transition
from forge.domain.verdicts import CheckVerdict, Committed
from forge.port_values import ReviewVerdict
from forge.store.models_execution import EngineCandidate, EngineEffect
from forge.store.models_runs import EngineRunSpec
from forge.store.records import Lease, StoreConflict
from forge.store.transactions import SessionFactory, fence, locked_run, transaction

_VALIDATED = TypeAdapter(ValidatedCandidate)


async def candidate_row(session: AsyncSession, lease: Lease) -> EngineCandidate:
    row = await session.scalar(
        select(EngineCandidate)
        .where(
            EngineCandidate.run_id == lease.run_id,
            EngineCandidate.attempt == lease.attempt,
            EngineCandidate.generation == lease.generation,
        )
        .with_for_update()
    )
    if row is None or digest(json.loads(row.manifest_json)) != row.digest:
        raise StoreConflict("accepted candidate is missing or its receipt is corrupt")
    return row


class DeliveryStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def begin_publication(self, lease: Lease, *, now: datetime) -> None:
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            run = await locked_run(session, lease.run_id)
            if lease.handler != Handler.PUBLISH or run.state not in (
                State.RUNNING.value,
                State.PUBLISHING.value,
            ):
                raise StoreConflict("publication needs its original handler in the owning state")
            if run.state == State.RUNNING.value:
                run.state = transition(State(run.state), State.PUBLISHING).value
                run.version += 1

    async def candidate(self, lease: Lease, *, now: datetime) -> CandidateBundle:
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            return CandidateBundle.model_validate_json(
                (await candidate_row(session, lease)).manifest_json
            )

    async def validated(self, lease: Lease, value: ValidatedCandidate, *, now: datetime) -> None:
        document = json.loads(_VALIDATED.dump_json(value))
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            row = await candidate_row(session, lease)
            evidence = json.loads(row.validation_json or "{}")
            if evidence.get("validated", document) != document:
                raise StoreConflict("validated candidate cannot change after its durable intent")
            evidence["validated"] = document
            row.validation_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
            if row.state == "received":
                row.state = "validated"

    async def published(self, lease: Lease, sha: str, effect_key: str, *, now: datetime) -> None:
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            row = await candidate_row(session, lease)
            effect = await session.scalar(
                select(EngineEffect).where(EngineEffect.key == effect_key)
            )
            if (
                effect is None
                or effect.run_id != lease.run_id
                or effect.kind != "commit"
                or effect.state not in ("committed", "adopted")
                or effect.outcome_json is None
                or row.validation_json is None
                or digest(json.loads(effect.payload_json)) != effect.payload_digest
            ):
                raise StoreConflict("publication needs a durable validated commit receipt")
            outcome = Committed.model_validate_json(effect.outcome_json)
            validated = json.loads(row.validation_json)["validated"]
            if outcome.sha != sha or json.loads(effect.payload_json)["candidate"] != validated:
                raise StoreConflict("publication differs from its exact validated commit intent")
            if row.published_sha not in (None, sha):
                raise StoreConflict("published candidate SHA is immutable")
            row.published_sha, row.state = sha, "published"

    async def evidence(self, lease: Lease, sha: str, *, now: datetime) -> dict[str, object]:
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            row = await candidate_row(session, lease)
            if row.state != "published" or row.published_sha != sha:
                raise StoreConflict("closing evidence is not for this published candidate")
            result: dict[str, object] = json.loads(row.validation_json or "{}")
            if "validated" not in result:
                raise StoreConflict("published candidate lacks validated manifest")
            return result

    async def record(
        self, lease: Lease, sha: str, value: CheckVerdict | ReviewVerdict, *, now: datetime
    ) -> None:
        name = "verification" if isinstance(value, CheckVerdict) else "review"
        bound = value.tested_sha if isinstance(value, CheckVerdict) else value.candidate_sha
        if bound != sha:
            raise StoreConflict("closing evidence names another candidate")
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            row = await candidate_row(session, lease)
            if row.state != "published" or row.published_sha != sha:
                raise StoreConflict("closing evidence requires this published candidate")
            document = json.loads(row.validation_json or "{}")
            encoded = value.model_dump(mode="json")
            if name == "review" and document.get(name, encoded) != encoded:
                raise StoreConflict("a durable model review cannot be repeated or replaced")
            document[name] = encoded
            row.validation_json = json.dumps(document, sort_keys=True, separators=(",", ":"))

    async def root_spec(self, root_run_id: str) -> RunSpec2:
        async with self.factory() as session:
            row = await session.scalar(
                select(EngineRunSpec).where(EngineRunSpec.run_id == root_run_id)
            )
            if row is None:
                raise StoreConflict("root draft requires its original frozen specification")
            spec = RunSpec2.model_validate_json(row.document_json)
            if row.digest != spec.digest or row.document_json != spec.spec_json:
                raise StoreConflict("root specification is not canonical")
            return spec
