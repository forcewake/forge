"""Source/execution integrations (ADR-0019).

Provider-specific adapters live here, one module per provider, so the core
(orchestrator, runs, durable) never imports a provider SDK directly until the
v0.5 contracts extraction (docs/specs/contracts-v0.2.md).
"""
