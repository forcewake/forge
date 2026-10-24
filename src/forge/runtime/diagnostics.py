"""Bounded read-only installation checks with no credentials in their results."""

import asyncio
from dataclasses import asdict, dataclass
from typing import Literal

from forge.runtime.assembly import Runtime
from forge.store.schema import check_schema

DEADLINE_SECONDS = 30


@dataclass(frozen=True)
class Diagnostic:
    name: str
    status: Literal["pass", "fail"]
    detail: str


@dataclass(frozen=True)
class Report:
    checks: tuple[Diagnostic, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.status == "pass" for check in self.checks)

    def document(self) -> dict[str, object]:
        return {
            "status": "ok" if self.passed else "failed",
            "checks": [asdict(check) for check in self.checks],
        }


async def inspect(runtime: Runtime, repository: str | None = None) -> Report:
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            return await _inspect(runtime, repository)
    except TimeoutError:
        return Report((Diagnostic("deadline", "fail", "installation check exceeded its bound"),))


async def _inspect(runtime: Runtime, repository: str | None) -> Report:
    try:
        await check_schema(runtime.engine)
    except Exception:
        return Report((Diagnostic("schema", "fail", "selected database schema is unavailable"),))
    checks = [Diagnostic("schema", "pass", "selected database schema matches this installation")]
    configured = runtime.config.repositories()
    if repository is not None:
        if repository not in configured:
            return Report((Diagnostic("repository", "fail", "repository is not configured"),))
        configured = {repository: configured[repository]}
    models = {
        model
        for _, entry in configured.values()
        for model in (
            entry.policy.model,
            entry.native_review.budget.model if entry.native_review else None,
        )
        if model is not None
    }
    for model in sorted(models):
        try:
            verified = await runtime.budget.admission.readiness(model) is None
        except Exception:
            verified = False
        checks.append(
            Diagnostic(
                "native_budget:" + model,
                "pass" if verified else "fail",
                "native guard and pricing verified" if verified else "native guard is unavailable",
            )
        )
    for name, (_, entry) in configured.items():
        try:
            native = runtime.connections.registry.named(name)
            head = await native.repository.head(native.ref, entry.policy.target_branch)
        except Exception:
            head = None
        checks.append(
            Diagnostic(
                "repository:" + name,
                "pass" if head is not None else "fail",
                "configured native branch is readable"
                if head is not None
                else "configured native branch is unavailable",
            )
        )
    return Report(tuple(checks))
