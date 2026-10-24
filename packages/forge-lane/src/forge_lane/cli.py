"""The CI entry point. Validation never opens a vendor session or redeems a key."""

import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from forge_protocol import LaneDispatch

from .client import LaneClient
from .driver import Driver
from .errors import ExitCode, LaneFailure
from .runner import Runner
from .supervisor import Timings
from .workspace import Workspace

DRIVERS = {
    "claude_code": "forge_lane.drivers.claude_code",
    "copilot_cli": "forge_lane.drivers.copilot_cli",
}


def load_driver(name: str) -> Driver:
    try:
        module = importlib.import_module(DRIVERS[name])
        return module.create_driver()
    except (KeyError, ImportError, AttributeError):
        raise LaneFailure(
            ExitCode.CONFIGURATION, "driver_unavailable", "Requested driver is not installed"
        ) from None


async def oidc_token(dispatch: LaneDispatch, variable: str, http: httpx.AsyncClient) -> str:
    explicit = os.environ.get(variable)
    if explicit:
        return explicit
    if dispatch.provider != "github":
        raise LaneFailure(
            ExitCode.AUTHENTICATION, "oidc_missing", "Configure a CI OIDC id_token for this lane"
        )
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    credential = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or not credential:
        raise LaneFailure(
            ExitCode.AUTHENTICATION,
            "oidc_missing",
            "GitHub Actions OIDC is unavailable; enable id-token: write",
        )
    audience = str(dispatch.server_url).rstrip("/") + "/lane/" + str(dispatch.nonce)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "audience"]
    url = urlunsplit(parsed._replace(query=urlencode([*query, ("audience", audience)])))
    try:
        response = await http.get(
            url, headers={"Authorization": f"Bearer {credential}"}, follow_redirects=False
        )
        response.raise_for_status()
        token = response.json()["value"]
        if not isinstance(token, str) or not token:
            raise ValueError("Empty token")
        return token
    except (httpx.HTTPError, ValueError, KeyError):
        raise LaneFailure(
            ExitCode.AUTHENTICATION, "oidc_failed", "CI OIDC exchange failed"
        ) from None


async def run(args: argparse.Namespace) -> int:
    try:
        dispatch = LaneDispatch.model_validate_json(args.dispatch.read_bytes())
        workspace = args.workspace.resolve()
        if args.check:
            await Workspace(workspace, dispatch.execution_base_sha).validate()
            print(
                json.dumps(
                    {
                        "protocol": dispatch.protocol,
                        "configuration": "valid",
                        "driver": dispatch.driver,
                    }
                )
            )
            return ExitCode.COMPLETED
        timings = Timings(checkpoint=float(os.environ.get("FORGE_LANE_CHECKPOINT_INTERVAL", "600")))
        driver = load_driver(dispatch.driver)
        async with httpx.AsyncClient(timeout=30.0) as http:
            token = await oidc_token(dispatch, args.oidc_token_env, http)
            result = await Runner(
                dispatch,
                LaneClient(dispatch, http),
                driver,
                workspace,
                timings=timings,
                trace_platform="actions" if dispatch.provider == "github" else "gitlab",
            ).execute(token)
        print(
            json.dumps(
                {
                    "classification": result.classification,
                    "report_accepted": result.report_accepted,
                    "exit_code": int(result.exit_code),
                }
            )
        )
        return result.exit_code
    except LaneFailure as exc:
        print(json.dumps({"classification": exc.classification, "detail": exc.detail}))
        return exc.code
    except (OSError, ValueError):
        print(json.dumps({"classification": "invalid_configuration"}))
        return ExitCode.CONFIGURATION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-lane")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("run", help="Execute one authenticated lane generation")
    command.add_argument("--dispatch", type=Path, required=True)
    command.add_argument("--workspace", type=Path, default=Path.cwd())
    command.add_argument("--oidc-token-env", default="FORGE_OIDC_TOKEN")
    command.add_argument(
        "--check",
        action="store_true",
        help="Validate local configuration without network or driver calls",
    )
    try:
        return int(asyncio.run(run(parser.parse_args(argv))))
    except KeyboardInterrupt:
        return 130
