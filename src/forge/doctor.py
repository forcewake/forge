"""``forge doctor``: machine-checkable environment verification (M4).

One command an agent (or a human) can run after setup and until it exits
zero. Checks are read-only and never print secret values — CI variable and
token checks report names/presence only.

Usage::

    uv run python -m forge.doctor                 # human-readable report
    uv run python -m forge.doctor --json          # machine-readable
    uv run python -m forge.doctor --project 68    # + target-project checks
    uv run python -m forge.doctor --capabilities  # the capability matrix (NXT-02)
    uv run python -m forge.doctor --capabilities --strict  # + the promotion gate (R28-27)
    uv run python -m forge.doctor --support-matrix         # per (driver, provider, recipe) support (NEXT-26)

Exit codes: 0 = all checks passed, 1 = at least one failed, 2 = usage error.
Warnings do not affect the exit code. ``--capabilities`` is offline: it prints
the reachability-based capability manifest (no environment contacts) and exits
nonzero if the manifest fails its own honesty validation. ``--strict`` adds the
promotion gate: a row claiming a tier whose required evidence class is missing
(an unexecuted cross-runner test cannot produce a cross-runner support badge)
exits 1 as well. ``--capabilities --json`` additionally carries the
``support_matrix`` field (NEXT-26: per (driver, provider, recipe) support from
executed profile evidence); ``--support-matrix`` prints it standalone and exits
1 when the matrix reports evidence problems (a registration citing an artifact
the checkout cannot show).
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
            # ADR-0016: the proposal-only lane must NOT carry a write token
            # (the trusted publisher is the only writer). The read-only fetch
            # token is optional — the runner's own credential suffices.
            forbidden = sorted(names & {"FORGE_BOT_TOKEN"})
            harness_vars = sorted(
                names
                & {
                    "FORGE_BOT_READ_TOKEN",
                    "ANTHROPIC_AUTH_TOKEN",
                    "ZAI_API_KEY",
                    "FORGE_GROK_AUTH",
                }
            )
            if forbidden:
                detail = (
                    "write token in the untrusted lane: "
                    f"{', '.join(forbidden)} — remove it (ADR-0016)"
                )
            elif "FORGE_BOT_READ_TOKEN" not in names:
                detail = "no FORGE_BOT_READ_TOKEN — the lane fetches with the runner credential"
            else:
                detail = ""
            results.append(
                _result(
                    "project.ci_variables",
                    not forbidden,
                    f"present: {', '.join(harness_vars) or 'none'}",
                    detail,
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

            # ADR-0023 §8: per-driver lane checks over the project's
            # preference (variable names only), additive to the JSON output.
            results.extend(check_harness_lanes(settings, names))
    except GitLabAPIError as exc:
        results.append(_result("project", False, "", _short(exc)))
    except Exception as exc:  # noqa: BLE001
        results.append(_result("project", False, "", _short(exc)))
    return results


def check_harness_lanes(settings: Settings, variable_names: set[str]) -> list[CheckResult]:
    """ADR-0023 §8: per-driver lane checks over the project's preference.

    Reports, for every preference entry, whether its required harness
    credential variables are present (variable NAMES only — values are
    never read, ADR-0015 §4), plus the compilable chain = preference ∩
    lanes-with-creds. Missing credentials are a warning (the chain shrinks;
    a lane without creds fails infrastructure at dispatch, which with the
    fallback switch OFF blocks the run visibly) — a hard failure only when
    the configured backend dispatches a harness and NOTHING is compilable.
    """
    from forge.config import ForgeConfig
    from forge.runs.backends import is_harness_backend
    from forge.runs.harness_selection import (
        DRIVER_CREDENTIAL_VARS,
        current_driver,
        resolve_preference,
        validate_preference,
    )

    backend = str(getattr(settings, "FORGE_IMPLEMENTER_BACKEND", "") or "").strip()
    try:
        preference = resolve_preference(ForgeConfig(), settings)
        validate_preference(
            preference, current_driver(backend) if is_harness_backend(backend) else None
        )
    except ValueError as exc:
        # A contradictory preference is a config error the operator fixes —
        # refuse to guess a chain from it (forge never silently repairs).
        return [
            _result("project.harness_preference", False, "", _short(exc)),
        ]

    if not preference:
        preference = [current_driver(backend)]

    results: list[CheckResult] = []
    chain: list[str] = []
    for driver in preference:
        required = DRIVER_CREDENTIAL_VARS.get(driver, ())
        missing = [name for name in required if name not in variable_names]
        if missing:
            results.append(
                _result(
                    f"project.harness.{driver}",
                    None,
                    "missing credential variable(s): "
                    f"{', '.join(missing)} — lane unusable until onboarded",
                    "",
                )
            )
            continue
        chain.append(driver)
        present = (
            f"credentials present: {', '.join(required)}" if required else "no credentials needed"
        )
        results.append(_result(f"project.harness.{driver}", True, present, ""))

    if chain:
        results.append(_result("project.harness_chain", True, "[" + ", ".join(chain) + "]", ""))
    elif is_harness_backend(backend):
        results.append(
            _result(
                "project.harness_chain",
                False,
                "",
                "no preference driver has credentials — every dispatch fails infrastructure",
            )
        )
    else:
        # The builtin backend dispatches no harness: the chain is a
        # declaration for later, not a broken lane today.
        results.append(
            _result(
                "project.harness_chain",
                None,
                "empty chain (preference ∩ lanes-with-creds) — harmless while the "
                "backend is builtin",
                "",
            )
        )
    return results


async def check_azure_devops(settings: Settings) -> list[CheckResult]:
    """Azure DevOps adapter checks (WARN-level when the adapter is off)."""
    # getattr-guards: the check must tolerate stub settings objects the
    # same way the optional GitHub checks do.
    if not getattr(settings, "FORGE_AZDO_ENABLED", False):
        return [_result("azdo.enabled", None, "disabled (FORGE_AZDO_ENABLED=false)", "")]

    results: list[CheckResult] = []
    org_url = str(getattr(settings, "FORGE_AZDO_ORG_URL", "") or "").rstrip("/")
    pat = getattr(settings, "FORGE_AZDO_PAT", None)
    if not org_url or pat is None:
        results.append(
            _result("azdo.credentials", False, "", "FORGE_AZDO_ORG_URL / FORGE_AZDO_PAT not set")
        )
        return results

    # Cheap authenticated probe: a project list also proves the PAT is not
    # scope-trapped (scope problems surface as 203 Non-Authoritative).
    import base64

    auth = base64.b64encode(f":{pat.get_secret_value()}".encode()).decode()
    try:
        resp = await httpx.AsyncClient(timeout=15).get(
            f"{org_url}/_apis/projects",
            params={"api-version": "7.1", "$top": "1"},
            headers={"Authorization": f"Basic {auth}"},
        )
        if resp.status_code == 200:
            results.append(_result("azdo.pat", True, f"authenticated against {org_url}", ""))
        elif resp.status_code in (401, 203):
            results.append(
                _result(
                    "azdo.pat",
                    False,
                    "",
                    f"HTTP {resp.status_code} — check the PAT (expiry, org scope, encoding)",
                )
            )
        else:
            results.append(_result("azdo.pat", False, "", f"HTTP {resp.status_code}"))
    except Exception as exc:  # noqa: BLE001
        results.append(_result("azdo.pat", False, "", _short(exc)))

    webhook_ready = bool(getattr(settings, "FORGE_AZDO_WEBHOOK_USERNAME", "")) and bool(
        getattr(settings, "FORGE_AZDO_WEBHOOK_PASSWORD", None)
    )
    results.append(
        _result(
            "azdo.webhook_credentials",
            webhook_ready,
            "Basic credentials configured for ingress validation",
            "FORGE_AZDO_WEBHOOK_USERNAME / FORGE_AZDO_WEBHOOK_PASSWORD not set — "
            "the ingress answers 503",
        )
    )
    results.append(
        _result(
            "azdo.lane_pipeline",
            getattr(settings, "FORGE_AZDO_LANE_PIPELINE_ID", None) is not None,
            f"lane pipeline id {getattr(settings, 'FORGE_AZDO_LANE_PIPELINE_ID', None)}",
            "FORGE_AZDO_LANE_PIPELINE_ID not set — /go runs the builtin lane only",
        )
    )
    return results


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:200]


def check_capabilities() -> CheckResult:
    """NXT-02: the reachability-based capability manifest holds honestly.

    Re-validates the seeded registry on every doctor run — including the
    routed-command cross-check against the LIVE ingress sets — so doctor
    never reports an unavailable command as supported, and a removed
    application binding surfaces here instead of as a silent over-claim.
    """
    from forge.capability_manifest import capabilities, manifest_problems

    rows = capabilities()
    problems = manifest_problems(rows)
    detail = f"{len(rows)} rows, {sum(1 for r in rows if r.entry_point is None)} not wired"
    if problems:
        return _result("capabilities.manifest", False, "", f"{detail} — " + "; ".join(problems[:3]))
    return _result("capabilities.manifest", True, detail, "")


def _capabilities_mode(as_json: bool, strict: bool = False) -> int:
    """``--capabilities``: print the matrix (NXT-02); exit 1 on drift.

    R28-27 ``--strict`` adds the PROMOTION GATE: every row claiming a
    tier above ``domain_contract`` must hold the evidence classes that
    tier requires (``classify_evidence`` over its pointers), and an
    over-claim fails the run — a capability cannot claim a tier its
    evidence class cannot support.

    NEXT-26: the JSON form additionally carries the ``support_matrix``
    field (per (driver, provider, recipe) support folded from the
    manifest, the live registrations and the provenance reports). Its
    problems do not change THIS mode's exit code — the standalone
    ``--support-matrix`` owns that verdict.
    """
    import json as _json

    from forge.adaptive.support_matrix import support_matrix
    from forge.capability_manifest import (
        TIER_LEGEND,
        capabilities,
        format_matrix,
        manifest_gate_problems,
        manifest_problems,
    )

    rows = capabilities()
    problems = manifest_problems(rows)
    gate_problems = manifest_gate_problems(rows) if strict else []
    if as_json:
        print(
            _json.dumps(
                {
                    "status": "failed" if problems or gate_problems else "ok",
                    "legend": TIER_LEGEND,
                    "capabilities": [row.to_json() for row in rows],
                    "problems": problems,
                    "gate_problems": gate_problems,
                    "support_matrix": support_matrix().to_json(),
                },
                indent=2,
            )
        )
    else:
        print(format_matrix(rows))
        if problems:
            print()
            for problem in problems:
                print(f"  DRIFT: {problem}")
        for problem in gate_problems:
            print(f"  GATE: {problem}")
    return 1 if problems or gate_problems else 0


def _support_matrix_mode(as_json: bool) -> int:
    """``--support-matrix``: the per-(driver, provider, recipe) matrix (NEXT-26).

    Offline: folds the capability manifest, the live registrations and
    the provenance reports into one evidence-graded matrix. Exits 1 when
    the matrix reports problems — a registration citing an evidence
    artifact the checkout cannot show is exactly the over-claim this
    mode exists to refuse.
    """
    import json as _json

    from forge.adaptive.support_matrix import format_support_matrix, support_matrix

    matrix = support_matrix()
    if as_json:
        print(_json.dumps(matrix.to_json(), indent=2))
    else:
        print(format_support_matrix(matrix))
    return 1 if matrix.problems else 0


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
    results.extend(await check_azure_devops(settings))
    if project_id is not None:
        results.extend(await check_project(settings, project_id))
    # NXT-02: offline honesty check, appended last so existing ordering
    # guarantees (python.version, gitlab.token first) are untouched.
    results.append(check_capabilities())
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
    parser.add_argument(
        "--capabilities",
        action="store_true",
        help="print the reachability-based capability matrix (NXT-02) and exit; offline",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "with --capabilities: also run the promotion gate (R28-27) — a row"
            " claiming a tier without the matching evidence class fails the run"
        ),
    )
    parser.add_argument(
        "--support-matrix",
        action="store_true",
        help=(
            "print the per-(driver, provider, recipe) support matrix from"
            " executed profile evidence (NEXT-26) and exit; offline"
        ),
    )
    args = parser.parse_args(argv)

    if args.capabilities:
        # Offline mode: no environment contacts, no Settings needed.
        return _capabilities_mode(args.json, strict=args.strict)

    if args.support_matrix:
        # Offline mode: the support matrix owns its own exit verdict.
        return _support_matrix_mode(args.json)

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
