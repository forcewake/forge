"""Server text requests use a confined grant after durable per-call admission."""

from collections.abc import Callable
from decimal import Decimal, DecimalException, ROUND_CEILING, localcontext
import hashlib
import json
import os
import re
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from forge.budget.admission import StrictGatewayAdmission
from forge.budget.credentials import CredentialUnavailable
from forge.budget.model_client import BudgetedModelClient
from forge.budget.strict import TrustedPolicy
from forge.domain.budget import Observation, Resources
from forge.port_values import ModelReply
from forge.ports import Clock, ModelClient
from forge.store.budget_calls import ModelCallStore
from forge.store.gateway_keys import GatewayKeyStore
from forge.store.records import Lease, StoreConflict

ServerPurpose = Literal["planning", "closing"]
MAX_BYTES = 1048576


def server_model_timeout(raw: str | None = None) -> float:
    """Bounded wait for one planner or closing-review reply.

    GLM-class models often need longer than 30s. ``FORGE_SERVER_MODEL_TIMEOUT_SECONDS``
    overrides the 120s default and stays within 1..600.
    """
    if raw is None:
        raw = os.environ.get("FORGE_SERVER_MODEL_TIMEOUT_SECONDS", "120")
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("server model timeout must be a number of seconds") from None
    if not 1 <= value <= 600:
        raise ValueError("server model timeout must be from 1 to 600 seconds")
    return value


class ModelUnavailable(ValueError):
    pass


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate model response key")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError("nonfinite model response value")


def _cost(raw: str | None) -> Decimal | None:
    try:
        if raw is None or len(raw) > 128:
            return None
        number = Decimal(raw)
        if not number.is_finite() or number <= 0:
            return None
        with localcontext() as context:
            context.prec = 80
            return number.quantize(Decimal("1e-18"), rounding=ROUND_CEILING)
    except DecimalException:
        return None


def _tokens(body: dict[str, Any]) -> int | None:
    usage = body.get("usage")
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not isinstance(usage, dict) or any(
        type(usage.get(name)) is not int or not 0 <= usage[name] <= 2**63 - 1 for name in fields
    ):
        return None
    return (
        usage["total_tokens"]
        if usage["prompt_tokens"] + usage["completion_tokens"] == usage["total_tokens"]
        else None
    )


class ServerModels:
    """Own one credential-free HTTP connection pool, with a fresh scoped facade per step."""

    def __init__(
        self,
        gateway_url: str,
        keys: GatewayKeyStore,
        calls: ModelCallStore,
        admission: StrictGatewayAdmission,
        policy: TrustedPolicy,
        clock: Clock,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        url = urlsplit(gateway_url)
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("server model gateway must be an origin without credentials")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "litellm")
        ):
            raise ValueError("server model gateway requires HTTPS outside local development")
        if timeout is None:
            timeout = server_model_timeout()
        if not 1 <= timeout <= 600:
            raise ValueError("server model timeout must be from 1 to 600 seconds")
        self.keys, self.calls, self.admission, self.clock = keys, calls, admission, clock
        self.policy = policy
        self._http = httpx.AsyncClient(
            base_url=gateway_url,
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept-Encoding": "identity"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def minimum(self, model: str) -> Resources:
        return Resources(usd=self.policy.minimum_usd(model), calls=1, tokens=1)

    def client(
        self,
        current_lease: Callable[[], Lease],
        *,
        alias: str,
        model: str,
        purpose: ServerPurpose,
    ) -> ModelClient:
        if purpose not in ("planning", "closing") or model not in self.policy.models or not alias:
            raise ValueError("server model scope requires one trusted model and purpose")
        inner = _ScopedText(self._http, self.keys, current_lease, self.clock, alias, model, purpose)
        return BudgetedModelClient(inner, self.calls, current_lease, self.clock, self.admission)


class _ScopedText:
    def __init__(
        self,
        http: httpx.AsyncClient,
        keys: GatewayKeyStore,
        lease: Callable[[], Lease],
        clock: Clock,
        alias: str,
        model: str,
        purpose: ServerPurpose,
    ) -> None:
        self.http, self.keys, self.lease, self.clock = http, keys, lease, clock
        self.alias, self.model, self.purpose = alias, model, purpose

    async def complete(
        self, *, model: str, system: str, prompt: str, credential_alias: str, max_output_tokens: int
    ) -> ModelReply:
        if (model, credential_alias) != (self.model, self.alias):
            raise StoreConflict("server model call differs from its bound route")
        lease, now = self.lease(), self.clock.now()
        record = await self.keys.get(self.alias)
        if record is None or (
            record.scope.identity.forge_run_id,
            record.scope.identity.attempt,
            record.scope.identity.generation,
            record.scope.purpose,
            record.scope.model,
            record.state,
            record.reference.blocked,
        ) != (lease.run_id, lease.attempt, lease.generation, self.purpose, model, "active", False):
            raise CredentialUnavailable("server model grant is not current")
        if record.scope.expires_at <= now:
            raise CredentialUnavailable("server model grant expired")
        document = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_output_tokens,
            "stream": False,
            "num_retries": 0,
            "max_retries": 0,
        }
        payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
        if len(payload) > MAX_BYTES:
            raise ModelUnavailable("server_model_input_limit")
        try:
            async with self.http.stream(
                "POST",
                "/v1/chat/completions",
                content=payload,
                headers={
                    "Authorization": "Bearer " + self.keys.reveal(record),
                    "Content-Type": "application/json",
                },
            ) as response:
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise ModelUnavailable("server_model_response_unavailable")
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise ModelUnavailable("server_model_output_limit")
                return _reply(bytes(raw), response.headers)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, RecursionError):
            raise ModelUnavailable("server_model_response_unavailable") from None


def _reply(raw: bytes, headers: httpx.Headers) -> ModelReply:
    body = json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("choices"), list)
        or len(body["choices"]) != 1
    ):
        raise ModelUnavailable("server_model_output_schema")
    choice = body["choices"][0]
    if not isinstance(choice, dict):
        raise ModelUnavailable("server_model_output_schema")
    message = choice.get("message")
    if (
        choice.get("finish_reason") != "stop"
        or not isinstance(message, dict)
        or message.get("role") != "assistant"
        or not isinstance(message.get("content"), str)
        or message.get("tool_calls")
    ):
        raise ModelUnavailable("server_model_output_incomplete")
    request = headers.get("x-litellm-call-id", "")
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", request) is None:
        request = ""
    return ModelReply(
        text=message["content"],
        usage=Observation(
            usd=_cost(headers.get("x-litellm-response-cost")), calls=1, tokens=_tokens(body)
        ),
        evidence=json.dumps(
            {
                "gateway": "litellm",
                "request": request,
                "response_digest": hashlib.sha256(raw).hexdigest(),
            },
            separators=(",", ":"),
        ),
    )
