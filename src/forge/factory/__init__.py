"""Factory agents (M2-1): LLM-driven planner, implementer and reviewer.

Built on the thin LiteLLM HTTP client in :mod:`forge.factory.llm` (ADR-0014);
no agno on this path. The agents are pure functions of their inputs plus
GitLab reads — they never touch the controller, never advance run state and
never write to GitLab (the reviewer is strictly read-only).
"""
