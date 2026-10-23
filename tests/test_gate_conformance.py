"""Real dispatch payloads must fit the shipped native CI surfaces."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "ci" / "templates"


class DeclaringGitHubClient:
    """A wrapper around the REAL GitHubClient (pointed at the
    production-entry fake native server) that REFUSES a workflow dispatch
    whose inputs fall outside the shipped template's declared surface.

    The sibling fake's ledger accepts anything — that permissiveness is
    the recorded failure mode; this wrapper is the early guard, and it
    records the exact payload key sets the capture fixture freezes.
    """

    def __init__(self, inner: Any, declared: set[str]) -> None:
        self._inner = inner
        self._declared = declared
        self.captured_inputs: list[dict[str, str]] = []

    async def dispatch_workflow(self, owner, repo, workflow_filename, ref, inputs=None):
        payload = dict(inputs or {})
        self.captured_inputs.append(dict(payload))
        undeclared = sorted(set(payload) - self._declared)
        if undeclared:
            from forge.integrations.github import GitHubAPIError

            raise GitHubAPIError(
                422,
                f"undeclared workflow_dispatch inputs {undeclared} — the shipped "
                "template does not declare them (conformance capture wrapper)",
            )
        return await self._inner.dispatch_workflow(
            owner, repo, workflow_filename, ref, inputs=payload
        )

    async def get_contents_file(self, owner, repo, path, ref):
        if path == ".github/workflows/forge-harness.github.yml":
            import base64

            content = (TEMPLATES_DIR / "forge-harness.github.yml").read_bytes()
            return {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(content).decode(),
                "size": len(content),
                "sha": "a" * 40,
            }
        return await self._inner.get_contents_file(owner, repo, path, ref)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class DeclaringGitLabClient:
    """The same guard on the GitLab pipeline-trigger boundary: every
    dispatched pipeline variable must be consumed by at least one shipped
    recipe (GitLab declares nothing in YAML — the recipes' consumed
    surface IS the contract)."""

    def __init__(self, inner: Any, consumed: set[str]) -> None:
        self._inner = inner
        self._consumed = consumed
        self.captured_keys: list[list[str]] = []

    async def create_pipeline(self, project_id, ref, variables=None):
        keys = [str(entry["key"]) for entry in (variables or [])]
        self.captured_keys.append(keys)
        orphaned = sorted(set(keys) - self._consumed)
        if orphaned:
            from forge.gitlab.client import GitLabAPIError

            raise GitLabAPIError(
                422,
                f"dispatched pipeline variables no shipped recipe consumes: "
                f"{orphaned} (conformance capture wrapper)",
            )
        return await self._inner.create_pipeline(project_id, ref, variables=variables)

    # The config reader probes the CLASS for the typed ``read_blob`` seam
    # (``hasattr(type(client), "read_blob")``) — a pure ``__getattr__``
    # delegator hides it and turns a provider-confirmed 404 into an
    # "unreadable" park. Delegate it explicitly.
    async def read_blob(self, project_id, file_path, ref="HEAD"):
        return await self._inner.read_blob(project_id, file_path, ref=ref)

    async def lint_ci_configuration(self, project_id, ref):
        return {"valid": True, "jobs": [{"name": "forge-agent-claude-code", "when": "on_success"}]}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _declared_github_inputs() -> set[str]:
    doc = yaml.safe_load((TEMPLATES_DIR / "forge-harness.github.yml").read_text(encoding="utf-8"))
    trigger = doc.get("on", doc.get(True))
    return set(trigger["workflow_dispatch"]["inputs"])


def _consumed_gitlab_variables() -> set[str]:

    consumed: set[str] = set()
    pattern = re.compile(r"\bFORGE_[A-Z0-9_]+")
    for path in sorted(TEMPLATES_DIR.glob("*.gitlab-ci.yml")):
        consumed |= set(pattern.findall(path.read_text(encoding="utf-8")))
    return consumed


@contextlib.contextmanager
def _env(**values: str):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


@contextlib.contextmanager
def _fake_native(workroot: Path, *, gitlab: bool):
    """The production-entry fake native server as a real subprocess — the
    same launch shape the sibling ``native``/``gitlab_native`` fixtures
    use, reused by import from the conftest module."""
    from tests.production_entry import conftest as pe

    ready = workroot / ("gitlab-native-ready.json" if gitlab else "gh-native-ready.json")
    ready.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        sys.executable,
        str(pe.FAKE_NATIVE_SERVER),
        "--ready-file",
        str(ready),
        "--repo",
        str(pe.GL_PROJECT_ID) if gitlab else pe.PE_REPO,
        "--base-branch",
        pe.GL_BASE_BRANCH if gitlab else pe.PE_BASE_BRANCH,
        "--base-sha",
        pe.GL_BASE_SHA if gitlab else pe.PE_BASE_SHA,
    ]
    if gitlab:
        arguments += ["--api", "gitlab"]
    process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20.0
    try:
        while not ready.is_file():
            if process.poll() is not None:
                raise AssertionError("the fake native server died at startup")
            if time.monotonic() > deadline:
                raise AssertionError("the fake native server never became ready")
            time.sleep(0.02)
        port = int(json.loads(ready.read_text(encoding="utf-8"))["port"])
        server = pe.FakeGitLabNative(process, port) if gitlab else pe.FakeNative(process, port)
        yield server
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover — a wedged child
            process.kill()
            process.wait(timeout=10)


class _StaticTokens:
    def __init__(self, token: str) -> None:
        self._token = token

    async def token(self) -> str:
        return self._token

    async def invalidate(self) -> None:
        return None


async def _capture_github(workroot: Path) -> dict[str, list[str]]:
    """The REAL GitHubRunService through the REAL fake native server,
    under the three delivery profiles — inputs recorded at the HTTP
    boundary by the declaring wrapper."""
    from forge.adaptive.credential_broker import (
        DELIVERY_MODE_GITHUB_NATIVE,
        DELIVERY_MODE_RUNNER_REDEMPTION,
    )
    from tests.production_entry import conftest as pe

    shapes: dict[str, list[str]] = {}
    with _fake_native(workroot, gitlab=False) as native:
        native.seed_issue(42, "Add the widget", "Body")
        for shape, delivery in (
            ("unbound", None),
            ("bound_native", DELIVERY_MODE_GITHUB_NATIVE),
            ("bound_redemption", DELIVERY_MODE_RUNNER_REDEMPTION),
        ):
            # a FRESH database per shape: the previous shape's run is
            # still parked waiting_harness and would occupy the project's
            # execution slot (one dispatch per drive, honestly isolated)
            database = pe.PEDatabase(f"sqlite+aiosqlite:///{workroot / f'gh-{shape}.db'}")
            await database.create_schema()
            try:
                shapes[shape] = await _drive_github_shape(native, database, delivery=delivery)
            finally:
                await database.dispose()
    return shapes


async def _drive_github_shape(native: Any, database: Any, *, delivery: str | None) -> list[str]:
    from forge.adaptive.credential_broker import StagedBroker
    from forge.adaptive.operator_snapshot import subject_of_run
    from forge.adaptive.project_credentials import ProjectCredentialRegistry
    from forge.config import ForgeConfig
    from forge.durable import FlowRun
    from forge.integrations.github import GitHubClient, GitHubRepositoryReader
    from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
    from forge.runs.github_service import GitHubRunService
    from forge.runs.stubs import StubImplementer, StubPlanner
    from tests.production_entry import conftest as pe
    from forge.adaptive.credential_broker import (
        DELIVERY_ROUTE_ENV,
        DELIVERY_TEMPLATE_DIR_ENV,
    )

    client = DeclaringGitHubClient(
        GitHubClient(
            base_url=native.base_url,
            token_provider=_StaticTokens("pe-capture-token"),  # noqa: S106
        ),
        _declared_github_inputs(),
    )
    registry = None
    broker = None
    if delivery is not None:
        registry = ProjectCredentialRegistry()
        broker = StagedBroker()
    reader = GitHubRepositoryReader(client, pe.PE_OWNER, pe.PE_REPO_NAME)
    service = GitHubRunService(
        database.worker_factory(),
        pe.pe_settings(),
        ForgeConfig(),
        stack=GitHubAgents(
            client=client,
            reader=reader,
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=pe.StubPRReviewer(),
            flow=GitHubPublishFlow(
                client, proposer=StubImplementer(), base_branch=pe.PE_BASE_BRANCH
            ),
        ),
        repo_full_name=pe.PE_REPO,
        credential_registry=registry,
        credential_broker=broker,
    )
    run_id = await service.start_run(
        project_id=42,
        issue_number=42,
        issue_title="Add the widget",
        issue_description="Body",
        author_username="alice",
    )
    if registry is not None and broker is not None:
        factory = database.worker_factory()
        async with factory() as session:
            row = await session.get(FlowRun, run_id)
            subject = subject_of_run(row)
        registry.bind(subject, "anthropic-gateway", CAPTURE_REF, bound_by="capture")
        broker.stage(CAPTURE_REF, CAPTURE_SECRET, env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
    with _env(
        ANTHROPIC_AUTH_TOKEN=AMBIENT_CAPTURE_VALUE,
        **{DELIVERY_ROUTE_ENV: delivery or "", DELIVERY_TEMPLATE_DIR_ENV: str(TEMPLATES_DIR)},
    ):
        await service.handle_go(
            project_id=42,
            issue_number=42,
            note_text=f"@forge /go {run_id}",
            author_username="alice",
        )
    if not client.captured_inputs:
        factory = database.worker_factory()
        async with factory() as session:
            row = await session.get(FlowRun, run_id)
        raise AssertionError(
            f"the github capture drive never dispatched (status={row.status}, "
            f"reason={row.status_reason})"
        )
    keys = sorted(client.captured_inputs[-1])
    await client.aclose()
    return keys


CAPTURE_REF = "env:ANTHROPIC_AUTH_TOKEN"  # noqa: S105 — a fixture ref, never a value
CAPTURE_SECRET = "conformance-capture-secret"  # noqa: S105 — a fixture value
AMBIENT_CAPTURE_VALUE = "conformance-capture-ambient"  # noqa: S105 — a fixture value


async def _capture_gitlab(workroot: Path) -> dict[str, list[str]]:
    """The REAL (GitLab) RunService through the fake native server's
    GitLab mode — the dispatched pipeline variables per delivery profile."""
    from forge.adaptive.credential_broker import (
        DELIVERY_MODE_GITLAB_PROTECTED,
        DELIVERY_MODE_RUNNER_REDEMPTION,
    )
    from tests.production_entry import conftest as pe

    shapes: dict[str, list[str]] = {}
    with _fake_native(workroot, gitlab=True) as native:
        native.seed_issue(pe.GL_ISSUE_IID, pe.GL_ISSUE_TITLE, pe.GL_ISSUE_DESC)
        for shape, delivery in (
            ("unbound", None),
            ("bound_native", DELIVERY_MODE_GITLAB_PROTECTED),
            ("bound_redemption", DELIVERY_MODE_RUNNER_REDEMPTION),
        ):
            # a FRESH database per shape (one dispatch per drive — the
            # parked previous run would occupy the execution slot)
            database = pe.PEDatabase(f"sqlite+aiosqlite:///{workroot / f'gl-{shape}.db'}")
            await database.create_schema()
            try:
                shapes[shape] = await _drive_gitlab_shape(native, database, delivery=delivery)
            finally:
                await database.dispose()
    return shapes


async def _drive_gitlab_shape(native: Any, database: Any, *, delivery: str | None) -> list[str]:
    from forge.adaptive.credential_broker import (
        DELIVERY_ROUTE_ENV,
        DELIVERY_TEMPLATE_DIR_ENV,
        StagedBroker,
    )
    from forge.adaptive.operator_snapshot import subject_of_run
    from forge.adaptive.project_credentials import ProjectCredentialRegistry
    from forge.config import ForgeConfig
    from forge.durable import FlowRun
    from forge.gitlab.client import GitLabClient
    from forge.runs.service import RunService
    from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
    from tests.production_entry import conftest as pe

    client = DeclaringGitLabClient(
        GitLabClient(base_url=native.base_url, token="pe-capture-token"),  # noqa: S106
        _consumed_gitlab_variables(),
    )
    registry = None
    broker = None
    if delivery is not None:
        registry = ProjectCredentialRegistry()
        broker = StagedBroker()
    service = RunService(
        database.worker_factory(),
        gitlab=client,
        settings=pe.gl_settings(),
        config=ForgeConfig(),
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
        credential_registry=registry,
        credential_broker=broker,
    )
    run_id = await service.start_run(
        pe.GL_PROJECT_ID,
        pe.GL_ISSUE_IID,
        pe.GL_ISSUE_TITLE,
        pe.GL_ISSUE_DESC,
        "alice",
    )
    if registry is not None and broker is not None:
        factory = database.worker_factory()
        async with factory() as session:
            row = await session.get(FlowRun, run_id)
            subject = subject_of_run(row)
        registry.bind(subject, "anthropic-gateway", CAPTURE_REF, bound_by="capture")
        broker.stage(CAPTURE_REF, CAPTURE_SECRET, env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
    with _env(
        ANTHROPIC_AUTH_TOKEN=AMBIENT_CAPTURE_VALUE,
        **{DELIVERY_ROUTE_ENV: delivery or "", DELIVERY_TEMPLATE_DIR_ENV: str(TEMPLATES_DIR)},
    ):
        await service.handle_command_note(
            pe.GL_PROJECT_ID,
            f"@forge /go {run_id}",
            "alice",
            pe.GL_ISSUE_IID,
            author_user_id=11,
        )
    if not client.captured_keys:
        factory = database.worker_factory()
        async with factory() as session:
            row = await session.get(FlowRun, run_id)
        raise AssertionError(
            f"the gitlab capture drive never dispatched (status={row.status}, "
            f"reason={row.status_reason})"
        )
    keys = sorted(client.captured_keys[-1])
    await client.close()
    return keys


async def capture_dispatch_payloads_async(workroot: Path | None = None) -> dict[str, Any]:
    """The recorded production dispatch payload KEY SETS, per provider and
    per delivery profile, captured through the real services and fakes.

    Only KEY SETS are recorded — never values (dispatch payloads can
    carry refs; values never ride them, and the capture keeps it that
    way).
    """
    from datetime import UTC, datetime

    root = workroot or Path(tempfile.mkdtemp(prefix="forge-conformance-capture-"))
    return {
        "schema": "forge.conformance-dispatch-captures/1",
        "captured_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "provenance": (
            "fresh captures: the REAL GitHubRunService/RunService driven "
            "through the production-entry fakes (fake native server subprocess; in-memory "
            "fake native HTTP endpoints), fronted by the declaring wrapper clients in "
            "tests/test_gate_conformance.py — keys only, never values"
        ),
        "providers": {
            "github": {"shapes": await _capture_github(root / "github")},
            "gitlab": {"shapes": await _capture_gitlab(root / "gitlab")},
        },
    }


def capture_dispatch_payloads() -> dict[str, Any]:
    """The sync entry the gate's ``--regenerate-captures`` mode drives."""
    return asyncio.run(capture_dispatch_payloads_async())


async def test_real_dispatches_fit_shipped_native_templates(tmp_path):
    captured = await capture_dispatch_payloads_async(tmp_path)
    assert {"github", "gitlab"} == captured["providers"].keys()
    assert all(captured["providers"][p]["shapes"] for p in ("github", "gitlab"))


async def test_an_undeclared_sent_key_is_refused_before_native_dispatch():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from forge.integrations.github import GitHubAPIError

    native = SimpleNamespace(dispatch_workflow=AsyncMock())
    wrapper = DeclaringGitHubClient(native, {"run_id"})
    with pytest.raises(GitHubAPIError):
        await wrapper.dispatch_workflow("o", "r", "lane.yml", "main", {"unknown": "value"})
    native.dispatch_workflow.assert_not_called()
