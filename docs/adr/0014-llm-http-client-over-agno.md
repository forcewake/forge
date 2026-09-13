# 0014 — LLM HTTP client over Agno for the factory agents

- **Status:** Accepted
- **Date:** 2026-09-13

## Context

The factory agents (planner, implementer, reviewer) need structured output
from the model: the planner returns a plan JSON, the implementer a strict
ChangeSet JSON (ADR-0001), the reviewer a verdict JSON. The reactive
Codeward-imported agents go through Agno's `LiteLLM` model wrapper, so the
first attempt reused the same path for the factory agents.

On the live review against GLM through the LiteLLM proxy, Agno's structured
output handling failed: the wrapper's response-format / parsing layer did not
return usable structured content from this model, while the same model
produced perfectly parseable JSON when asked directly with a strict prompt
and, where supported, `response_format: json_object`. Debugging inside the
wrapper added opacity on a path where forge needs exact control: the
ChangeSet bytes are a security-relevant contract, every call must land in the
usage ledger including failures (ADR-0013), and json-mode fallbacks must be
deterministic and testable.

## Decision

The factory agents use a thin, in-repo HTTP client
(`forge.factory.llm.LLMClient`, plain `httpx`) against the LiteLLM proxy's
OpenAI-compatible `/v1/chat/completions` endpoint:

- model names are the proxy tiers (`fast`/`strong`/`code`) sent as the bare
  tier name — the `openai/` prefix seen in `forge.llm.provider` is a
  litellm-python client detail that the proxy rejects over raw HTTP (verified
  live: `model=openai/fast` 400s, bare `fast` resolves);
- `json_mode` sends `response_format: {"type": "json_object"}` and, on a 400
  rejection, retries once without it while appending a raw-JSON instruction
  to the system prompt;
- responses are parsed by a local strict parser (code-fence stripping, first
  balanced object) that raises instead of guessing;
- every call — success, HTTP failure, invalid JSON, cancellation — writes an
  `llm_calls` ledger row (ADR-0013), with unknown usage as NULL, never zero.

Agno stays isolated behind the reactive path (code review, pipeline
debugging, security triage, chat). No new factory code imports Agno or the
litellm Python package.

## Consequences

- **Positive:** full control over prompts, fallbacks and error paths; every
  failure mode is unit-testable offline; the ledger covers the whole factory
  spend; one dependency (httpx) instead of an agent framework on the
  security-relevant path.
- **Negative:** no agent-framework conveniences (tool loops, retries,
  history management) on this path — forge owns them explicitly; the reactive
  path keeps its Agno quirks until it is migrated or replaced; two model
  access patterns coexist until then.
