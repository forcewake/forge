"""A real lane cannot widen frozen inputs through dispatch, tools or saved files."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256

import pytest

from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import BudgetLimits
from forge.domain.paths import resolve_write_policy
from forge_lane.drivers.claude_code import ClaudeCodeDriver
from forge_lane.drivers.claude_policy import FilePolicy
from forge_lane.drivers.copilot_cli import CopilotCLIDriver
from forge_lane.errors import ExitCode
from forge_protocol import CheckpointEntry, CheckpointManifest, ExecutionPolicy, McpServerPolicy
from tests.test_domain_approved_input import spec
from tests.test_lane_runner_v1 import FakeDriver, WireServer, checkout, execute, inputs


def frozen_inputs(base, *, generation=0, **changes):
    frozen = spec(
        base_sha=base,
        driver=changes.pop("driver", "fake"),
        limits=BudgetLimits(usd=Decimal("10"), calls=20, tokens=10000, wallclock_seconds=10),
        **changes,
    )
    dispatch, old = inputs(base, generation=generation, driver=frozen.driver)
    approved = ApprovedInput.from_spec(frozen)
    dispatch = dispatch.model_copy(update={"approved_digest": approved.approved_digest})
    return dispatch, approved.to_brief(old, frozen)


class PolicyDriver(FakeDriver):
    requires_model_key = True

    async def prepare(self, brief, workspace):
        assert brief.frozen_spec is not None


@pytest.mark.parametrize(
    "field,value", [("driver", "different"), ("model", "different"), ("timeout_seconds", 11)]
)
async def test_dispatch_overrides_refuse_before_grant_or_driver(tmp_path, field, value):
    workspace = tmp_path / "work"
    dispatch, brief = frozen_inputs(checkout(workspace))
    server, driver = WireServer(dispatch, brief), PolicyDriver()
    result = await execute(dispatch.model_copy(update={field: value}), server, driver, workspace)
    assert result.classification == "frozen_dispatch_mismatch"
    assert not driver.starts and server.model_keys == 0


async def test_driver_without_a_policy_consumer_and_slow_preflight_refuse_before_key(tmp_path):
    workspace = tmp_path / "work"
    dispatch, brief = frozen_inputs(checkout(workspace))
    server = WireServer(dispatch, brief)

    class WithoutPolicy(FakeDriver):
        prepare = None

    result = await execute(dispatch, server, WithoutPolicy(), workspace)
    assert result.classification == "policy_capability_unavailable" and server.model_keys == 0

    class Slow(PolicyDriver):
        async def prepare(self, brief, workspace):
            await asyncio.Future()

    dispatch = dispatch.model_copy(update={"timeout_seconds": 1})
    document = spec(
        base_sha=dispatch.base_sha,
        driver="fake",
        limits=BudgetLimits(usd=Decimal("10"), calls=20, tokens=10000, wallclock_seconds=1),
    )
    approved = ApprovedInput.from_spec(document)
    dispatch = dispatch.model_copy(update={"approved_digest": approved.approved_digest})
    server = WireServer(dispatch, approved.to_brief(brief, document))
    driver = Slow()
    result = await execute(dispatch, server, driver, workspace)
    assert result.exit_code == ExitCode.DEADLINE and result.report.status == "timed_out"
    assert not driver.starts and server.model_keys == 0


def test_frozen_write_scope_and_fixed_authority_denies_both_hold(tmp_path):
    policy = replace(
        resolve_write_policy("code_only"),
        denied_paths=frozenset({"src/private.py"}),
        denied_prefixes=("generated/",),
        denied_globs=("*secret*",),
        permitted_globs=("src/permitted-secret.py", "src/sensitive.py"),
        sensitive_paths=frozenset({"src/sensitive.py"}),
    )
    frozen = spec(write_policy=policy)
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "link.py").symlink_to(workspace / "src/private.py")
    guard = FilePolicy(
        workspace,
        None,
        scope=ApprovedInput.from_spec(frozen)
        .to_brief(inputs(frozen.base_sha)[1], frozen)
        .frozen_spec.write_policy,
    )
    assert guard.permitted("Write", {"file_path": "src/public.py"})
    assert guard.permitted("Write", {"file_path": "src/permitted-secret.py"})
    assert guard.permitted("Read", {"file_path": "src/private.py"})
    for path in (
        "src/private.py",
        "generated/x.py",
        "src/secret.py",
        "src/sensitive.py",
        "link.py",
        "README.md",
        "uv.lock",
        ".mcp.json",
        ".copilot/config.json",
        ".git/config",
        ".env",
    ):
        assert not guard.permitted("Write", {"file_path": path}), path


@pytest.mark.parametrize("kind", ["stdio", "credential", "copilot"])
async def test_unsupported_mcp_refuses_before_grant_or_process(tmp_path, kind, monkeypatch):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    server_policy = McpServerPolicy(
        name="docs", transport="http", tools=("lookup",), url="https://docs.invalid/mcp"
    )
    if kind == "stdio":
        server_policy = McpServerPolicy(
            name="docs", transport="stdio", tools=("lookup",), command="/opt/mcp/docs"
        )
    elif kind == "credential":
        server_policy = server_policy.model_copy(update={"credential_ref": "docs-token"})
    name = "copilot_cli" if kind == "copilot" else "claude_code"
    dispatch, brief = frozen_inputs(
        base,
        driver=name,
        metering="copilot-scoped-unmetered" if name == "copilot_cli" else "metered",
        execution_policy=ExecutionPolicy(mcp_servers=(server_policy,)),
    )
    server = WireServer(dispatch, brief)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "synthetic-explicit-token")

    async def forbidden(*args):
        raise AssertionError("a process must not start")

    driver = (
        CopilotCLIDriver(transport_factory=forbidden) if kind == "copilot" else ClaudeCodeDriver()
    )
    result = await execute(dispatch, server, driver, workspace)
    assert result.classification == "mcp_capability_unavailable" and server.model_keys == 0


@pytest.mark.parametrize("operation", ["edit", "rename"])
async def test_candidate_collection_refuses_out_of_scope_edits_from_a_driver(tmp_path, operation):
    workspace = tmp_path / "work"
    policy = replace(resolve_write_policy("no_dependencies"), denied_paths=frozenset({"base.txt"}))
    dispatch, brief = frozen_inputs(checkout(workspace), write_policy=policy)
    server = WireServer(dispatch, brief)

    def mutate(path, env):
        if operation == "rename":
            (path / "base.txt").rename(path / "renamed.txt")
        else:
            (path / "base.txt").write_text("outside scope\n")

    driver = PolicyDriver(mutate)
    result = await execute(dispatch, server, driver, workspace)
    assert result.classification == "write_policy_refused" and server.model_keys == 1
    assert not server.candidates and not server.blobs


@pytest.mark.parametrize("path", ["base.txt", ".mcp.json"])
async def test_checkpoint_scope_refuses_before_download_write_or_key(tmp_path, path):
    workspace = tmp_path / "work"
    policy = replace(resolve_write_policy("no_dependencies"), denied_paths=frozenset({"base.txt"}))
    dispatch, brief = frozen_inputs(checkout(workspace), generation=1, write_policy=policy)
    data = b"restored forbidden bytes"
    blob = sha256(data).hexdigest()
    manifest = CheckpointManifest(
        forge_run_id=dispatch.forge_run_id,
        attempt=0,
        generation=0,
        base_sha=dispatch.base_sha,
        approved_digest=dispatch.approved_digest,
        sequence=1,
        entries=(
            CheckpointEntry(
                path=path,
                mode="100644",
                sha256=blob,
                state="modified" if path == "base.txt" else "new",
                size_bytes=len(data),
            ),
        ),
        driver="fake",
    )
    dispatch = dispatch.model_copy(update={"checkpoint_id": manifest.checkpoint_id})
    server, driver = WireServer(dispatch, brief), PolicyDriver()
    server.checkpoints = [manifest.model_dump(mode="json")]
    server.blobs[blob] = data
    result = await execute(dispatch, server, driver, workspace)
    assert result.classification == "write_policy_refused"
    assert server.model_keys == 0 and not driver.starts
    assert not any(
        method == "GET" and target.startswith("blobs/") for method, target in server.calls
    )
    assert (workspace / "base.txt").read_text() == "base\n" and not (
        workspace / ".mcp.json"
    ).exists()


async def test_metered_driver_without_gateway_credential_capability_refuses_before_key(tmp_path):
    workspace = tmp_path / "work"
    dispatch, brief = frozen_inputs(checkout(workspace))
    server = WireServer(dispatch, brief)
    driver = PolicyDriver()
    driver.requires_model_key = False
    result = await execute(dispatch, server, driver, workspace)
    assert result.classification == "unsupported_metering_policy"
    assert server.model_keys == 0 and not driver.starts


async def test_repair_lane_preserves_approval_and_uses_exact_execution_base(tmp_path):
    from forge_protocol import Brief, RepairContext
    from tests.test_lane_runner_v1 import git

    workspace = tmp_path / "work"
    original = checkout(workspace)
    (workspace / "base.txt").write_text("previous candidate\n")
    git(workspace, "commit", "-qam", "owned candidate")
    candidate_sha = git(workspace, "rev-parse", "HEAD").decode().strip()
    dispatch, brief = frozen_inputs(original, generation=2)
    repair = RepairContext(
        forge_run_id=dispatch.forge_run_id,
        attempt=2,
        generation=2,
        approved_digest=dispatch.approved_digest,
        original_base_sha=original,
        candidate_sha=candidate_sha,
        prior_attempt=1,
        prior_generation=1,
        failure_context="unit: expected corrected parser",
    )
    dispatch = dispatch.model_copy(update={"attempt": 2, "repair": repair})
    brief = Brief.model_validate({**brief.model_dump(), "attempt": 2, "repair": repair})
    server = WireServer(dispatch, brief)
    driver = PolicyDriver(edit=lambda path, env: (path / "fixed.py").write_text("fixed\n"))
    result = await execute(dispatch, server, driver, workspace)
    assert result.exit_code == ExitCode.COMPLETED
    served = driver.starts[0][0]
    assert served.base_sha == original and served.spec_json == brief.spec_json
    assert served.execution_base_sha == candidate_sha
    assert repair.failure_context in served.execution_brief_text
    assert served.brief_text == brief.brief_text
    assert server.candidates[0].base_sha == candidate_sha
    assert b"previous candidate" not in server.blobs[server.candidates[0].diff_sha256]


async def test_repair_context_mismatch_refuses_before_grant_or_driver(tmp_path):
    from forge_protocol import Brief, RepairContext

    workspace = tmp_path / "work"
    base = checkout(workspace)
    dispatch, brief = frozen_inputs(base, generation=2)
    dispatch = dispatch.model_copy(update={"attempt": 2})
    repair = RepairContext(
        forge_run_id=dispatch.forge_run_id,
        attempt=2,
        generation=2,
        approved_digest=dispatch.approved_digest,
        original_base_sha=base,
        candidate_sha=base,
        prior_attempt=1,
        prior_generation=1,
        failure_context="unit: failure",
    )
    brief = Brief.model_validate({**brief.model_dump(), "attempt": 2, "repair": repair})
    server, driver = WireServer(dispatch, brief), PolicyDriver()
    result = await execute(dispatch, server, driver, workspace)
    assert result.exit_code == ExitCode.APPROVAL
    assert not driver.starts and server.model_keys == 0
