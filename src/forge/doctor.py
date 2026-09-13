"""``forge doctor``: machine-checkable environment verification (M4).

One command an agent (or a human) can run after setup and until it exits
zero. Checks are read-only and never print secret values — CI variable and
token checks report names/presence only.

Usage::

    uv run python -m forge.doctor                 # human-readable report
    uv run python -m forge.doctor --json          # machine-readable
    uv run python -m forge.doctor --project 68    # + target-project checks

Exit codes: 0 = all checks passed, 1 = at least one failed, 2 = usage error.
Warnings do not affect the exit code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass

import httpx

from forge.config import Settings
from forge.gitlab.client import GitLabClient, GitLabAPIError
from forge.runs.service import forge_token

PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str


def _result(name: str, ok: bool | None, ok_detail: str, fail_detail: str) -> CheckResult:
    """ok=True → pass, False → fail, None → warn (usable but not ideal)."""
    if ok is True:
        return CheckResult(name, PASS, ok_detail)
    if ok is False:
        return CheckResult(name, FAIL, fail_detail)
    return CheckResult(name, WARN, ok_detail)


async def check_gitlab(settings: Settings) -> CheckResult:
    """The admin/operator token authenticates against the GitLab instance."""
    try:
        async with GitLabClient(
            base_url=settings.GITLAB_URL, token=settings.GITLAB_TOKEN.get_secret_value()
        ) as gitlab:
            resp = await gitlab._get("/user")
            username = resp.json().get("username", "?")
        return _result("gitlab.token", True, f"authenticated as @{username}", "unreachable")
    except Exception as exc:  # noqa: BLE001 — report anything as a failure
        return _result("gitlab.token", False, "", _short(exc))


async def check_bot_token(settings: Settings) -> CheckResult:
    """The bot token (forge's acting identity) authenticates and is distinct."""
    if not settings.FORGE_BOT_TOKEN:
        return _result("forge.bot_token", False, "", "FORGE_BOT_TOKEN is not set")
    try:
        async with GitLabClient(
            base_url=settings.GITLAB_URL, token=forge_token(settings)
        ) as gitlab:
            resp = await gitlab._get("/user")
            username = resp.json().get("username", "?")
        if username == settings.FORGE_BOT_USERNAME:
            return _result("forge.bot_token", True, f"acts as @{username}", "")
        return _result(
            "forge.bot_token",
            None,
            f"acts as @{username}, FORGE_BOT_USERNAME is @{settings.FORGE_BOT_USERNAME}",
            "",
        )
    except Exception as exc:  # noqa: BLE001
        return _result("forge.bot_token", False, "", _short(exc))


async def check_redis(settings: Settings) -> CheckResult:
    from forge.utils.redis_client import RedisManager

    try:
        manager = await RedisManager.from_settings(settings)
        if manager is None:
            return _result("redis", False, "", "REDIS_URL is not set")
        await manager.ping()
        await manager.close()
        return _result("redis", True, str(settings.REDIS_URL), "")
    except Exception as exc:  # noqa: BLE001
        return _result("redis", False, "", _short(exc))


async def check_database(settings: Settings) -> CheckResult:
    from sqlalchemy import text

    from forge.database import get_engine

    endpoint = settings.DATABASE_URL.split("@")[-1].split("///")[-1]
    try:
        engine = get_engine(settings.DATABASE_URL)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            await conn.execute(text("SELECT 1 FROM flow_runs LIMIT 1"))
        await engine.dispose()
        return _result("database", True, endpoint + " (schema applied)", "")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "no such table" in message or "does not exist" in message:
            return _result(
                "database", None, f"{endpoint} reachable but schema missing — run migrations", ""
            )
        return _result("database", False, "", _short(exc))


async def check_litellm(settings: Settings) -> CheckResult:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.LITELLM_URL.rstrip('/')}/health")
        if resp.status_code == 200:
            return _result("litellm", True, str(settings.LITELLM_URL), "")
        return _result("litellm", None, f"reachable, /health returned {resp.status_code}", "")
    except Exception as exc:  # noqa: BLE001
        return _result("litellm", False, "", _short(exc))


async def check_project(settings: Settings, project_id: int) -> list[CheckResult]:
    """Target-project onboarding checks (ADR-0011/0015): webhook, CI vars,
    runner, execution profile. Variable values are never read or printed."""
    results: list[CheckResult] = []
    try:
        async with GitLabClient(
            base_url=settings.GITLAB_URL, token=settings.GITLAB_TOKEN.get_secret_value()
        ) as gitlab:
            project = (await gitlab._get(f"/projects/{project_id}")).json()
            results.append(
                _result(
                    "project.exists",
                    True,
                    str(project.get("path_with_namespace", project_id)),
                    "",
                )
            )

            hooks = (await gitlab._get(f"/projects/{project_id}/hooks")).json()
            results.append(
                _result(
                    "project.webhook",
                    bool(hooks),
                    f"{len(hooks)} hook(s) configured",
                    "no webhooks — notes/pipelines will never reach forge",
                )
            )

            variables = (await gitlab._get(f"/projects/{project_id}/variables")).json()
            names = {v.get("key") for v in variables}
            missing = [key for key in ("FORGE_BOT_TOKEN",) if key not in names]
            harness_vars = sorted(
                names
                & {"FORGE_BOT_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ZAI_API_KEY", "FORGE_GROK_AUTH"}
            )
            results.append(
                _result(
                    "project.ci_variables",
                    not missing,
                    f"present: {', '.join(harness_vars) or 'none'}",
                    f"missing: {', '.join(missing)}",
                )
            )

            runners = (await gitlab._get(f"/projects/{project_id}/runners")).json()
            online = [r for r in runners if r.get("active")]
            results.append(
                _result(
                    "project.runner",
                    bool(online),
                    f"{len(online)} active runner(s)",
                    "no active runner — CI jobs will hang in pending",
                )
            )
    except GitLabAPIError as exc:
        results.append(_result("project", False, "", _short(exc)))
    except Exception as exc:  # noqa: BLE001
        results.append(_result("project", False, "", _short(exc)))
    return results


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:200]


async def run_checks(settings: Settings, project_id: int | None = None) -> list[CheckResult]:
    """Core environment checks (+ project checks when requested)."""
    import sys

    results = [
        CheckResult("python.version", PASS, f"{sys.version_info.major}.{sys.version_info.minor}")
    ]
    results.append(await check_gitlab(settings))
    results.append(await check_bot_token(settings))
    results.append(await check_redis(settings))
    results.append(await check_database(settings))
    results.append(await check_litellm(settings))
    if project_id is not None:
        results.extend(await check_project(settings, project_id))
    return results


def format_report(results: list[CheckResult]) -> str:
    icons = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}
    width = max((len(r.name) for r in results), default=0)
    lines = [f"forge doctor — {len(results)} checks", ""]
    for r in results:
        lines.append(f"  {icons[r.status]:<4} {r.name:<{width}}  {r.detail}")
    failed = sum(1 for r in results if r.status == FAIL)
    warned = sum(1 for r in results if r.status == WARN)
    lines.append("")
    lines.append(f"  {failed} failed, {warned} warned, {len(results) - failed - warned} passed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-doctor", description=__doc__)
    parser.add_argument(
        "--project", type=int, default=None, help="also check a target project (id)"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    settings = Settings()  # type: ignore[call-arg]
    results = asyncio.run(run_checks(settings, args.project))
    failed = any(r.status == FAIL for r in results)

    if args.json:
        print(
            json.dumps(
                {
                    "status": "failed" if failed else "ok",
                    "checks": [asdict(r) for r in results],
                },
                indent=2,
            )
        )
    else:
        print(format_report(results))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
