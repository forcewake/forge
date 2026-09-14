"""Thin LiteLLM-proxy HTTP client for the factory agents (ADR-0014).

The reactive (Codeward-imported) agents keep going through Agno; the factory
agents (planner, implementer, reviewer) deliberately do not: structured output
through agno proved unreliable with GLM on the live review, so this module is
a plain ``httpx`` client against the LiteLLM proxy's OpenAI-compatible
``/v1/chat/completions`` endpoint. No agno, no litellm-python import.

Every call — successes, HTTP failures, invalid JSON, cancellations — is
recorded in the durable ``llm_calls`` ledger (ADR-0013). Unknown usage stays
``NULL``, never zero: zero would silently falsify totals.

Token budgets are enforced two ways: each consumer truncates its input
to its own ``MAX_INPUT_CHARS`` before calling :meth:`LLMClient.complete`
(deterministically, via :func:`truncate_chars`), and a run-level budget
handle (:class:`forge.durable.budgets.BudgetGuard`, ADR-0018 §5) reserves
calls/tokens before dispatch and reconciles the provider's real usage
afterwards — a refused reservation raises ``LLMError("budget_exhausted")``
without any HTTP request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable import LLMCall

if TYPE_CHECKING:
    from forge.config import Settings
    from forge.durable.budgets import BudgetGuard, Reservation

logger = logging.getLogger(__name__)

#: The proxy needs no real key; this placeholder keeps httpx headers happy.
_PROXY_API_KEY = "not-needed-for-proxy"

#: Appended to the system prompt on the response_format fallback retry.
_JSON_ONLY_SUFFIX = "\n\nRespond with ONLY a JSON object."

#: Textbook failure strings for the ledger (kept short; never include prompts).
_HTTP_FAILURE = "http_error"
_BAD_RESPONSE = "malformed_response"
_INVALID_JSON = "invalid_json"
_CANCELLED = "cancelled"


class LLMError(Exception):
    """The proxy call itself failed (transport, non-200, malformed body)."""


class LLMResponseError(Exception):
    """The model answered, but the content is not usable JSON."""


@dataclass(frozen=True)
class LLMResult:
    """One successful completion plus its usage counters (None = unknown)."""

    text: str
    input_tokens: int | None
    output_tokens: int | None


def truncate_chars(text: str, limit: int) -> str:
    """Deterministically cut *text* to at most *limit* characters (head kept)."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit]


def parse_json(text: str) -> dict:
    """Parse a JSON object out of a model response.

    Strips Markdown code fences, then extracts the first balanced ``{...}``
    block (string- and escape-aware) and ``json.loads`` it. Raises
    :class:`LLMResponseError` on any failure — never returns a guess.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Drop the opening fence line (```json) and a trailing fence.
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        cleaned = cleaned.strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    start = cleaned.find("{")
    if start == -1:
        raise LLMResponseError("no JSON object found in model response")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                block = cleaned[start : index + 1]
                try:
                    parsed = json.loads(block)
                except json.JSONDecodeError as exc:
                    raise LLMResponseError(f"model response is not valid JSON: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise LLMResponseError("model response JSON is not an object")
                return parsed
    raise LLMResponseError("unbalanced JSON object in model response")


async def record_llm_call(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    flow_run_id: str | None,
    role: str,
    model: str,
    status: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    """Write one ``llm_calls`` ledger row (ADR-0013).

    Best-effort: a ledger write failure is logged, never allowed to corrupt
    the caller's outcome (the run state machine stays authoritative).
    """
    try:
        async with session_factory() as session:
            session.add(
                LLMCall(
                    flow_run_id=flow_run_id,
                    role=role,
                    provider="litellm-proxy",
                    model=model,
                    status=status,
                    # Unknown usage stays NULL — never zero (ADR-0013).
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=duration_ms,
                    error=(error[:2000] if error else None),
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to record llm_calls row (role=%s)", role)


class LLMClient:
    """Async httpx client for the LiteLLM proxy's chat-completions endpoint.

    With an optional *budget* handle (:class:`forge.durable.budgets.BudgetGuard`)
    every completion is budget-enforced (ADR-0018 §5): a hold for one call and
    the ``max_tokens`` estimate is reserved BEFORE the provider is contacted —
    a refusal raises :class:`LLMError` ``"budget_exhausted"`` without any HTTP
    request — and the provider's real usage reconciles the hold afterwards.
    A dispatch that fails or is cancelled still consumes its call (the
    provider may have processed it) with unknown tokens.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        timeout: float = 120.0,
        budget: BudgetGuard | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._budget = budget
        self._client = httpx.AsyncClient(
            base_url=settings.LITELLM_URL.rstrip("/"),
            headers={
                "Authorization": f"Bearer {_PROXY_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def set_budget(self, budget: BudgetGuard | None) -> None:
        """Bind (or clear) the run budget — per-run rebinding after the
        RunSpec freeze (the client outlives a single run on shared agents)."""
        self._budget = budget

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def complete(
        self,
        *,
        tier: str,
        system: str,
        user: str,
        role: str,
        flow_run_id: str | None,
        json_mode: bool = False,
        max_tokens: int = 4096,
    ) -> LLMResult:
        """One chat completion against the proxy tier.

        With ``json_mode`` the request carries ``response_format``; if the
        proxy/provider rejects it with 400 the call retries once without it,
        asking for raw JSON in the system prompt. The response text of a
        ``json_mode`` call is validated with :func:`parse_json` before the
        call counts as successful.

        With a budget handle the call is reserved first (1 call + a
        ``max_tokens`` token estimate); a refused reservation raises
        :class:`LLMError` ``"budget_exhausted"`` before any HTTP request.
        """
        started = time.monotonic()
        reservation = await self._reserve_for_call(
            tier=tier, role=role, flow_run_id=flow_run_id, max_tokens=max_tokens, started=started
        )
        try:
            text, usage = await self._dispatch(
                tier=tier,
                system=system,
                user=user,
                json_mode=json_mode,
                max_tokens=max_tokens,
            )
        except asyncio.CancelledError:
            # The request may still have been processed provider-side: the
            # call is consumed, the token actuals stay unknown (never zero).
            await self._reconcile_dispatch(reservation, None, None)
            await self._journal(
                flow_run_id, role, tier, "cancelled", None, None, started, _CANCELLED
            )
            raise
        except LLMError as exc:
            await self._reconcile_dispatch(reservation, None, None)
            await self._journal(flow_run_id, role, tier, "failed", None, None, started, str(exc))
            raise

        if json_mode:
            try:
                parse_json(text)
            except LLMResponseError as exc:
                await self._reconcile_dispatch(reservation, *usage)
                await self._journal(
                    flow_run_id, role, tier, "failed", *usage, started, f"{_INVALID_JSON}: {exc}"
                )
                raise

        await self._reconcile_dispatch(reservation, *usage)
        await self._journal(flow_run_id, role, tier, "ok", *usage, started, None)
        return LLMResult(text=text, input_tokens=usage[0], output_tokens=usage[1])

    # ------------------------------------------------------------------

    async def _reserve_for_call(
        self,
        *,
        tier: str,
        role: str,
        flow_run_id: str | None,
        max_tokens: int,
        started: float,
    ) -> Reservation | None:
        """The pre-dispatch budget hold, or ``None`` when unrestricted.

        A refused reservation never reaches the provider: it is journaled as a
        failed row (unknown usage — nothing was spent) and
        ``LLMError("budget_exhausted")`` is raised.
        """
        if self._budget is None:
            return None
        reservation = await self._budget.reserve(calls=1, tokens=max_tokens)
        if reservation is not None:
            return reservation
        await self._journal(
            flow_run_id, role, tier, "failed", None, None, started, "budget_exhausted"
        )
        raise LLMError("budget_exhausted")

    async def _reconcile_dispatch(
        self,
        reservation: Reservation | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        """Settle the hold against actuals; best-effort, never raises.

        The known usage parts sum to the token actual; unknown parts add
        nothing (unknown is never counted as zero). A reconciliation failure
        is logged and swallowed — the ledger-write posture of
        :func:`record_llm_call`: the call outcome stays authoritative.
        """
        if self._budget is None or reservation is None:
            return
        known = [value for value in (input_tokens, output_tokens) if isinstance(value, int)]
        actual_tokens = sum(known) if known else None
        try:
            await self._budget.reconcile(reservation, actual_calls=1, actual_tokens=actual_tokens)
        except Exception:
            logger.exception(
                "Failed to reconcile budget reservation %s — the hold stays reserved",
                reservation.id,
            )

    async def _dispatch(
        self,
        *,
        tier: str,
        system: str,
        user: str,
        json_mode: bool,
        max_tokens: int,
    ) -> tuple[str, tuple[int | None, int | None]]:
        """Send the request(s); return ``(content, (input_tokens, output_tokens))``.

        Raises :class:`LLMError` on transport/protocol failures and
        :class:`LLMResponseError` when the body has no usable content.
        """
        # The proxy resolves the bare tier name ("fast"/"strong"/"code").
        # The openai/ prefix seen in forge.llm.provider is a litellm-python
        # client detail — over raw HTTP the prefixed name 400s.
        payload: dict[str, Any] = {
            "model": tier,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        response = await self._post(payload)
        if response.status_code == 400 and json_mode:
            # Proxy/provider rejected response_format — retry once, raw JSON.
            payload.pop("response_format")
            payload["messages"][0]["content"] = system + _JSON_ONLY_SUFFIX
            response = await self._post(payload)
        if response.status_code != 200:
            raise LLMError(
                f"litellm proxy returned {response.status_code}: "
                f"{truncate_chars(response.text, 500)}"
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"{_BAD_RESPONSE}: {exc}") from exc
        if not isinstance(content, str):
            raise LLMError(f"{_BAD_RESPONSE}: content is {type(content).__name__}")

        usage = self._usage_tokens(body)
        return content, usage

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self._client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"{_HTTP_FAILURE}: {exc}") from exc

    @staticmethod
    def _usage_tokens(body: dict[str, Any]) -> tuple[int | None, int | None]:
        usage = body.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        return (
            input_tokens if isinstance(input_tokens, int) else None,
            output_tokens if isinstance(output_tokens, int) else None,
        )

    async def _journal(
        self,
        flow_run_id: str | None,
        role: str,
        tier: str,
        status: str,
        input_tokens: int | None,
        output_tokens: int | None,
        started: float,
        error: str | None,
    ) -> None:
        if self._session_factory is None:
            return
        await record_llm_call(
            self._session_factory,
            flow_run_id=flow_run_id,
            role=role,
            # The ledger stores the tier as configured; the openai/ prefix is
            # a wire detail of the proxy.
            model=tier,
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )
