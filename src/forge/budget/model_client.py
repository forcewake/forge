"""A server planner request is admitted durably before the model port is called."""

import asyncio

from forge.domain.budget import Observation, BudgetExhausted
from forge.budget.admission import StrictGatewayAdmission
from forge.port_values import ModelReply
from forge.ports import Clock, ModelClient
from forge.store.budget_calls import ModelCallStore
from forge.store.records import Lease


class BudgetedModelClient:
    def __init__(
        self,
        inner: ModelClient,
        calls: ModelCallStore,
        lease: Lease,
        clock: Clock,
        admission: StrictGatewayAdmission,
    ) -> None:
        self.inner, self.calls, self.lease, self.clock = inner, calls, lease, clock
        self.admission = admission

    async def complete(
        self, *, model: str, system: str, prompt: str, credential_alias: str, max_output_tokens: int
    ) -> ModelReply:
        refused = await self.admission.readiness(model)
        if refused is not None:
            raise BudgetExhausted(refused.reason)
        if type(max_output_tokens) is not int or not 0 < max_output_tokens <= 1048576:
            raise ValueError("model output bound must be positive and finite")
        # The text-only port has no images or opaque tokenizer inputs. Reserve every
        # UTF-8 input byte plus the output bound; observed overruns still block admission.
        tokens = len(system.encode("utf-8")) + len(prompt.encode("utf-8")) + max_output_tokens
        source = await self.calls.admit(
            self.lease, credential_alias, model, tokens, now=self.clock.now()
        )
        try:
            reply = await self.inner.complete(
                model=model,
                system=system,
                prompt=prompt,
                credential_alias=credential_alias,
                max_output_tokens=max_output_tokens,
            )
        except BaseException:
            await asyncio.shield(self.calls.observe(source, None))
            raise
        usage = Observation(usd=reply.usage.usd, calls=reply.usage.calls, tokens=reply.usage.tokens)
        await self.calls.observe(source, usage)
        return reply
