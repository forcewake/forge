"""Scripted in-memory LLM fake for the factory-agent tests.

Mirrors :class:`forge.factory.llm.LLMClient.complete` semantics, including the
ADR-0013 ledger behaviour: every call journals an ``llm_calls`` row — ok rows
with usage tokens, failed rows for errors and for invalid JSON in json_mode.
No network, no model.
"""

from __future__ import annotations

import time
from typing import Any

from forge.factory.llm import LLMResult, parse_json, record_llm_call


class FakeLLM:
    """Plays back a script of responses and journals every call.

    Script entries (consumed in order; exhausted script falls back to
    ``default_text``):

    - :class:`LLMResult` — success with explicit usage counters;
    - ``str`` — success with default usage counters (10 in / 5 out);
    - ``Exception`` — the call fails (journals a failed row, raises).
    """

    def __init__(
        self,
        session_factory: Any = None,
        script: list[Any] | None = None,
        default_text: str = "{}",
    ) -> None:
        self.session_factory = session_factory
        self.script = list(script or [])
        self.default_text = default_text
        self.calls: list[dict] = []

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
        self.calls.append(
            {
                "tier": tier,
                "system": system,
                "user": user,
                "role": role,
                "flow_run_id": flow_run_id,
                "json_mode": json_mode,
                "max_tokens": max_tokens,
            }
        )

        started = time.monotonic()
        entry: Any = self.script.pop(0) if self.script else self.default_text
        if isinstance(entry, Exception):
            await self._journal(entry, role, tier, flow_run_id, started, None, None, str(entry))
            raise entry

        if isinstance(entry, LLMResult):
            text, input_tokens, output_tokens = entry.text, entry.input_tokens, entry.output_tokens
        else:
            text, input_tokens, output_tokens = str(entry), 10, 5

        if json_mode:
            try:
                parse_json(text)
            except Exception as exc:
                await self._journal(
                    exc,
                    role,
                    tier,
                    flow_run_id,
                    started,
                    input_tokens,
                    output_tokens,
                    f"invalid_json: {exc}",
                )
                raise

        await self._journal(
            None, role, tier, flow_run_id, started, input_tokens, output_tokens, None
        )
        return LLMResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens)

    async def _journal(
        self,
        error: Exception | None,
        role: str,
        tier: str,
        flow_run_id: str | None,
        started: float,
        input_tokens: int | None,
        output_tokens: int | None,
        error_text: str | None,
    ) -> None:
        if self.session_factory is None:
            return
        await record_llm_call(
            self.session_factory,
            flow_run_id=flow_run_id,
            role=role,
            model=tier,
            status="failed" if error is not None else "ok",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error_text,
        )

    # -- assertion helpers -------------------------------------------------

    def roles(self) -> list[str]:
        return [call["role"] for call in self.calls]

    def calls_for(self, role: str) -> list[dict]:
        return [call for call in self.calls if call["role"] == role]
