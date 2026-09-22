"""NXT-04 (issue #127): verify REAL provider and harness constructions.

The review's finding: the suites pass against fakes that mirror a DESIRED
contract, and nothing proves the REAL constructors and signatures still
match the seams those fakes stand in for. A fake that drifts silently
with the real class re-licenses the drift; these tests pin the real
classes themselves to the seams their consumers rely on — no mocks of
the real classes anywhere (that would defeat the point). Real objects
are constructed with minimal side-effect-free kwargs where possible,
otherwise compared as unbound methods via ``inspect`` /
``typing.get_type_hints``. No test in this module performs network I/O.

Surfaces covered:

- the three REAL driver clients in ``forge.adaptive.drivers`` vs the
  adapter Protocols in ``forge.adaptive.adapters`` (method-by-method
  signature comparison: parameter names/kinds/required-ness, coroutine
  parity, return void-ness), their constructors' minimal-kwargs shapes,
  and the ``*_from_env`` factories the lane runner actually calls;
- ``Mailbox`` (in-memory) and ``PostgresMailbox`` (durable) vs the
  ``MailboxSurface`` protocol: the shared method surface parameter for
  parameter, the documented async refinement of the Postgres side, and
  the NXT-12 ladder that refines the coarse in-memory ``apply``;
- ``AsyncMailboxAdapter`` and ``PostgresMailbox`` vs the UNIFIED
  ``AsyncMailboxSurface`` protocol (mailbox_bridge): the same awaitable
  signatures parameter for parameter on BOTH implementations — the seam
  ``OperatorControlService`` awaits, whatever store backs it;
- ``OperatorControlService`` vs the exact call shapes
  ``command_router.ControlCommandRouter.handle`` and
  ``lane_control.LaneSteeringSession`` issue (bind-level checks plus
  real invocations — attribute and return-shape contracts);
- ``harness_entry.DRIVERS`` render arms vs the actual lane runner
  (``forge.lane_driver``) and the shipped-driver vocabulary;
- ``discovery_stage``'s reader duck-type vs the REAL GitHub / GitLab /
  Azure reader classes: constructible surfaces, method presence and
  signature arity (``get_tree`` + ``read_text`` / the GitLab ``get_file``
  fallback leg).
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable
from typing import Any, get_type_hints

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.adaptive.adapters import (
    ClaudeSDKAdapter,
    ClaudeSDKClient,
    CodexAppAdapter,
    CodexAppClient,
    OpenCodeAdapter,
    OpenCodeClient,
)
from forge.adaptive.control import Mailbox, MailboxSurface
from forge.adaptive.drivers import (
    ClaudeSDKDriverClient,
    CodexAppDriverClient,
    OpenCodeDriverClient,
    claude_sdk_client_from_env,
    codex_app_client_from_env,
    opencode_client_from_env,
)
from forge.adaptive.mailbox_bridge import (
    ASYNC_SURFACE_MEMBERS,
    AsyncMailboxAdapter,
    AsyncMailboxSurface,
)
from forge.adaptive.mailbox_db import PostgresMailbox
from forge.adaptive.wiring import OperatorControlService
from forge.gitlab.client import GitLabClient
from forge.harness_entry import (
    DRIVERS,
    LANE_DRIVERS,
    SCRIPTED_DRIVERS,
    TASK_PROMPT,
    render_driver_script,
)
from forge.integrations.azure import AzureDevOpsClient, AzureRepositoryReader
from forge.integrations.github import (
    GitHubClient,
    GitHubRepositoryReader,
    GitHubStaticCredentials,
)
from forge.lane_driver import LANE_DRIVER_IDS
from forge.models.base import Base  # noqa: F401 — registry import side effect
from forge.runs.harness_selection import SHIPPED_DRIVERS


# ---------------------------------------------------------------------------
# Comparison helpers — the drift detectors
# ---------------------------------------------------------------------------


def _param_shape(method: Callable[..., Any]) -> list[tuple[str, str, bool]]:
    """(name, kind, has-default) per non-self parameter of *method*.

    The stable comparison key: renames, reordering between positional and
    keyword, and a default growing or disappearing all change it.
    """
    parameters = list(inspect.signature(method).parameters.items())
    if parameters and parameters[0][0] == "self":
        parameters = parameters[1:]
    return [
        (name, parameter.kind.name, parameter.default is not inspect.Parameter.empty)
        for name, parameter in parameters
    ]


def _assert_call_surface(
    expected: Callable[..., Any], real: Callable[..., Any], *, what: str
) -> None:
    """*real* accepts exactly the arguments *expected* (the seam) declares."""
    assert _param_shape(expected) == _param_shape(real), (
        f"{what}: real signature {inspect.signature(real)} drifted from the seam "
        f"{inspect.signature(expected)}"
    )
    assert inspect.iscoroutinefunction(real) is inspect.iscoroutinefunction(expected), (
        f"{what}: coroutine parity with the seam changed (real={inspect.iscoroutinefunction(real)})"
    )
    expected_return = get_type_hints(expected).get("return")
    real_return = get_type_hints(real).get("return")
    assert (expected_return is type(None)) == (real_return is type(None)), (
        f"{what}: the seam expects a {'void' if expected_return is type(None) else 'value'} "
        f"return, the real method now returns {real_return!r}"
    )


def _assert_param_surface(
    expected: Callable[..., Any], real: Callable[..., Any], *, what: str
) -> None:
    """The parameter-shape half of :func:`_assert_call_surface`.

    For surfaces whose coroutine-ness is a DOCUMENTED deliberate
    difference (the durable mailbox's async refinement): names, kinds and
    required-ness still must not drift.
    """
    assert _param_shape(expected) == _param_shape(real), (
        f"{what}: real signature {inspect.signature(real)} drifted from the seam "
        f"{inspect.signature(expected)}"
    )


def _binds(method: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Assert *method*'s signature accepts exactly this consumer call shape."""
    inspect.signature(method).bind(None, *args, **kwargs)  # None = the self slot


# ---------------------------------------------------------------------------
# The REAL driver clients vs the adapter Protocols
# ---------------------------------------------------------------------------


async def _refuse_connect() -> Any:
    raise AssertionError("construction is lazy — connect must not run at import/build time")


@pytest.fixture
def claude_client() -> ClaudeSDKDriverClient:
    # ``sdk`` is the documented module stand-in seam (the vendor package is
    # the ``interactive`` extra); construction itself has no side effects.
    return ClaudeSDKDriverClient(sdk=types.ModuleType("claude_agent_sdk"))


@pytest.fixture
def codex_client() -> CodexAppDriverClient:
    return CodexAppDriverClient(connect=_refuse_connect)


@pytest.fixture
async def opencode_http() -> httpx.AsyncClient:
    http = httpx.AsyncClient(base_url="http://127.0.0.1:1")
    yield http
    await http.aclose()


@pytest.fixture
def opencode_client(opencode_http: httpx.AsyncClient) -> OpenCodeDriverClient:
    return OpenCodeDriverClient(opencode_http)


class TestDriverClientProtocolSurfaces:
    """Each real client still satisfies its adapter Protocol, method by method."""

    def test_claude_sdk_driver_client_matches_the_protocol(self, claude_client):
        for name in ("start_session", "send", "interrupt", "query"):
            _assert_call_surface(
                getattr(ClaudeSDKClient, name),
                getattr(type(claude_client), name),
                what=f"ClaudeSDKDriverClient.{name}",
            )

    def test_codex_app_driver_client_matches_the_protocol(self, codex_client):
        for name in ("start_thread", "send_turn", "steer_active_turn", "interrupt"):
            _assert_call_surface(
                getattr(CodexAppClient, name),
                getattr(type(codex_client), name),
                what=f"CodexAppDriverClient.{name}",
            )

    def test_opencode_driver_client_matches_the_protocol(self, opencode_client):
        for name in ("start_session", "prompt", "events", "abort"):
            _assert_call_surface(
                getattr(OpenCodeClient, name),
                getattr(type(opencode_client), name),
                what=f"OpenCodeDriverClient.{name}",
            )

    def test_the_adapters_accept_the_real_clients(
        self, claude_client, codex_client, opencode_client
    ):
        # The adapters are the Protocol consumers; each constructs over the
        # REAL client object (not a fake) — the injection the lane performs.
        assert ClaudeSDKAdapter(client=claude_client).profile.sdk == "claude-sdk"
        assert CodexAppAdapter(client=codex_client).profile.sdk == "codex-app"
        assert OpenCodeAdapter(client=opencode_client).profile.sdk == "opencode-server"

    def test_claude_constructor_is_all_keyword_optional(self):
        parameters = list(inspect.signature(ClaudeSDKDriverClient.__init__).parameters.items())[1:]
        assert parameters, "the constructor must exist and be inspectable"
        for name, parameter in parameters:
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
                f"claude ctor param {name!r} is {parameter.kind.name} — the lane builds it "
                "keyword-only with defaults"
            )
            assert parameter.default is not inspect.Parameter.empty
        assert "sdk" in dict(parameters)  # the module stand-in seam stays injectable

    def test_codex_constructor_takes_one_lazy_connect_callable(self):
        parameters = dict(
            list(inspect.signature(CodexAppDriverClient.__init__).parameters.items())[1:]
        )
        required = [name for name, p in parameters.items() if p.default is inspect.Parameter.empty]
        assert required == ["connect"]  # the transport seam — lazy, never called at build

    def test_opencode_constructor_takes_the_http_client_keyword_only_beyond_it(self):
        parameters = list(inspect.signature(OpenCodeDriverClient.__init__).parameters.items())[1:]
        name, parameter = parameters[0]
        assert name == "http" and parameter.default is inspect.Parameter.empty
        for name, parameter in parameters[1:]:
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is not inspect.Parameter.empty


class TestDriverFromEnvFactories:
    """The lane runner's actual construction seams build real clients."""

    def test_claude_factory_signature_and_construction(self):
        (param,) = [p for n, p in inspect.signature(claude_sdk_client_from_env).parameters.items()]
        assert param.name == "sdk" and param.default is None
        client = claude_sdk_client_from_env(sdk=types.ModuleType("claude_agent_sdk"))
        assert isinstance(client, ClaudeSDKDriverClient)

    def test_codex_factory_signature_and_construction(self):
        parameters = inspect.signature(codex_app_client_from_env).parameters
        assert list(parameters) == ["env"] and parameters["env"].default is None
        assert isinstance(codex_app_client_from_env(), CodexAppDriverClient)

    async def test_opencode_factory_signature_and_construction(self):
        parameters = inspect.signature(opencode_client_from_env).parameters
        assert list(parameters) == ["provider_key", "env"]
        assert parameters["provider_key"].default is None
        assert parameters["env"].kind is inspect.Parameter.KEYWORD_ONLY
        client = opencode_client_from_env()
        try:
            assert isinstance(client, OpenCodeDriverClient)
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# MailboxSurface vs Mailbox vs PostgresMailbox
# ---------------------------------------------------------------------------

#: The surface the control plane codes against, as declared (CTL-04).
_SURFACE_MEMBERS = frozenset({"submit", "authorize", "apply", "checkpoint", "pending"})
#: The names both implementations keep verbatim (async on the durable side).
_SHARED_MEMBERS = ("submit", "authorize", "checkpoint", "pending")


class TestMailboxSurface:
    def test_the_protocol_declares_exactly_the_known_surface(self):
        # Catches unnoticed protocol growth: a new member nobody implements.
        assert set(getattr(MailboxSurface, "__protocol_attrs__")) == _SURFACE_MEMBERS

    def test_in_memory_mailbox_matches_the_protocol_parameter_for_parameter(self):
        mailbox = Mailbox()
        assert isinstance(mailbox, MailboxSurface)  # runtime-checkable presence check
        for name in sorted(_SURFACE_MEMBERS):
            _assert_call_surface(
                getattr(MailboxSurface, name), getattr(Mailbox, name), what=f"Mailbox.{name}"
            )
            # The reference implementation also carries the exact annotations.
            assert get_type_hints(getattr(MailboxSurface, name)) == get_type_hints(
                getattr(Mailbox, name)
            ), f"Mailbox.{name} annotation drift"

    def test_postgres_mailbox_keeps_the_shared_names_as_awaitables(self):
        mailbox = PostgresMailbox(async_sessionmaker(create_async_engine("sqlite+aiosqlite://")))
        for name in _SHARED_MEMBERS:
            real = getattr(type(mailbox), name)
            _assert_param_surface(
                getattr(MailboxSurface, name), real, what=f"PostgresMailbox.{name}"
            )
            # The documented refinement: production I/O is async — the seam's
            # one deliberate difference, so it is pinned, not ignored.
            assert inspect.iscoroutinefunction(real), f"PostgresMailbox.{name} must await"

    async def test_postgres_mailbox_refines_apply_into_the_nxt12_ladder(self):
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            mailbox = PostgresMailbox(async_sessionmaker(engine, expire_on_commit=False))
            assert not hasattr(mailbox, "apply")  # the coarse rung is gone by design
            # dispatch carries the SAME CAS kwargs the in-memory apply takes.
            dispatch_kwonly = {
                name: parameter
                for name, parameter in inspect.signature(
                    PostgresMailbox.dispatch
                ).parameters.items()
                if parameter.kind is inspect.Parameter.KEYWORD_ONLY
            }
            apply_kwonly = {
                name
                for name, parameter in inspect.signature(Mailbox.apply).parameters.items()
                if parameter.kind is inspect.Parameter.KEYWORD_ONLY
            }
            # The CAS gate is the SAME gate; the durable side may only ADD
            # its own defaulted extras (the vendor correlation id).
            assert apply_kwonly <= set(dispatch_kwonly)
            extras = set(dispatch_kwonly) - apply_kwonly
            assert all(
                dispatch_kwonly[name].default is not inspect.Parameter.empty for name in extras
            )
            assert apply_kwonly == {"current_plan_revision", "current_execution_epoch"}
            for rung in ("vendor_accepted", "outcome_unknown", "observe", "checkpoint"):
                _binds(getattr(PostgresMailbox, rung), "cmd-1")
            _binds(PostgresMailbox.get, "cmd-1")
        finally:
            await engine.dispose()


class TestAsyncMailboxSurface:
    """The UNIFIED async protocol the control service awaits: BOTH
    implementations satisfy the SAME awaitable signatures, parameter for
    parameter — the old "documented deliberate difference" (the durable
    side async, the reference side sync) is gone; the service's seam no
    longer knows which store backs it."""

    def test_the_protocol_declares_exactly_the_known_surface(self):
        # Catches unnoticed protocol growth: a new member nobody implements.
        assert set(getattr(AsyncMailboxSurface, "__protocol_attrs__")) == ASYNC_SURFACE_MEMBERS
        assert "apply" not in ASYNC_SURFACE_MEMBERS  # the memory-only coarse rung

    def test_the_memory_adapter_matches_the_protocol_parameter_for_parameter(self):
        adapter = AsyncMailboxAdapter()
        assert isinstance(adapter, AsyncMailboxSurface)  # runtime-checkable presence check
        for name in sorted(ASYNC_SURFACE_MEMBERS):
            _assert_call_surface(
                getattr(AsyncMailboxSurface, name),
                getattr(type(adapter), name),
                what=f"AsyncMailboxAdapter.{name}",
            )
        # the memory-only coarse rung the durable side refines away:
        _binds(
            AsyncMailboxAdapter.apply,
            "cmd-1",
            current_plan_revision=1,
            current_execution_epoch=1,
        )
        assert not hasattr(PostgresMailbox, "apply")

    async def test_the_durable_mailbox_matches_the_protocol_parameter_for_parameter(self):
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            mailbox = PostgresMailbox(async_sessionmaker(engine, expire_on_commit=False))
            assert isinstance(mailbox, AsyncMailboxSurface)
            for name in sorted(ASYNC_SURFACE_MEMBERS):
                real = getattr(type(mailbox), name)
                _assert_call_surface(
                    getattr(AsyncMailboxSurface, name),
                    real,
                    what=f"PostgresMailbox.{name}",
                )
                assert inspect.iscoroutinefunction(real), f"PostgresMailbox.{name} must await"
        finally:
            await engine.dispose()

    def test_the_dispatch_picks_the_surface_once_at_construction(self):
        from forge.adaptive.mailbox_bridge import control_surface_for

        memory = control_surface_for(Mailbox())
        assert isinstance(memory, AsyncMailboxAdapter)  # the sync reference is wrapped
        durable = control_surface_for(
            PostgresMailbox(async_sessionmaker(create_async_engine("sqlite+aiosqlite://")))
        )
        assert isinstance(durable, PostgresMailbox)  # an async mailbox IS the surface


# ---------------------------------------------------------------------------
# OperatorControlService vs its real consumers (command_router / lane_control)
# ---------------------------------------------------------------------------


class TestOperatorControlServiceSurface:
    """The service still answers the calls its consumers actually issue.

    The shapes pinned here are the literal call sites: ``command_router.py``
    ``ControlCommandRouter.handle`` (pause/resume/steer/answer — AWAITED
    since the service's mailbox seam went async) and ``lane_control.py``
    ``LaneSteeringSession.drain_once``
    (``service.mailbox.pending(work_id)`` — the RAW sync reference view,
    unchanged).
    """

    def test_constructs_with_no_arguments(self):
        # Both consumers build it bare: shared_control_service() and
        # lane_driver.steering_service_from_env() call OperatorControlService().
        service = OperatorControlService()
        assert isinstance(service.mailbox, Mailbox)

    def test_the_router_facing_methods_are_awaitables(self):
        # ControlCommandRouter.handle awaits each of these — a sync method
        # here would hand the router an unawaited coroutine.
        for name in ("pause", "resume", "steer", "answer", "pending", "submit"):
            assert inspect.iscoroutinefunction(getattr(OperatorControlService, name)), (
                f"OperatorControlService.{name} must be a coroutine function"
            )

    async def test_pause_accepts_the_router_call_shape_and_returns_pause_status(self):
        service = OperatorControlService()
        _binds(OperatorControlService.pause, "run-1", "pavel", "adaptive:pause:run-1:n1")
        state = await service.pause("run-1", "pavel", "adaptive:pause:run-1:n1")
        assert hasattr(state, "pause_status")  # the router renders it verbatim
        # a redelivered pause fences the epoch exactly once (NXT-09)
        await service.pause("run-1", "pavel", "adaptive:pause:run-1:n1")
        assert service.pause_states["run-1"].publication_epoch == 1

    async def test_resume_accepts_the_router_call_shape_and_answers_a_bool(self):
        _binds(OperatorControlService.resume, "run-1", "pavel", "adaptive:resume:run-1:n1")
        service = OperatorControlService()
        assert await service.resume("run-1", "pavel", "adaptive:resume:run-1:n1") is False

    async def test_steer_accepts_the_router_call_shape_and_answers_status_and_classification(self):
        _binds(
            OperatorControlService.steer,
            "run-1",
            "pavel",
            "use the existing helper",
            run_id="run-1",
        )
        service = OperatorControlService()
        outcome = await service.steer("run-1", "pavel", "use the existing helper", run_id="run-1")
        assert outcome["status"] == "accepted"
        assert outcome["classification"] == "steer"
        rejected = await service.steer("run-1", "pavel", "skip the tests", run_id="run-1")
        assert rejected["status"] == "rejected"  # the router's refusal branch

    async def test_answer_accepts_the_router_call_shape_and_answers_created(self):
        _binds(
            OperatorControlService.answer,
            "run-1",
            "pavel",
            "q-1",
            "postgres:16",
            run_id="run-1",
        )
        service = OperatorControlService()
        assert await service.answer("run-1", "pavel", "q-1", "postgres:16", run_id="run-1") is True
        assert await service.answer("run-1", "pavel", "q-1", "postgres:16", run_id="run-1") is False

    async def test_the_lane_bridge_attribute_path_still_works(self):
        # lane_control.LaneSteeringSession.drain_once issues
        # self.service.mailbox.pending(self.work_id) — attribute + 1-arg call,
        # SYNC: the raw in-memory reference view the mailbox field keeps.
        service = OperatorControlService()
        await service.steer("run-1", "pavel", "fix the assertion first")
        _binds(type(service.mailbox).pending, "run-1")
        assert not inspect.iscoroutinefunction(type(service.mailbox).pending)
        pending = service.mailbox.pending("run-1")  # no loop, no await
        assert [command.kind for command in pending] == ["steer"]


# ---------------------------------------------------------------------------
# harness_entry.DRIVERS render arms vs the actual lane drivers
# ---------------------------------------------------------------------------


class TestHarnessDriverRenderArms:
    def test_every_driver_in_drivers_renders_a_script(self):
        assert DRIVERS == SCRIPTED_DRIVERS + LANE_DRIVERS  # the closed vocabulary
        for driver in DRIVERS:
            script = render_driver_script(driver, "some-model", ".forge/brief.md")
            assert isinstance(script, str) and script.strip(), f"{driver} rendered nothing"

    def test_an_unknown_driver_is_refused(self):
        with pytest.raises(ValueError, match="unknown driver"):
            render_driver_script("vibe-drv", "m", "b.md")

    def test_scripted_arms_pin_echo_and_tee(self):
        echoes = {
            "claude-code": "claude --version",
            "grok-build": "grok --version",
            "opencode": "opencode --version",
            "copilot": "copilot --version",
        }
        for driver, echo in echoes.items():
            script = render_driver_script(driver, "m", ".forge/brief.md")
            assert echo in script, f"{driver} lost its R15 version echo"
            assert "| tee -a" in script, f"{driver} lost the event tee"

    def test_lane_arms_hand_over_to_the_real_lane_runner(self):
        codex = render_driver_script("codex-sdk-lane", "gpt-5", ".forge/brief.md")
        assert "python -m forge.lane_driver --driver codex" in codex
        opencode = render_driver_script("opencode-sdk-lane", "", ".forge/brief.md")
        assert "python -m forge.lane_driver --driver opencode" in opencode
        for script in (codex, opencode):
            assert "| tee -a" not in script  # the lane runner writes receipts itself
            # no scripted prompt invocation on a lane: the shared short
            # pointer prompt the scripted arms embed is absent
            assert TASK_PROMPT not in script

    def test_the_lane_runner_accepts_the_keys_the_arms_render(self):
        # The rendered --driver values must be dispatchable CLI choices.
        assert set(LANE_DRIVER_IDS) == {"claude", "codex", "opencode", "copilot"}
        # The registered harness ids the runner reports are real driver names.
        assert LANE_DRIVER_IDS["codex"] == "codex-sdk-lane"
        assert LANE_DRIVER_IDS["opencode"] == "opencode-sdk-lane"
        assert LANE_DRIVER_IDS["copilot"] == "copilot-sdk-lane"
        assert LANE_DRIVER_IDS["codex"] in DRIVERS and LANE_DRIVER_IDS["opencode"] in DRIVERS

    def test_drivers_and_shipped_drivers_agree(self):
        # The claude lane renders through its own arm since the live
        # claude-sdk-lane slice (harness_entry pins claude 2.1.273), so the
        # closed vocabularies now agree exactly.
        assert set(DRIVERS) <= SHIPPED_DRIVERS
        assert SHIPPED_DRIVERS - set(DRIVERS) == frozenset()
        assert LANE_DRIVERS == (
            "claude-sdk-lane",
            "codex-sdk-lane",
            "opencode-sdk-lane",
            "copilot-sdk-lane",
        )
        assert LANE_DRIVER_IDS["claude"] == "claude-sdk-lane"


# ---------------------------------------------------------------------------
# discovery_stage's reader duck-type vs the REAL provider readers
# ---------------------------------------------------------------------------


@pytest.fixture
def github_reader() -> GitHubRepositoryReader:
    # GitHubStaticCredentials is the repo's own static-token provider: a real
    # TokenProvider, not a mock. No request happens at construction.
    return GitHubRepositoryReader(
        GitHubClient(
            base_url="https://api.github.com", token_provider=GitHubStaticCredentials("t")
        ),
        "example",
        "repo",
    )


@pytest.fixture
def azure_reader() -> AzureRepositoryReader:
    return AzureRepositoryReader(
        AzureDevOpsClient(base_url="https://dev.azure.com/example", token="pat"),
        "project",
        "repo",
    )


@pytest.fixture
def gitlab_client() -> GitLabClient:
    return GitLabClient(base_url="https://gitlab.example", token="token")


class TestDiscoveryReaderDuckType:
    """The snapshot seam's calls fit the REAL GitHub/GitLab/Azure readers."""

    def test_all_three_construct_side_effect_free(self, github_reader, azure_reader, gitlab_client):
        assert isinstance(github_reader, GitHubRepositoryReader)
        assert isinstance(azure_reader, AzureRepositoryReader)
        assert isinstance(gitlab_client, GitLabClient)

    def test_get_tree_matches_the_seam_on_all_three(
        self, github_reader, azure_reader, gitlab_client
    ):
        # load_snapshot_files issues: reader.get_tree(project_id, "", ref, recursive=True)
        for reader in (github_reader, azure_reader, gitlab_client):
            method = getattr(type(reader), "get_tree")
            assert inspect.iscoroutinefunction(method), f"{type(reader).__name__}.get_tree"
            _binds(method, 1, "", "main", recursive=True)

    def test_read_text_matches_the_seam_on_github_and_azure(self, github_reader, azure_reader):
        for reader in (github_reader, azure_reader):
            method = getattr(type(reader), "read_text")
            assert inspect.iscoroutinefunction(method)
            _binds(method, "src/app.py", "main")

    def test_gitlab_satisfies_the_seam_through_the_get_file_fallback(self, gitlab_client):
        # GitLabClient carries no read_text (its file reads are base64
        # RepositoryFile records); the seam's documented fallback leg calls
        # get_file(project_id, file_path, ref) and decodes.
        assert not hasattr(GitLabClient, "read_text")
        method = getattr(type(gitlab_client), "get_file")
        assert inspect.iscoroutinefunction(method)
        _binds(method, 1, "src/app.py", "main")

    async def test_the_real_gitlab_client_loads_a_snapshot_over_the_fallback(self):
        # One level deeper than signatures: the REAL GitLabClient (its real
        # constructor over a fake HTTP transport — the issue #127 pattern,
        # never a mocked reader class) runs the snapshot seam end to end,
        # proving the fallback leg decodes what get_file really returns.
        import base64

        from forge.adaptive.discovery_stage import load_snapshot_files
        from forge.gitlab.schemas import RepositoryFile, TreeEntry

        content = "def start_run():\n    return 1\n"
        encoded = base64.b64encode(content.encode()).decode()
        requested: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            requested.append(path)
            if path.endswith("/repository/tree"):
                return httpx.Response(
                    200,
                    json=[
                        TreeEntry(
                            id="a" * 40,
                            name="app.py",
                            type="blob",
                            path="src/app.py",
                            mode="100644",
                        ).model_dump()
                    ],
                )
            if "/repository/files/" in path:
                return httpx.Response(
                    200,
                    json=RepositoryFile(
                        file_name="app.py",
                        file_path="src/app.py",
                        size=len(content),
                        encoding="base64",
                        content=encoded,
                        ref="main",
                    ).model_dump(),
                )
            raise AssertionError(f"unexpected provider call: {path}")  # pragma: no cover

        client = GitLabClient(base_url="https://gitlab.example", token="token")
        client._client = httpx.AsyncClient(
            base_url="https://gitlab.example/api/v4",
            headers={"PRIVATE-TOKEN": "token"},
            transport=httpx.MockTransport(_handler),
        )
        try:
            files = await load_snapshot_files(client, 7, "main")
            assert files == {"src/app.py": content}
            # one tree listing + one file read — the seam issues nothing else
            assert len(requested) == 2
        finally:
            await client.close()
