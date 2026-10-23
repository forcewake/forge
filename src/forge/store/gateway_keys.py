"""Presealed key material; remote observations never revive purged credentials."""

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
from uuid import uuid4
from typing import cast

from pydantic import HttpUrl

from sqlalchemy import select

from forge_protocol import ModelKeyGrant, RunIdentity

from forge.budget.credentials import CredentialUnavailable, CredentialVault, KeyScope, Purpose
from forge.port_values import GatewayKeyRef
from forge.store.models_budget import EngineGatewayKey
from forge.store.models_runs import EngineRun
from forge.store.records import Lease, StoreConflict
from forge.store.transactions import SessionFactory, fence, locked_run, stored_time, transaction


@dataclass(frozen=True)
class KeyRecord:
    scope: KeyScope
    reference: GatewayKeyRef
    state: str
    token_digest: str
    ciphertext: bytes | None = field(repr=False)


def _record(row: EngineGatewayKey) -> KeyRecord:
    if row.reference_json is None or row.token_digest is None:
        raise CredentialUnavailable("gateway row predates authenticated scope storage")
    reference = GatewayKeyRef.model_validate_json(row.reference_json)
    scope = KeyScope(
        identity=RunIdentity(
            forge_run_id=row.run_id, attempt=row.attempt, generation=row.generation
        ),
        purpose=cast(Purpose, row.purpose),
        alias=row.alias,
        model=reference.model,
        expires_at=stored_time(row.expires_at),
    )
    if scope.aad_digest != row.aad_digest or scope.expires_at != reference.expires_at:
        raise CredentialUnavailable("stored gateway scope disagrees with its authentication data")
    return KeyRecord(scope, reference, row.state, row.token_digest, row.ciphertext)


class GatewayKeyStore:
    def __init__(self, factory: SessionFactory, vault: CredentialVault) -> None:
        self.factory, self.vault = factory, vault

    async def prepare(
        self, lease: Lease, purpose: Purpose, reference: GatewayKeyRef, *, now: datetime
    ) -> KeyRecord:
        identity = RunIdentity(
            forge_run_id=lease.run_id, attempt=lease.attempt, generation=lease.generation
        )
        scope = KeyScope(
            identity=identity,
            purpose=purpose,
            alias=reference.alias,
            model=reference.model,
            expires_at=reference.expires_at,
        )
        if reference.blocked or reference.expires_at <= now:
            raise StoreConflict("new gateway grant must be live and unblocked")
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            row = await session.scalar(
                select(EngineGatewayKey).where(EngineGatewayKey.alias == scope.alias)
            )
            if row is not None:
                record = _record(row)
                if record.scope != scope or record.reference != reference:
                    raise StoreConflict("gateway alias was reused with different input")
                return record
            old = await session.scalars(
                select(EngineGatewayKey).where(
                    EngineGatewayKey.run_id == lease.run_id, EngineGatewayKey.purpose == purpose
                )
            )
            if any(item.state not in ("blocked", "purged") for item in old):
                raise StoreConflict("previous gateway key must be blocked before a new generation")
            secret = self.vault.generate()
            row = EngineGatewayKey(
                id=uuid4().hex,
                run_id=lease.run_id,
                alias=scope.alias,
                attempt=lease.attempt,
                generation=lease.generation,
                purpose=purpose,
                reference_json=reference.model_dump_json(),
                token_digest=hashlib.sha256(secret.encode()).hexdigest(),
                aad_digest=scope.aad_digest,
                expires_at=scope.expires_at,
                state="reserved",
                ciphertext=self.vault.seal(scope, secret),
            )
            session.add(row)
            await session.flush()
            return _record(row)

    async def get(self, alias: str) -> KeyRecord | None:
        async with self.factory() as session:
            row = await session.scalar(
                select(EngineGatewayKey).where(EngineGatewayKey.alias == alias)
            )
            return None if row is None else _record(row)

    async def previous(self, run_id: str, purpose: Purpose) -> tuple[KeyRecord, ...]:
        async with self.factory() as session:
            rows = await session.scalars(
                select(EngineGatewayKey).where(
                    EngineGatewayKey.run_id == run_id, EngineGatewayKey.purpose == purpose
                )
            )
            return tuple(_record(row) for row in rows)

    def reveal(self, record: KeyRecord) -> str:
        if record.ciphertext is None or record.state == "purged":
            raise CredentialUnavailable("gateway credential has been purged")
        return self.vault.open(record.scope, record.ciphertext)

    async def observed(self, alias: str, reference: GatewayKeyRef) -> None:
        async with transaction(self.factory) as session:
            existing = await session.scalar(
                select(EngineGatewayKey).where(EngineGatewayKey.alias == alias)
            )
            if existing is None:
                raise StoreConflict("gateway key was not prepared")
            await locked_run(session, existing.run_id)
            await session.refresh(existing)
            record = _record(existing)
            if (reference.alias, reference.model, reference.expires_at) != (
                record.reference.alias,
                record.reference.model,
                record.reference.expires_at,
            ):
                raise StoreConflict("gateway observation changed immutable scope")
            if not reference.allowance.covers(record.reference.allowance):
                raise StoreConflict("gateway observation cannot reduce an amended allowance")
            if existing.state == "purged" and reference.blocked:
                return
            if existing.state == "purged" or (
                existing.state == "blocked" and not reference.blocked
            ):
                raise StoreConflict("gateway observation cannot revive a retired key")
            existing.reference_json = reference.model_dump_json()
            existing.state = "blocked" if reference.blocked else "active"

    async def purge(self, alias: str) -> None:
        async with transaction(self.factory) as session:
            row = await session.scalar(
                select(EngineGatewayKey).where(EngineGatewayKey.alias == alias)
            )
            if row is None:
                raise StoreConflict("gateway key does not exist")
            await locked_run(session, row.run_id)
            await session.refresh(row)
            if row.state not in ("blocked", "purged"):
                raise StoreConflict("block must be confirmed before purging gateway material")
            row.ciphertext, row.state = None, "purged"

    async def grant(
        self, identity: RunIdentity, gateway_url: str, *, now: datetime
    ) -> ModelKeyGrant:
        async with self.factory() as session:
            run = await session.get(EngineRun, identity.forge_run_id)
            if (
                run is None
                or run.cancel_requested
                or run.state in ("ready_for_human", "blocked", "failed", "cancelled")
                or (run.attempt, run.generation) != (identity.attempt, identity.generation)
            ):
                raise CredentialUnavailable("gateway grant is not current")
            row = await session.scalar(
                select(EngineGatewayKey).where(
                    EngineGatewayKey.run_id == identity.forge_run_id,
                    EngineGatewayKey.attempt == identity.attempt,
                    EngineGatewayKey.generation == identity.generation,
                    EngineGatewayKey.purpose == "implementation",
                )
            )
            if row is None or row.state != "active" or stored_time(row.expires_at) <= now:
                raise CredentialUnavailable("gateway grant is not available")
            record = _record(row)
            return ModelKeyGrant(
                **identity.model_dump(),
                gateway_url=HttpUrl(gateway_url),
                key=self.reveal(record),
                model=record.reference.model,
                expires_at=record.scope.expires_at,
            )
