"""Pinned LiteLLM admin contract; all gateway writes are called through Effects."""

from datetime import datetime
from decimal import Decimal, localcontext
import json
import math
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import SecretStr

from forge_protocol import RunIdentity

from forge.budget.credentials import CredentialUnavailable
from forge.budget.strict import ALLOWED_ROUTES, TrustedPolicy
from forge.domain.budget import Observation, Resources
from forge.domain.verdicts import Unknown
from forge.port_values import (
    Applied,
    DefinitelyAbsent,
    Found,
    GatewayKeyRef,
    GatewayOutcome,
    Inconclusive,
    ProbeResult,
    Refused,
)
from forge.ports import Clock
from forge.store.gateway_keys import GatewayKeyStore, KeyRecord

MAX_RESPONSE_BYTES = 1048576


class GatewayUnavailable(ValueError):
    pass


def _usd(value: Decimal) -> float:
    result = float(value)
    if Decimal.from_float(result) > value:
        result = math.nextafter(result, 0)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("positive USD budget cannot be represented by gateway")
    return result


def _evidence(reason: str, status: int | None = None) -> str:
    return json.dumps({"reason": reason, "status": status}, separators=(",", ":"))


class LiteLLMGateway:
    """USD is the native hard axis; calls/tokens are Forge's cumulative accounting."""

    def __init__(
        self,
        base_url: str,
        admin_key: SecretStr,
        keys: GatewayKeyStore,
        clock: Clock,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10,
        strict_policy: bytes | None = None,
        strict_artifact_digest: str | None = None,
    ) -> None:
        url = urlsplit(base_url)
        if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("gateway URL must be an origin without embedded credentials")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "litellm")
        ):
            raise ValueError("gateway requires HTTPS outside explicit local development hosts")
        if not 0 < timeout <= 60:
            raise ValueError("gateway timeout must be bounded")
        self.keys, self.clock, self.timeout = keys, clock, timeout
        self.policy = TrustedPolicy.parse(strict_policy) if strict_policy is not None else None
        self.artifact_digest = strict_artifact_digest
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {admin_key.get_secret_value()}"},
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def readiness(self, model: str) -> Refused | None:
        nonce = uuid4().hex
        if self.policy is not None:
            try:
                status, body = await self._request(
                    "GET", "/forge/budget/readiness", params={"nonce": nonce}
                )
                if status == 200 and self.policy.accepts(body, nonce, model, self.artifact_digest):
                    return None
            except GatewayUnavailable:
                pass
        return Refused(
            reason="strict_budget_enforcement_unavailable",
            evidence=_evidence("trusted_guard_or_pricing_not_verified"),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> tuple[int, object]:
        try:
            async with self._client.stream(method, path, **kwargs) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise GatewayUnavailable("gateway response exceeded bound")
                if not body:
                    return response.status_code, None
                return response.status_code, json.loads(body, parse_float=Decimal)
        except (httpx.HTTPError, ValueError):
            raise GatewayUnavailable("gateway response unavailable") from None

    async def _info(self, record: KeyRecord) -> dict[str, Any] | None:
        status, data = await self._request(
            "GET",
            "/key/list",
            params={
                "key_alias": record.scope.alias,
                "return_full_object": "true",
                "page": 1,
                "size": 2,
            },
        )
        if status != 200 or not isinstance(data, dict):
            raise GatewayUnavailable("gateway alias read unavailable")
        rows, count = data.get("keys"), data.get("total_count")
        if count == 0 and rows == []:
            return None
        if type(count) is not int or count != 1 or not isinstance(rows, list) or len(rows) != 1:
            raise GatewayUnavailable("gateway alias is not unique")
        row = rows[0]
        if not isinstance(row, dict) or row.get("token") != record.token_digest:
            raise GatewayUnavailable("gateway alias resolves to different material")
        return row

    def _metadata(self, record: KeyRecord, allowance: Resources) -> dict[str, Any]:
        return {
            "forge": {
                **record.scope.identity.model_dump(mode="json"),
                "purpose": record.scope.purpose,
                "aad_digest": record.scope.aad_digest,
                "allowance": allowance.model_dump(mode="json"),
                "strict_policy_digest": self.policy.digest if self.policy else None,
            }
        }

    def _verified(
        self, record: KeyRecord, row: dict[str, Any], allowance: Resources
    ) -> GatewayKeyRef:
        cap = row.get("max_budget")
        if isinstance(cap, bool) or not isinstance(cap, (int, Decimal)):
            raise GatewayUnavailable("gateway USD cap is unknown")
        expires = row.get("expires")
        if not isinstance(expires, str):
            raise GatewayUnavailable("gateway expiry is unknown")
        expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry > record.scope.expires_at:
            raise GatewayUnavailable("gateway expiry exceeds the frozen deadline")
        if (
            row.get("key_alias") != record.scope.alias
            or row.get("models") != [record.scope.model]
            or Decimal(cap) != Decimal(str(_usd(allowance.usd)))
            or row.get("metadata") != self._metadata(record, allowance)
            or row.get("allowed_routes") != list(ALLOWED_ROUTES)
            or row.get("max_parallel_requests") != 1
            or row.get("config") not in (None, {})
            or row.get("aliases") not in (None, {})
            or row.get("budget_duration") is not None
            or row.get("team_id") is not None
            or row.get("user_id") is not None
            or type(row.get("blocked")) is not bool
        ):
            raise GatewayUnavailable("gateway key differs from frozen grant")
        return GatewayKeyRef(
            alias=record.scope.alias,
            model=record.scope.model,
            allowance=allowance,
            expires_at=record.scope.expires_at,
            blocked=row["blocked"],
        )

    async def _probe(
        self, alias: str, allowance: Resources | None, *, block: bool = False
    ) -> ProbeResult[GatewayOutcome]:
        record = await self.keys.get(alias)
        if record is None:
            return Inconclusive(
                reason="unprepared_alias", evidence=_evidence("no_durable_material")
            )
        try:
            row = await self._info(record)
            if row is None:
                return DefinitelyAbsent(evidence=_evidence("alias_absent_in_database"))
            reference = self._verified(record, row, allowance or record.reference.allowance)
            if block and not reference.blocked:
                return Inconclusive(reason="key_not_blocked", evidence=_evidence("key_not_blocked"))
            await self.keys.observed(alias, reference)
            return Found(
                value=Applied(value=reference, evidence=_evidence("verified_alias")),
                evidence=_evidence("verified_alias"),
            )
        except (GatewayUnavailable, ValueError):
            return Inconclusive(
                reason="gateway_probe_unavailable", evidence=_evidence("probe_unavailable")
            )

    async def probe_mint(self, alias: str) -> ProbeResult[GatewayOutcome]:
        return await self._probe(alias, None)

    async def probe_update(self, alias: str, allowance: Resources) -> ProbeResult[GatewayOutcome]:
        return await self._probe(alias, allowance)

    async def probe_block(self, alias: str) -> ProbeResult[GatewayOutcome]:
        return await self._probe(alias, None, block=True)

    async def _write(
        self,
        path: str,
        data: dict[str, Any],
        alias: str,
        allowance: Resources,
        *,
        block: bool = False,
    ) -> GatewayOutcome:
        try:
            status, _ = await self._request("POST", path, json=data)
        except GatewayUnavailable:
            return Unknown(evidence=_evidence("write_unobserved"))
        if status in (400, 401, 403, 404, 422):
            return Refused(reason="gateway_refused", evidence=_evidence("gateway_refused", status))
        if status not in (200, 201):
            return Unknown(evidence=_evidence("write_unobserved", status))
        result = await self._probe(alias, allowance, block=block)
        return (
            result.value
            if isinstance(result, Found)
            else Unknown(evidence=_evidence("write_not_verified"))
        )

    async def mint(
        self,
        identity: RunIdentity,
        alias: str,
        model: str,
        allowance: Resources,
        expires_at: datetime,
    ) -> GatewayOutcome:
        readiness = await self.readiness(model)
        if readiness is not None:
            return readiness
        record = await self.keys.get(alias)
        if record is None or (
            record.scope.identity,
            record.scope.model,
            record.reference.allowance,
            record.scope.expires_at,
        ) != (identity, model, allowance, expires_at):
            return Refused(reason="unprepared_gateway_grant", evidence=_evidence("scope_mismatch"))
        seconds = math.floor((expires_at - self.clock.now()).total_seconds() - self.timeout - 1)
        if seconds <= 0 or allowance.usd <= 0:
            return Refused(
                reason="gateway_grant_exhausted", evidence=_evidence("no_live_usd_allowance")
            )
        try:
            secret = self.keys.reveal(record)
            cap = _usd(allowance.usd)
        except (CredentialUnavailable, ValueError):
            return Refused(
                reason="gateway_credential_unavailable",
                evidence=_evidence("credential_unavailable"),
            )
        return await self._write(
            "/key/generate",
            {
                "key": secret,
                "key_alias": alias,
                "max_budget": cap,
                "duration": f"{seconds}s",
                "models": [model],
                "metadata": self._metadata(record, allowance),
                "budget_duration": None,
                "key_type": "default",
                "allowed_routes": list(ALLOWED_ROUTES),
                "max_parallel_requests": 1,
                "blocked": False,
            },
            alias,
            allowance,
        )

    async def update(self, alias: str, allowance: Resources) -> GatewayOutcome:
        record = await self.keys.get(alias)
        if (
            record is None
            or record.state != "active"
            or not allowance.covers(record.reference.allowance)
        ):
            return Refused(
                reason="gateway_update_unavailable", evidence=_evidence("not_active_or_decreasing")
            )
        return await self._write(
            "/key/update",
            {
                "key": record.token_digest,
                "max_budget": _usd(allowance.usd),
                "metadata": self._metadata(record, allowance),
            },
            alias,
            allowance,
        )

    async def block(self, alias: str) -> GatewayOutcome:
        record = await self.keys.get(alias)
        if record is None:
            return Refused(reason="gateway_key_unknown", evidence=_evidence("unprepared_alias"))
        return await self._write(
            "/key/block",
            {"key": record.token_digest},
            alias,
            record.reference.allowance,
            block=True,
        )

    async def read_spend(self, alias: str) -> Observation:
        record = await self.keys.get(alias)
        if record is None:
            raise GatewayUnavailable("gateway key is unknown")
        row = await self._info(record)
        if row is None:
            raise GatewayUnavailable("gateway key disappeared")
        self._verified(record, row, record.reference.allowance)
        if self.policy is None or row.get("blocked") is not True:
            raise GatewayUnavailable("strict native block is required before settlement")
        status, usage = await self._request(
            "GET", "/forge/budget/usage", params={"token": record.token_digest}
        )
        if (
            status != 200
            or not isinstance(usage, dict)
            or usage.get("schema") != "forge.strict-budget-usage/1"
            or usage.get("token_digest") != record.token_digest
            or usage.get("policy_digest") != self.policy.digest
        ):
            raise GatewayUnavailable("durable gateway usage is unavailable")
        settled, held = Decimal(usage["settled_usd"]), Decimal(usage["held_usd"])
        if min(settled, held) < 0 or not settled.is_finite() or not held.is_finite():
            raise GatewayUnavailable("durable gateway money is invalid")
        with localcontext() as context:
            context.prec = 80
            total = settled + held
        if usage.get("complete") is True and held != 0:
            raise GatewayUnavailable("complete usage still has an unresolved hold")
        return Observation(
            usd=total,
            calls=usage.get("calls") if usage.get("complete") is True else None,
            tokens=usage.get("tokens") if usage.get("complete") is True else None,
        )
