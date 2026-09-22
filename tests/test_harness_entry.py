"""harness_entry tests (E3b): the Actions lane's driver runner.

The rendered scripts are asserted against the contract the GitLab templates
establish (``ci/templates/*.gitlab-ci.yml``; interface ground truth:
``docs/research/2026-09-13-harness-interfaces.md``) — same flags, same unattended
posture, same hardened grok preamble. The subprocess is exercised end-to-end
with a fake driver script, no CLIs and no network.
"""

import hashlib
import io
import json
import re
import shlex
import urllib.error
from pathlib import Path

import pytest

from forge.harness_entry import (
    DEFAULT_DRIVER_VERSIONS,
    DRIVERS,
    LANE_DRIVERS,
    SCRIPTED_DRIVERS,
    _CLAUDE_TOOL_RULES,
    emit_candidate_meta,
    fetch_workitem,
    main,
    parse_usage,
    render_brief,
    render_driver_script,
    resolve_driver_versions,
)
from forge.harnesses.brief_envelope import build_brief_envelope, render_approved_sections

BRIEF = ".forge/brief.md"


# ----------------------------------------------------------------------
# Per-driver script rendering
# ----------------------------------------------------------------------


class TestRenderClaudeCode:
    def test_script_follows_the_unattended_contract(self):
        script = render_driver_script("claude-code", "glm-5.3-flash[1m]", BRIEF)

        assert "claude -p " in script  # headless print mode
        assert "--model 'glm-5.3-flash[1m]'" in script
        assert (
            "--permission-mode bypassPermissions" in script
        )  # the lane boundary is no-creds + publisher, not an allowlist
        assert "--setting-sources ''" in script  # no external settings load
        assert "--output-format stream-json" in script  # normalized event stream
        assert "--allowedTools" in script and "Bash(git status:*)" in script  # git-only shell
        assert BRIEF in script

    def test_empty_model_omits_the_model_flag(self):
        script = render_driver_script("claude-code", "", BRIEF)

        assert "--model" not in script

    def test_repair_dispatch_caps_thinking_budget(self):
        """Repair re-dispatches are guided fixes: the thinking-cap export is
        GUARDED at runtime by repair-context presence (the rendered script
        is static — first cycles take the guard's else path and think
        freely)."""
        script = render_driver_script("claude-code", "m", BRIEF)
        assert 'if [ -n "$FORGE_REPAIR_CONTEXT" ]; then' in script
        assert "MAX_THINKING_TOKENS=" in script


class TestRenderGrokBuild:
    def test_script_installs_the_platform_binary_explicitly(self):
        """The wrapper declares the platform binary as an optionalDependency:
        a flaky registry silently skips it and the CLI hangs forever — both
        packages are installed explicitly, with retries (verified live)."""
        script = render_driver_script("grok-build", "grok-4.6", BRIEF)

        assert "npm install -g --no-fund --no-audit @xai-official/grok" in script
        assert "@xai-official/grok-linux-x64@${GROK_VER}" in script
        assert "for attempt in 1 2 3" in script  # retries
        assert "test -d /usr/local/lib/node_modules/@xai-official/grok-linux-x64" in script

    def test_script_follows_the_unattended_contract(self):
        script = render_driver_script("grok-build", "grok-4.6", BRIEF)

        assert "grok --no-auto-update --always-approve --no-alt-screen" in script
        # --always-approve is REQUIRED: headless grok hangs without it.
        assert "--output-format streaming-json" in script
        assert "--debug-file .forge/grok-debug.log" in script
        assert "-p " in script


class TestRenderOpencode:
    def test_script_uses_auto_approval(self):
        script = render_driver_script("opencode", "zai/glm-5.3-flash", BRIEF)

        assert "opencode run --auto " in script  # unattended flag
        assert BRIEF in script
        # The model is config-owned on this driver (opencode.json), not a
        # CLI flag — mirror of the GitLab template.
        assert "--model" not in script


class TestRenderCommon:
    def test_every_driver_keeps_the_audit_trail_streaming(self):
        """The event log is tee'd so a failed driver still leaves its stream
        for the artifact step (the workflow uploads ``if: always()``) —
        for the SCRIPTED drivers; the SDK lanes write their own receipts
        via forge.lane_driver (no event log to tee)."""
        for driver in SCRIPTED_DRIVERS:
            script = render_driver_script(driver, "m", BRIEF)
            assert ".forge/events.jsonl" in script
            assert "tee -a" in script

    def test_unknown_driver_is_rejected(self):
        with pytest.raises(ValueError, match="unknown driver"):
            render_driver_script("codex", "", BRIEF)

    def test_the_prompt_is_the_short_shared_pointer(self):
        """The quality lives in the brief file; the -p prompt only points at
        it (forge.harnesses.prompt.TASK_PROMPT), identically for every
        SCRIPTED driver — the SDK lanes carry no prompt pointer at all
        (the lane runner inlines the brief as the ONE task)."""
        from forge.harnesses.prompt import TASK_PROMPT

        for driver in SCRIPTED_DRIVERS:
            script = render_driver_script(driver, "m", BRIEF)
            assert shlex.quote(TASK_PROMPT) in script
            assert "brief.md" in script  # the pointer names the brief


class TestRenderSdkLanes:
    """The EXE-02 SDK-lane drivers: the rendered script provisions the CLI
    and hands over to forge's own lane runner — the Actions/AzDO mirror of
    the codex/opencode GitLab sdk-lane templates."""

    def test_both_lanes_hand_over_to_the_lane_runner(self):
        for driver, key in (("codex-sdk-lane", "codex"), ("opencode-sdk-lane", "opencode")):
            script = render_driver_script(driver, "m", BRIEF)
            assert "python -m forge.lane_driver --driver " + key in script, driver
            # No scripted invocation, no prompt pointer, no event tee —
            # the lane runner's meta/usage artifacts ARE the audit trail.
            assert "TASK_PROMPT" not in script
            assert "tee -a" not in script
            assert "$FORGE_FILTER_PIPE" not in script

    def test_codex_lane_routes_the_model_and_the_sandbox_recipe(self):
        script = render_driver_script("codex-sdk-lane", "gpt-5.3", BRIEF)

        assert "export CODEX_MODEL=gpt-5.3" in script
        assert 'export CODEX_CWD="$PWD"' in script
        # The OPTIONAL provider-native credential (the ChatGPT-login blob),
        # guarded exactly like the grok lane's subscription auth.
        assert 'if [ -n "$FORGE_CODEX_AUTH" ]; then' in script
        assert 'printf "%s" "$FORGE_CODEX_AUTH" > ~/.codex/auth.json' in script
        assert "chmod 600 ~/.codex/auth.json" in script

    def test_codex_lane_omits_the_model_export_when_unrouted(self):
        assert "CODEX_MODEL" not in render_driver_script("codex-sdk-lane", "", BRIEF)

    def test_opencode_lane_carries_the_mechanical_deny_and_once_permission(self):
        script = render_driver_script("opencode-sdk-lane", "", BRIEF)

        assert '"git commit *": "deny"' in script
        assert '"git push *": "deny"' in script
        assert '"external_directory": "allow"' in script
        assert '"doom_loop": "allow"' in script
        # The config-injection env rides the ambient environment into the
        # lane-spawned serve child (the spawner merges os.environ).
        assert "export OPENCODE_CONFIG_CONTENT=" in script
        # LIVE-found: reject starves every tool call in a task lane.
        assert 'export OPENCODE_PERMISSION_RESPONSE="${OPENCODE_PERMISSION_RESPONSE:-once}"' in (
            script
        )
        assert 'export OPENCODE_SERVE_CWD="$PWD"' in script

    def test_opencode_lane_splits_a_provider_slash_model_route(self):
        script = render_driver_script("opencode-sdk-lane", "zai/glm-5.3-flash", BRIEF)

        assert "export OPENCODE_PROVIDER_ID=zai" in script
        assert "export OPENCODE_MODEL_ID=glm-5.3-flash" in script

    def test_opencode_lane_keeps_a_bare_model_as_the_model_id(self):
        script = render_driver_script("opencode-sdk-lane", "glm-5.3-flash", BRIEF)

        assert "export OPENCODE_MODEL_ID=glm-5.3-flash" in script
        assert "OPENCODE_PROVIDER_ID" not in script

    def test_mcp_servers_are_not_consumed_on_the_sdk_lanes(self):
        servers = {"context7": {"type": "http", "url": "https://mcp.example.com/mcp"}}
        for driver in LANE_DRIVERS:
            assert "context7" not in render_driver_script(driver, "m", BRIEF, mcp_servers=servers)

    def test_the_sdk_lane_ids_are_the_registered_lane_driver_ids(self):
        from forge.lane_driver import LANE_DRIVER_IDS

        assert LANE_DRIVERS == (
            LANE_DRIVER_IDS["claude"],
            LANE_DRIVER_IDS["codex"],
            LANE_DRIVER_IDS["opencode"],
        )

    def test_the_claude_lane_exports_the_lane_control_pair(self):
        """NXT-10: the claude arm forwards the dispatch's lane control pair
        into the lane runner's env (empty when unset — the lane then
        honestly stays on its local mailbox)."""
        script = render_driver_script("claude-sdk-lane", "m", BRIEF)

        assert 'export FORGE_LANE_CONTROL_URL="${FORGE_LANE_CONTROL_URL:-}"' in script
        assert 'export FORGE_LANE_CONTROL_TOKEN="${FORGE_LANE_CONTROL_TOKEN:-}"' in script


# ----------------------------------------------------------------------
# Versioned drivers (R15): pinned npm installs, "latest" opt-out, the
# resolved version echoed into the job log.
# ----------------------------------------------------------------------

#: driver id → (npm package, CLI binary) — the install/echo surface.
_PACKAGES = {
    "claude-code": ("@anthropic-ai/claude-code", "claude"),
    "grok-build": ("@xai-official/grok", "grok"),
    "opencode": ("opencode-ai", "opencode"),
    "copilot": ("@github/copilot", "copilot"),
    "claude-sdk-lane": ("@anthropic-ai/claude-code", "claude"),
    "codex-sdk-lane": ("@openai/codex", "codex"),
    "opencode-sdk-lane": ("opencode-ai", "opencode"),
}


class TestDriverVersionPins:
    def test_defaults_pin_every_install_to_the_known_good_version(self):
        """No moving npm dist-tag: every preamble installs
        ``package@<known-good>`` (a CLI release must never silently change
        lane behavior between the plan gate and the run)."""
        for driver in DRIVERS:
            package, _ = _PACKAGES[driver]
            script = render_driver_script(driver, "m", BRIEF)
            assert f"npm install -g --no-fund --no-audit {package}" in script, driver
            assert (
                f"npm install -g --no-fund --no-audit {package}@{DEFAULT_DRIVER_VERSIONS[driver]}"
            ) in script, driver

    def test_every_preamble_echoes_the_resolved_version(self):
        """The resolved CLI version lands in the job log — pin drift is
        visible, never silent (the ``<cli> --version`` tail each preamble
        already carried)."""
        for driver in DRIVERS:
            _, cli = _PACKAGES[driver]
            assert f"{cli} --version" in render_driver_script(driver, "m", BRIEF)

    def test_latest_keeps_the_unpinned_dist_tag_install(self):
        """The documented opt-out: ``latest`` installs the moving dist-tag
        (npm resolves it identically to the old unpinned install)."""
        pins = dict.fromkeys(DRIVERS, "latest")
        script = render_driver_script("claude-code", "m", BRIEF, driver_versions=pins)
        assert "@anthropic-ai/claude-code@latest && break" in script
        assert (
            render_driver_script("grok-build", "m", BRIEF, driver_versions=pins).count(
                "@xai-official/grok@latest"
            )
            == 1
        )  # the platform binary stays GROK_VER-driven, not "latest"

    def test_an_override_pins_just_one_driver(self):
        pins = resolve_driver_versions('{"claude-code": "2.0.0"}')
        script = render_driver_script("claude-code", "m", BRIEF, driver_versions=pins)
        assert "@anthropic-ai/claude-code@2.0.0" in script
        # Other drivers keep their defaults.
        other = render_driver_script("copilot", "m", BRIEF, driver_versions=pins)
        assert f"@github/copilot@{DEFAULT_DRIVER_VERSIONS['copilot']}" in other

    def test_grok_platform_binary_follows_the_pinned_wrapper(self):
        """The platform binary version is read back from the binary just
        installed — pinning the wrapper pins the optionalDependency too."""
        script = render_driver_script("grok-build", "m", BRIEF)
        assert "GROK_VER=\"$(grok --version | awk '{print $2}')\"" in script
        assert '"@xai-official/grok-linux-x64@${GROK_VER}"' in script

    def test_render_refuses_a_pin_with_shell_metacharacters(self):
        """Defense in depth: the pin is spliced into a shell command, so a
        hand-rolled mapping (bypassing resolve_driver_versions) with
        metacharacters is rejected, never rendered."""
        with pytest.raises(ValueError, match="bad driver version pin"):
            render_driver_script(
                "claude-code", "m", BRIEF, driver_versions={"claude-code": "1.0; rm -rf /"}
            )


class TestResolveDriverVersions:
    def test_absent_and_empty_fall_back_to_the_known_good_defaults(self):
        assert resolve_driver_versions(None) == DEFAULT_DRIVER_VERSIONS
        assert resolve_driver_versions("") == DEFAULT_DRIVER_VERSIONS
        assert resolve_driver_versions("   ") == DEFAULT_DRIVER_VERSIONS

    def test_the_json_override_wins_per_driver(self):
        pins = resolve_driver_versions('{"grok-build": "1.0.30", "copilot": "latest"}')

        assert pins["grok-build"] == "1.0.30"
        assert pins["copilot"] == "latest"
        assert pins["claude-code"] == DEFAULT_DRIVER_VERSIONS["claude-code"]

    def test_malformed_json_fails_closed(self):
        with pytest.raises(ValueError, match="FORGE_DRIVER_VERSIONS is not valid JSON"):
            resolve_driver_versions('{"claude-code": ')

    def test_a_non_object_fails_closed(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            resolve_driver_versions('["claude-code"]')

    def test_an_unknown_driver_fails_closed(self):
        with pytest.raises(ValueError, match="unknown driver 'codex'"):
            resolve_driver_versions('{"codex": "1.0"}')

    def test_a_version_with_metacharacters_fails_closed(self):
        with pytest.raises(ValueError, match="bad version"):
            resolve_driver_versions('{"claude-code": "1.0; x"}')

    def test_a_non_string_version_is_tolerated_as_its_string_form(self):
        """``json.loads`` happily yields numbers; the pin is the string
        form (2.1 → "2.1") — the charset check still applies to it."""
        pins = resolve_driver_versions('{"claude-code": 2.1}')

        assert pins == {**DEFAULT_DRIVER_VERSIONS, "claude-code": "2.1"}


# ----------------------------------------------------------------------
# The shared brief builder (single source for both lanes)
# ----------------------------------------------------------------------


class TestRenderBrief:
    def test_brief_carries_role_constraints_and_contract(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        brief = render_brief("Users cannot reset their password.", "1. Add the endpoint.\n")

        assert "## Role" in brief and "staff engineer" in brief
        assert "1. Add the endpoint." in brief  # plan verbatim
        assert "Users cannot reset their password." in brief
        assert "NEVER create or modify" in brief  # denied paths
        assert "working tree" in brief  # ci_lane output contract
        assert "NOT** commit" in brief

    def test_skills_line_names_conventions_files_present_in_the_workspace(
        self, tmp_path: Path, monkeypatch
    ):
        """Fixture-level skills check: a workspace containing AGENTS.md → the
        brief instructs the agent to read it (Claude reads CLAUDE.md the
        same way)."""
        monkeypatch.chdir(tmp_path)

        bare = render_brief("body", "plan")
        assert "if `AGENTS.md` or `CLAUDE.md` exists" in bare  # conditional form

        (tmp_path / "AGENTS.md").write_text("# conventions\n")
        with_agents = render_brief("body", "plan")
        assert "`AGENTS.md`" in with_agents
        assert "read" in with_agents and "follow" in with_agents

    def test_codegraph_section_follows_the_mcp_variable(self, tmp_path: Path, monkeypatch):
        """Single source of truth: the same ``FORGE_HARNESS_MCP`` variable
        that provisions the server in the driver turns on the brief's
        codegraph direction. Broken JSON degrades to "off", never raises."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FORGE_HARNESS_MCP", raising=False)

        off = render_brief("body", "plan")
        assert "codegraph" not in off

        monkeypatch.setenv(
            "FORGE_HARNESS_MCP",
            '{"codegraph": {"type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"]}}',
        )
        on = render_brief("body", "plan")
        assert "## Code navigation — codegraph MCP" in on

        monkeypatch.setenv("FORGE_HARNESS_MCP", "not-json{")
        broken = render_brief("body", "plan")
        assert "codegraph" not in broken

    def test_render_brief_mode_fetches_issue_and_plan_and_writes_the_brief(
        self, tmp_path: Path, monkeypatch
    ):
        """--render-brief: the lane fetches its own brief content (issue body
        + the forge plan comment) with the read-only runner token — no
        dispatch-input size limit ever binds."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/acme-widget")
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_runner")  # noqa: S105 — fake
        monkeypatch.setenv("FORGE_ISSUE_NUMBER", "42")

        async def _noop_async():
            return None

        def fake_fetch(
            repo: str,
            issue_number: int,
            token: str,
            *,
            plan_note_id: int = 0,
            run_id: str = "",
            envelope_digest: str = "",
            spec_digest: str = "",
        ):
            assert (repo, issue_number, token) == ("acme/acme-widget", 42, "ghs_runner")
            # No dispatch inputs here: the legacy scan runs, unbound (R05),
            # and no envelope digest is dispatched (A03 legacy posture).
            assert plan_note_id == 0 and run_id == ""
            assert envelope_digest == "" and spec_digest == ""
            return ("Users cannot reset their password.", "## Forge plan — run `abcd`\n...")

        monkeypatch.setattr("forge.harness_entry.fetch_issue_context", fake_fetch)

        rc = main(["--render-brief"])

        assert rc == 0
        brief = (tmp_path / ".forge" / "brief.md").read_text()
        assert "## Approved plan" in brief
        assert "## Forge plan — run `abcd`" in brief  # the plan, verbatim
        assert "Users cannot reset their password." in brief

    def test_render_brief_mode_requires_repo_issue_and_token(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("FORGE_ISSUE_NUMBER", raising=False)

        rc = main(["--render-brief", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (tmp_path / ".forge" / "exit").read_text().strip() == "failed"


# ----------------------------------------------------------------------
# The bound plan transport (R05) + the approved BriefEnvelope (A03):
# FORGE_PLAN_NOTE_ID addresses the EXACT approved plan comment; the
# dispatched envelope + spec digests pin the APPROVED BYTES — no scan, no
# identity heuristic, and no post-approval body edit can pass. Fail-closed.
# ----------------------------------------------------------------------


GH_REPO = "acme/acme-widget"
GH_ISSUE = 42
GH_NOTE_ID = 1234
GH_RUN_ID = "d" * 32
GH_SPEC_DIGEST = "c" * 64
GH_TASK_TITLE = "Users cannot reset their password"
GH_TASK_DESCRIPTION = "The reset email never arrives."
GH_PLAN_BODY = "1. Add the endpoint.\n2. Add tests.\n"


def gh_envelope_digest(run_id: str = GH_RUN_ID) -> str:
    """The envelope digest the control plane would dispatch for the
    approved task/plan bytes below."""
    return build_brief_envelope(
        run_id=run_id,
        task_title=GH_TASK_TITLE,
        task_description=GH_TASK_DESCRIPTION,
        plan_text=GH_PLAN_BODY,
        spec_digest=GH_SPEC_DIGEST,
    )["envelope_digest"]


def gh_plan_body(
    *,
    run_id: str = GH_RUN_ID,
    plan: str = GH_PLAN_BODY,
    title: str = GH_TASK_TITLE,
    description: str = GH_TASK_DESCRIPTION,
    with_sections: bool = True,
) -> str:
    """A forge plan comment body: header, the A03 approved-bytes sections,
    and the /go footer (whose full run id the R05 cross-check requires)."""
    sections = (
        render_approved_sections(task_title=title, task_description=description, plan_text=plan)
        if with_sections
        else plan
    )
    return (
        f"## Forge plan — run `{run_id[:8]}`\n\n"
        f"{sections}\n\n"
        "---\n\n"
        f"**Plan digest:** `{'a' * 64}`\n\n"
        f"Approve this exact plan by commenting `/go {run_id}`.\n\n"
        "*This is an automated message.*"
    )


def install_ghFake(monkeypatch, responses: dict[str, object], requests: list) -> None:
    """Serve *responses* (url-prefix → payload | Exception) to harness_entry's
    urllib calls, recording every request (stdlib-mock style: the lane is
    deliberately urllib-only, so pytest-httpx's httpx transport cannot
    intercept it — the EXACT-URL assertions below are the same contract)."""

    def fake_urlopen(request, timeout=30):
        url = request.full_url
        requests.append(request)
        # Longest prefix first: the issue URL is a prefix of its comments
        # URL, so specificity decides who serves a request.
        for prefix, payload in sorted(responses.items(), key=lambda item: -len(item[0])):
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected URL fetched: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def gh_issue_payload(body: str = GH_TASK_DESCRIPTION) -> dict:
    return {"number": GH_ISSUE, "body": body}


def gh_plan_comment(
    *,
    login: str = "forcewake-forge[bot]",
    body: str | None = None,
    note_id: int = GH_NOTE_ID,
) -> dict:
    return {
        "id": note_id,
        "body": body if body is not None else gh_plan_body(),
        "user": {"login": login},
    }


class TestBoundPlanBrief:
    """--render-brief with the dispatched envelope inputs: the lane fetches
    exactly GET /repos/{owner}/{repo}/issues/comments/{id}, validates the
    R05 tamper guards (author + header + run id), extracts the APPROVED
    sections and re-verifies the envelope digest over exactly those bytes —
    author/header/run-id alone no longer pass — and fails closed (rc 1,
    exit file ``failed``) on any violation."""

    def _env(self, monkeypatch, tmp_path: Path, *, envelope_digest: str | None = None) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_REPOSITORY", GH_REPO)
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_runner")  # noqa: S105 — fake
        monkeypatch.setenv("FORGE_ISSUE_NUMBER", str(GH_ISSUE))
        monkeypatch.setenv("FORGE_PLAN_NOTE_ID", str(GH_NOTE_ID))
        monkeypatch.setenv("FORGE_RUN_ID", GH_RUN_ID)
        monkeypatch.setenv("FORGE_SPEC_DIGEST", GH_SPEC_DIGEST)
        monkeypatch.setenv(
            "FORGE_ENVELOPE_DIGEST",
            gh_envelope_digest() if envelope_digest is None else envelope_digest,
        )

    def _responses(
        self, comment: dict, *, issue_body: str = GH_TASK_DESCRIPTION
    ) -> dict[str, object]:
        return {
            f"https://api.github.com/repos/{GH_REPO}/issues/{GH_ISSUE}": gh_issue_payload(
                issue_body
            ),
            f"https://api.github.com/repos/{GH_REPO}/issues/comments/{GH_NOTE_ID}": comment,
        }

    def test_binds_to_the_exact_comment_without_scanning(self, tmp_path: Path, monkeypatch):
        self._env(monkeypatch, tmp_path)
        requests: list = []
        issue_url = f"https://api.github.com/repos/{GH_REPO}/issues/{GH_ISSUE}"
        scan_url = f"https://api.github.com/repos/{GH_REPO}/issues/{GH_ISSUE}/comments"
        install_ghFake(
            monkeypatch,
            {
                **self._responses(gh_plan_comment()),
                # A scan would hit this and find NOTHING plan-shaped —
                # the bound path must never even request it.
                scan_url: [],
            },
            requests,
        )

        rc = main(["--render-brief"])

        assert rc == 0
        fetched = [request.full_url for request in requests]
        assert f"https://api.github.com/repos/{GH_REPO}/issues/comments/{GH_NOTE_ID}" in fetched
        assert not any(url.startswith(scan_url) for url in fetched)  # no heuristic scan
        # A03: the LIVE issue is never read for the task text.
        assert not any(url.startswith(issue_url) for url in fetched)
        brief = (tmp_path / ".forge" / "brief.md").read_text()
        assert "1. Add the endpoint." in brief  # the approved plan, verbatim
        assert "2. Add tests." in brief
        assert GH_TASK_TITLE in brief  # the FROZEN task bytes
        assert GH_TASK_DESCRIPTION in brief

    def test_missing_comment_fails_closed(self, tmp_path: Path, monkeypatch):
        self._env(monkeypatch, tmp_path)
        install_ghFake(
            monkeypatch,
            {
                **self._responses(
                    urllib.error.HTTPError("not-found", 404, "Not Found", None, io.BytesIO(b""))
                ),
            },
            [],
        )

        rc = main(["--render-brief", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (tmp_path / ".forge" / "exit").read_text().strip() == "failed"

    def test_comment_without_the_plan_header_fails_closed(self, tmp_path: Path, monkeypatch):
        self._env(monkeypatch, tmp_path)
        install_ghFake(
            monkeypatch,
            self._responses(gh_plan_comment(body="just chatter mentioning plans")),
            [],
        )

        rc = main(["--render-brief", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (tmp_path / ".forge" / "exit").read_text().strip() == "failed"

    def test_comment_from_another_author_fails_closed(self, tmp_path: Path, monkeypatch):
        """The tamper guard: with FORGE_GITHUB_BOT_LOGIN configured, a
        comment posted by anyone else is refused even at the exact id."""
        self._env(monkeypatch, tmp_path)
        monkeypatch.setenv("FORGE_GITHUB_BOT_LOGIN", "acme-forge")
        install_ghFake(
            monkeypatch,
            self._responses(gh_plan_comment(login="mallory")),
            [],
        )

        rc = main(["--render-brief", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (tmp_path / ".forge" / "exit").read_text().strip() == "failed"

    def test_another_runs_plan_fails_closed(self, tmp_path: Path, monkeypatch):
        """The run-id cross-check: a plan comment for a DIFFERENT run can
        never ride this lane's brief (the review's core finding)."""
        self._env(monkeypatch, tmp_path)
        other_run = "e" * 32
        install_ghFake(
            monkeypatch,
            self._responses(gh_plan_comment(body=gh_plan_body(run_id=other_run))),
            [],
        )

        rc = main(["--render-brief", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (tmp_path / ".forge" / "exit").read_text().strip() == "failed"

    def test_edited_comment_body_fails_closed_after_approval(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """A03 — the core finding: the SAME comment id with the SAME
        author/header/run id, but the plan bytes edited after /go, is
        refused on the envelope digest mismatch."""
        self._env(monkeypatch, tmp_path)
        edited = gh_plan_body(plan=GH_PLAN_BODY + "3. And delete the tests.\n")
        install_ghFake(monkeypatch, self._responses(gh_plan_comment(body=edited)), [])

        rc = main(["--render-brief", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (tmp_path / ".forge" / "exit").read_text().strip() == "failed"
        captured = capsys.readouterr()
        assert "approved brief bytes changed after approval (re-approval required)" in captured.err

    def test_edited_task_section_fails_closed_after_approval(self, tmp_path: Path, monkeypatch):
        """Same refusal when the TASK bytes were edited, not the plan."""
        self._env(monkeypatch, tmp_path)
        edited = gh_plan_body(description="Totally different task now.")
        install_ghFake(monkeypatch, self._responses(gh_plan_comment(body=edited)), [])

        rc = main(["--render-brief", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (tmp_path / ".forge" / "exit").read_text().strip() == "failed"

    def test_tampered_envelope_digest_fails_closed(self, tmp_path: Path, monkeypatch, capsys):
        """A dispatched digest that matches nothing is a fail-closed refusal
        even against a perfectly intact comment (defense in depth)."""
        self._env(monkeypatch, tmp_path, envelope_digest="0" * 64)
        install_ghFake(monkeypatch, self._responses(gh_plan_comment()), [])

        rc = main(["--render-brief", "--exit-file", ".forge/exit"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "approved brief bytes changed after approval (re-approval required)" in captured.err

    def test_comment_without_envelope_sections_fails_closed(self, tmp_path: Path, monkeypatch):
        """The R05 posture alone (author/header/run id, no approved-bytes
        sections) no longer passes an enforced envelope: a pre-A03 comment
        can never satisfy the content binding."""
        self._env(monkeypatch, tmp_path)
        legacy = gh_plan_body(with_sections=False)
        install_ghFake(monkeypatch, self._responses(gh_plan_comment(body=legacy)), [])

        rc = main(["--render-brief", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (tmp_path / ".forge" / "exit").read_text().strip() == "failed"

    def test_issue_edited_after_approval_keeps_the_frozen_task_bytes(
        self, tmp_path: Path, monkeypatch
    ):
        """Acceptance: an issue edit after approval cannot change the brief
        of the current attempt — the enforced path never even fetches the
        live issue; the task text is the envelope-verified frozen bytes."""
        self._env(monkeypatch, tmp_path)
        requests: list = []
        edited_issue = "MALICIOUS POST-APPROVAL EDIT: wipe the database"
        install_ghFake(
            monkeypatch,
            self._responses(gh_plan_comment(), issue_body=edited_issue),
            requests,
        )

        rc = main(["--render-brief"])

        assert rc == 0
        issue_url = f"https://api.github.com/repos/{GH_REPO}/issues/{GH_ISSUE}"
        assert not any(request.full_url.startswith(issue_url) for request in requests)
        brief = (tmp_path / ".forge" / "brief.md").read_text()
        assert GH_TASK_TITLE in brief and GH_TASK_DESCRIPTION in brief
        assert "MALICIOUS POST-APPROVAL EDIT" not in brief

    def test_correct_bytes_render_the_brief(self, tmp_path: Path, monkeypatch):
        """The happy path: intact comment + matching dispatched digests →
        the brief renders from the approved bytes."""
        self._env(monkeypatch, tmp_path)
        install_ghFake(monkeypatch, self._responses(gh_plan_comment()), [])

        rc = main(["--render-brief"])

        assert rc == 0
        brief = (tmp_path / ".forge" / "brief.md").read_text()
        assert "## Approved plan" in brief
        assert "1. Add the endpoint." in brief
        assert GH_TASK_TITLE in brief

    def test_configured_bot_login_with_app_suffix_is_accepted(self, tmp_path: Path, monkeypatch):
        """FORGE_GITHUB_BOT_LOGIN names the App slug; the actual comment
        author login carries GitHub's ``[bot]`` suffix — comparison is
        suffix- and case-tolerant."""
        self._env(monkeypatch, tmp_path)
        monkeypatch.setenv("FORGE_GITHUB_BOT_LOGIN", "Acme-Forge")
        install_ghFake(
            monkeypatch,
            self._responses(gh_plan_comment(login="acme-forge[bot]")),
            [],
        )

        rc = main(["--render-brief"])

        assert rc == 0
        assert "## Approved plan" in (tmp_path / ".forge" / "brief.md").read_text()

    def test_envelope_inputs_absent_keeps_the_loud_r05_posture(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """Legacy replay (envelope + spec digest inputs empty): the bound
        comment still drives the plan, the task text comes from the LIVE
        issue, and the lane SAYS the envelope binding is not enforced —
        on stderr, loud."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_REPOSITORY", GH_REPO)
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_runner")  # noqa: S105 — fake
        monkeypatch.setenv("FORGE_ISSUE_NUMBER", str(GH_ISSUE))
        monkeypatch.setenv("FORGE_PLAN_NOTE_ID", str(GH_NOTE_ID))
        monkeypatch.setenv("FORGE_RUN_ID", GH_RUN_ID)
        monkeypatch.delenv("FORGE_ENVELOPE_DIGEST", raising=False)
        monkeypatch.delenv("FORGE_SPEC_DIGEST", raising=False)
        requests: list = []
        install_ghFake(
            monkeypatch,
            self._responses(gh_plan_comment(body=gh_plan_body(with_sections=False))),
            requests,
        )

        rc = main(["--render-brief"])

        assert rc == 0
        issue_url = f"https://api.github.com/repos/{GH_REPO}/issues/{GH_ISSUE}"
        assert any(request.full_url.startswith(issue_url) for request in requests)
        captured = capsys.readouterr()
        assert "approved-brief-bytes binding NOT enforced" in captured.err
        assert "## Forge plan" in (tmp_path / ".forge" / "brief.md").read_text()

    def test_input_absent_falls_back_to_the_loud_unenforced_scan(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """Legacy replay (empty plan_note_id input): the old scan runs and
        the lane SAYS binding is not enforced — on stderr, loud."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_REPOSITORY", GH_REPO)
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_runner")  # noqa: S105 — fake
        monkeypatch.setenv("FORGE_ISSUE_NUMBER", str(GH_ISSUE))
        monkeypatch.delenv("FORGE_PLAN_NOTE_ID", raising=False)
        monkeypatch.delenv("FORGE_RUN_ID", raising=False)
        monkeypatch.delenv("FORGE_ENVELOPE_DIGEST", raising=False)
        monkeypatch.delenv("FORGE_SPEC_DIGEST", raising=False)
        requests: list = []
        install_ghFake(
            monkeypatch,
            {
                f"https://api.github.com/repos/{GH_REPO}/issues/{GH_ISSUE}": gh_issue_payload(),
                f"https://api.github.com/repos/{GH_REPO}/issues/{GH_ISSUE}/comments": [
                    gh_plan_comment(body=gh_plan_body(with_sections=False))
                ],
            },
            requests,
        )

        rc = main(["--render-brief"])

        assert rc == 0
        # The scan URL WAS fetched (the fallback is the old heuristic).
        assert any(
            request.full_url.startswith(
                f"https://api.github.com/repos/{GH_REPO}/issues/{GH_ISSUE}/comments"
            )
            for request in requests
        )
        captured = capsys.readouterr()
        assert "binding NOT enforced" in captured.err
        assert "## Forge plan" in (tmp_path / ".forge" / "brief.md").read_text()


# ----------------------------------------------------------------------
# The BriefEnvelope primitives (A03): byte-faithful render/extract round
# trip, canonical digest binding, fail-closed tamper refusals.
# ----------------------------------------------------------------------


class TestBriefEnvelope:
    def _envelope(self, **overrides) -> dict:
        values = dict(
            run_id="d" * 32,
            task_title="Title",
            task_description="Description.\n\nWith paragraphs.",
            plan_text="1. Step one.\n2. Step two.\n",
            spec_digest="c" * 64,
        )
        values.update(overrides)
        return build_brief_envelope(**values)

    def test_envelope_schema_binds_bytes_run_and_spec(self):
        envelope = self._envelope()

        assert envelope["schema_version"] == 1
        assert envelope["run_id"] == "d" * 32
        assert envelope["task_title_digest"] == hashlib.sha256(b"Title").hexdigest()
        assert (
            envelope["plan_text_digest"]
            == hashlib.sha256(b"1. Step one.\n2. Step two.\n").hexdigest()
        )
        assert envelope["spec_digest"] == "c" * 64
        # envelope_digest = sha256 over the canonical envelope JSON (run_id
        # + task bytes + plan bytes + spec_digest) — byte-identical to the
        # RunSpec canonicalization (the A02 binding it reuses).
        core = {key: value for key, value in envelope.items() if key != "envelope_digest"}
        from forge.runs.spec import canonical_json_digest

        assert envelope["envelope_digest"] == canonical_json_digest(core)

    def test_sections_round_trip_byte_exactly(self):
        envelope = self._envelope()
        body = render_approved_sections(
            task_title=envelope["task_title"],
            task_description=envelope["task_description"],
            plan_text=envelope["plan_text"],
        )

        from forge.harnesses.brief_envelope import extract_approved_sections

        title, description, plan = extract_approved_sections(body)

        assert (title, description, plan) == (
            envelope["task_title"],
            envelope["task_description"],
            envelope["plan_text"],
        )

    def test_sections_tolerate_empty_and_multiline_content(self):
        from forge.harnesses.brief_envelope import extract_approved_sections

        body = render_approved_sections(task_title="T", task_description="", plan_text="")
        assert extract_approved_sections(body) == ("T", "", "")

        multi = render_approved_sections(
            task_title="T",
            task_description="a\n\n- b\n- c\n",
            plan_text="# Plan\n\ntext with\n\nblank lines\n",
        )
        assert extract_approved_sections(multi) == (
            "T",
            "a\n\n- b\n- c\n",
            "# Plan\n\ntext with\n\nblank lines\n",
        )

    def test_missing_sections_raise(self):
        from forge.harnesses.brief_envelope import BriefEnvelopeError, extract_approved_sections

        with pytest.raises(BriefEnvelopeError, match="no approved brief sections"):
            extract_approved_sections("## Forge plan\n\njust the old plan text\n")

    def test_verify_fails_closed_on_any_byte_change(self):
        from forge.harnesses.brief_envelope import BriefEnvelopeError, verify_brief_envelope

        envelope = self._envelope()
        kwargs = {
            "run_id": envelope["run_id"],
            "task_title": envelope["task_title"],
            "task_description": envelope["task_description"],
            "plan_text": envelope["plan_text"],
            "spec_digest": envelope["spec_digest"],
        }
        verify_brief_envelope(envelope["envelope_digest"], **kwargs)  # intact → passes

        for tampered in (
            {**kwargs, "plan_text": kwargs["plan_text"] + "3. Extra.\n"},
            {**kwargs, "task_title": "Changed"},
            {**kwargs, "spec_digest": "9" * 64},
            {**kwargs, "run_id": "e" * 32},
        ):
            with pytest.raises(
                BriefEnvelopeError,
                match="approved brief bytes changed after approval \\(re-approval required\\)",
            ):
                verify_brief_envelope(envelope["envelope_digest"], **tampered)


# ----------------------------------------------------------------------
# The Azure DevOps brief transport (AZ-4): fetch_workitem + --render-brief-azure
# ----------------------------------------------------------------------


ORG_URL = "https://dev.azure.com/fabrikam"
AZDO_PROJECT = "Fabrikam"
WORK_ITEM = 142
READ_TOKEN = "azdo-read-pat"  # noqa: S105 — fake value for tests


class _FakeResponse:
    """urllib.urlopen stand-in: a context manager serving one JSON body."""

    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_witFake(monkeypatch, responses: dict[str, dict], requests: list) -> None:
    """Serve *responses* (url-prefix → payload) to forge.harness_entry's
    urllib calls, recording every request (stdlib-mock style: no sockets)."""

    def fake_urlopen(request, timeout=30):
        url = request.full_url
        requests.append(request)
        for prefix, payload in responses.items():
            if url.startswith(prefix):
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected URL fetched: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def wit_fixtures() -> tuple[dict, dict]:
    work_item = {
        "id": WORK_ITEM,
        "fields": {
            "System.Title": "Ship the flux capacitor",
            "System.Description": "<p>Users <b>cannot</b> reset</p>",
            "System.State": "Approved",
        },
    }
    comments = {
        "totalCount": 3,
        "comments": [
            {
                "text": "sounds great",
                "createdBy": {"displayName": "Dev User", "uniqueName": "dev@fabrikam.example"},
            },
            {
                # A bot comment WITHOUT the plan heading — never the plan.
                "text": "working on it",
                "createdBy": {
                    "displayName": "Forge Bot",
                    "uniqueName": "forge-bot@fabrikam.example",
                },
            },
            {
                # The plan: the LAST bot comment containing the heading.
                "text": "## Forge plan — run `abcd`\n\n1. Add the endpoint.\n",
                "createdBy": {
                    "displayName": "Forge Bot",
                    "uniqueName": "forge-bot@fabrikam.example",
                },
            },
        ],
    }
    return work_item, comments


class TestFetchWorkitem:
    async def test_fetches_item_and_plan_with_the_documented_contract(self, monkeypatch):
        import base64

        requests: list = []
        item, comments = wit_fixtures()
        base = f"{ORG_URL}/{AZDO_PROJECT}/_apis/wit/workItems/{WORK_ITEM}"
        install_witFake(
            monkeypatch,
            {f"{base}?": item, f"{base}/comments?": comments},
            requests,
        )

        body, plan = fetch_workitem(ORG_URL, AZDO_PROJECT, WORK_ITEM, READ_TOKEN)

        assert body == "Ship the flux capacitor\n\nUsers cannot reset"
        assert plan.startswith("## Forge plan — run `abcd`")
        item_req, comments_req = requests
        # Ground truth (research §1.2/§1.3/§5.1): Basic ":"+PAT, GA 7.1 for
        # the item, the preview stripe + markdown for the comments.
        expected = "Basic " + base64.b64encode(f":{READ_TOKEN}".encode()).decode()
        headers = {key.lower(): value for key, value in item_req.headers.items()}
        assert headers["authorization"] == expected
        assert headers["user-agent"] == "forge-harness-entry"
        assert "api-version=7.1" in item_req.full_url
        assert "api-version=7.1-preview.4" in comments_req.full_url
        assert "format=markdown" in comments_req.full_url

    async def test_bot_identity_matches_local_part_and_display_name(self):
        from forge.harness_entry import _is_bot_identity

        assert _is_bot_identity("forge-bot@fabrikam.example", "forge-bot")
        assert _is_bot_identity("Forge Bot", "Forge Bot")  # display-name equality
        assert _is_bot_identity("forge-bot", "forge-bot")
        assert not _is_bot_identity("dev@fabrikam.example", "forge-bot")
        assert not _is_bot_identity("", "forge-bot")
        assert not _is_bot_identity("forge-bot@fabrikam.example", "")

    async def test_a_non_bot_plan_comment_is_never_taken(self, monkeypatch):
        requests: list = []
        comments = {
            "comments": [
                {
                    "text": "## Forge plan — run `fake`\n",
                    "createdBy": {"uniqueName": "mallory@fabrikam.example"},
                }
            ]
        }
        base = f"{ORG_URL}/{AZDO_PROJECT}/_apis/wit/workItems/{WORK_ITEM}"
        install_witFake(
            monkeypatch,
            {f"{base}?": {"fields": {"System.Title": "t"}}, f"{base}/comments?": comments},
            requests,
        )

        body, plan = fetch_workitem(ORG_URL, AZDO_PROJECT, WORK_ITEM, READ_TOKEN)

        assert body == "t"
        assert plan == ""  # no bot-authored plan comment → empty, never guessed


class TestRenderBriefAzure:
    def test_render_brief_azure_mode_fetches_workitem_and_writes_the_brief(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FORGE_AZDO_ORG_URL", ORG_URL)
        monkeypatch.setenv("FORGE_AZDO_PROJECT", AZDO_PROJECT)
        monkeypatch.setenv("FORGE_AZDO_READ_TOKEN", READ_TOKEN)  # noqa: S105 — fake
        monkeypatch.setenv("FORGE_AZDO_BOT_NAME", "forge-bot")
        monkeypatch.setenv("FORGE_ISSUE_NUMBER", str(WORK_ITEM))

        def fake_fetch(org_url, project, issue_number, token, *, bot_name):
            assert (org_url, project, issue_number, token) == (
                ORG_URL,
                AZDO_PROJECT,
                WORK_ITEM,
                READ_TOKEN,
            )
            assert bot_name == "forge-bot"
            return ("Users cannot reset their password.", "## Forge plan — run `abcd`\n...")

        monkeypatch.setattr("forge.harness_entry.fetch_workitem", fake_fetch)

        rc = main(["--render-brief-azure"])

        assert rc == 0
        brief = (tmp_path / ".forge" / "brief.md").read_text()
        assert "## Approved plan" in brief
        assert "## Forge plan — run `abcd`" in brief  # the plan, verbatim
        assert "Users cannot reset their password." in brief

    def test_render_brief_azure_mode_requires_org_project_issue_and_token(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        for name in (
            "FORGE_AZDO_ORG_URL",
            "FORGE_AZDO_PROJECT",
            "FORGE_AZDO_READ_TOKEN",
            "FORGE_ISSUE_NUMBER",
        ):
            monkeypatch.delenv(name, raising=False)

        rc = main(["--render-brief-azure", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (tmp_path / ".forge" / "exit").read_text().strip() == "failed"

    def test_cli_flags_override_the_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FORGE_AZDO_ORG_URL", "https://dev.azure.com/wrong")
        monkeypatch.setenv("FORGE_AZDO_READ_TOKEN", "wrong")  # noqa: S105 — fake

        seen: dict = {}

        def fake_fetch(org_url, project, issue_number, token, *, bot_name):
            seen.update(org_url=org_url, project=project, issue_number=issue_number, token=token)
            return ("body", "plan")

        monkeypatch.setattr("forge.harness_entry.fetch_workitem", fake_fetch)

        rc = main(
            [
                "--render-brief-azure",
                "--org-url",
                ORG_URL,
                "--project",
                AZDO_PROJECT,
                "--read-token",
                READ_TOKEN,
                "--issue",
                str(WORK_ITEM),
            ]
        )

        assert rc == 0
        assert seen == {
            "org_url": ORG_URL,
            "project": AZDO_PROJECT,
            "issue_number": WORK_ITEM,
            "token": READ_TOKEN,
        }


# ----------------------------------------------------------------------
# Usage receipt aggregation (unknown stays unknown, never zero)
# ----------------------------------------------------------------------


class TestParseUsage:
    def test_claude_per_turn_receipts_are_summed(self):
        log = (
            '{"type":"system","subtype":"init"}\n'
            '{"type":"assistant","message":{}}\n'
            '{"type":"result","usage":{"input_tokens":100,'
            '"cache_read_input_tokens":50,"output_tokens":20}}\n'
            '{"type":"result","usage":{"input_tokens":30,"output_tokens":10}}\n'
        )

        usage = parse_usage("claude-code", log)

        assert usage == {
            "input_tokens": 130,
            "cached_input_tokens": 50,
            "output_tokens": 30,
            "driver": "claude-code",
            "completeness": "aggregate",
            "source": "stream-json",
        }

    def test_grok_end_aggregate_wins_over_per_response_events(self):
        log = (
            '{"type":"usage","usage":{"input_tokens":100,"output_tokens":20}}\n'
            '{"type":"usage","usage":{"input_tokens":10,"output_tokens":5}}\n'
            '{"type":"end","usage":{"input_tokens":110,"output_tokens":25}}\n'
        )

        usage = parse_usage("grok-build", log)

        assert usage["input_tokens"] == 110  # NOT 110+110 — no double counting
        assert usage["output_tokens"] == 25

    def test_opencode_has_no_receipt(self):
        assert parse_usage("opencode", '{"type":"message","part":{}}\n') is None

    def test_garbage_lines_are_skipped(self):
        usage = parse_usage("claude-code", "not json\n{broken\n")

        assert usage is None

    def test_tokens_that_never_appear_stay_none(self):
        usage = parse_usage("claude-code", '{"type":"result","usage":{}}\n')

        assert usage is None


# ----------------------------------------------------------------------
# Candidate meta v2 emission (--emit-meta, R16/R23)
# ----------------------------------------------------------------------


class TestEmitCandidateMeta:
    def test_writes_schema_v2_with_identity_digest_and_usage(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("GITHUB_RUN_ID", "501")
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
        staged = tmp_path / "forge-output"
        staged.mkdir()
        diff = staged / "candidate.diff"
        diff.write_text("diff --git a/src/app.py b/src/app.py\n")
        control = tmp_path / ".forge"
        control.mkdir()
        usage_path = control / "usage.json"
        usage_path.write_text(json.dumps({"input_tokens": 42, "output_tokens": 7}))
        exit_path = control / "exit"
        exit_path.write_text("completed\n")

        meta = emit_candidate_meta(
            run_id="d" * 32,
            attempt_base_oid="1" * 40,
            driver="claude-code",
            model="glm-5.3-flash[1m]",
            diff_file=str(diff),
            meta_file=str(staged / "candidate.meta.json"),
            exit_file=str(exit_path),
            usage_file=str(usage_path),
        )

        assert meta["schema_version"] == 2
        assert meta["run_id"] == "d" * 32
        assert meta["attempt_id"] == "501:3"  # GitHub's own run:attempt identity
        assert meta["attempt_base_oid"] == "1" * 40
        assert meta["driver"] == "claude-code"
        assert meta["model"] == "glm-5.3-flash[1m]"
        assert meta["exit"] == "completed"
        # The digest binds the meta to the EXACT staged diff bytes — the
        # control plane re-checks it after download (R16).
        assert meta["manifest_digest"] == (
            f"sha256:{hashlib.sha256(diff.read_bytes()).hexdigest()}"
        )
        # The usage receipt rides INSIDE the meta (R23).
        assert meta["usage"] == {"input_tokens": 42, "output_tokens": 7}
        written = json.loads((staged / "candidate.meta.json").read_text())
        # C10: tuples JSON-round-trip to lists — compare through the same lens
        assert written == json.loads(json.dumps(meta))

    def test_missing_usage_exit_and_env_degrade_to_unknown(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
        monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)
        staged = tmp_path / "forge-output"
        staged.mkdir()
        diff = staged / "candidate.diff"
        diff.write_bytes(b"")

        meta = emit_candidate_meta(
            run_id="d" * 32,
            attempt_base_oid="1" * 40,
            driver="opencode",
            model="",
            diff_file=str(diff),
            meta_file=str(staged / "candidate.meta.json"),
            exit_file=str(tmp_path / ".forge" / "missing-exit"),
            usage_file=str(tmp_path / ".forge" / "missing-usage.json"),
        )

        assert meta["usage"] is None  # unknown stays unknown, never zero
        assert meta["exit"] == "unknown"
        assert meta["attempt_id"] == ""  # no fabricated identity outside Actions

    def test_non_object_usage_json_degrades_to_none(self, tmp_path: Path):
        staged = tmp_path / "forge-output"
        staged.mkdir()
        (staged / "candidate.diff").write_bytes(b"x")
        usage_path = tmp_path / ".forge" / "usage.json"
        usage_path.parent.mkdir()
        usage_path.write_text("[1, 2, 3]")

        meta = emit_candidate_meta(
            run_id="r",
            attempt_base_oid="b",
            driver="claude-code",
            model="",
            diff_file=str(staged / "candidate.diff"),
            meta_file=str(tmp_path / "nope"),
            exit_file=str(tmp_path / "nope"),
            usage_file=str(usage_path),
        )

        assert meta["usage"] is None

    def test_bootstrap_classification_and_profile_digest_ride_the_meta(self, tmp_path: Path):
        """A18: the meta records the lane's environment-bootstrap
        classification (ok|failed) and the digest of the execution profile
        derived from THIS checkout — the executed twin of the digest the
        gate froze into the approved spec."""
        staged = tmp_path / "forge-output"
        staged.mkdir()
        (staged / "candidate.diff").write_bytes(b"diff --git\n")
        control = tmp_path / ".forge"
        control.mkdir()
        (control / "bootstrap").write_text("failed\n")

        meta = emit_candidate_meta(
            run_id="d" * 32,
            attempt_base_oid="1" * 40,
            driver="claude-code",
            model="",
            diff_file=str(staged / "candidate.diff"),
            meta_file=str(staged / "candidate.meta.json"),
            exit_file=str(control / "exit"),
            usage_file=str(control / "usage.json"),
            bootstrap_file=str(control / "bootstrap"),
            profile_digest="e" * 64,
        )

        assert meta["bootstrap"] == "failed"
        assert meta["profile_digest"] == "e" * 64

    def test_bootstrap_and_profile_degrade_to_unknown_honestly(self, tmp_path: Path):
        """No marker file / no derivable profile: unknown stays unknown —
        the meta still carries the (empty) fields, never a fabrication."""
        staged = tmp_path / "forge-output"
        staged.mkdir()
        (staged / "candidate.diff").write_bytes(b"diff --git\n")

        meta = emit_candidate_meta(
            run_id="d" * 32,
            attempt_base_oid="1" * 40,
            driver="opencode",
            model="",
            diff_file=str(staged / "candidate.diff"),
            meta_file=str(staged / "candidate.meta.json"),
            exit_file=str(tmp_path / ".forge" / "exit"),
            usage_file=str(tmp_path / ".forge" / "usage.json"),
            bootstrap_file=str(tmp_path / ".forge" / "missing-bootstrap"),
        )

        assert meta["bootstrap"] == ""  # pre-A18 lane, no marker
        assert meta["profile_digest"] == ""

    def test_a_garbage_bootstrap_status_is_not_a_classification(self, tmp_path: Path):
        staged = tmp_path / "forge-output"
        staged.mkdir()
        (staged / "candidate.diff").write_bytes(b"diff --git\n")
        control = tmp_path / ".forge"
        control.mkdir()
        (control / "bootstrap").write_text("sort-of-fine\n")

        meta = emit_candidate_meta(
            run_id="r",
            attempt_base_oid="b",
            driver="claude-code",
            model="",
            diff_file=str(staged / "candidate.diff"),
            meta_file=str(staged / "candidate.meta.json"),
            exit_file=str(control / "exit"),
            usage_file=str(control / "usage.json"),
            bootstrap_file=str(control / "bootstrap"),
        )

        assert meta["bootstrap"] == ""

    def test_cli_emit_meta_writes_the_meta_and_exits_zero(self, tmp_path: Path, monkeypatch):
        """The workflow's emit step: staged diff + ``--emit-meta`` with the
        dispatched identity → meta v2 on disk, rc 0."""
        monkeypatch.setenv("GITHUB_RUN_ID", "501")
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
        staged = tmp_path / "forge-output"
        staged.mkdir()
        (staged / "candidate.diff").write_bytes(b"diff --git\n")
        control = tmp_path / ".forge"
        control.mkdir()
        (control / "usage.json").write_text(
            json.dumps({"input_tokens": 42, "output_tokens": 7, "completeness": "aggregate"})
        )
        (control / "exit").write_text("completed\n")
        monkeypatch.chdir(tmp_path)

        rc = main(
            [
                "--emit-meta",
                "--forge-run-id",
                "d" * 32,
                "--attempt-base-oid",
                "1" * 40,
                "--driver",
                "claude-code",
                "--model",
                "glm-5.3-flash[1m]",
            ]
        )

        assert rc == 0
        meta = json.loads((staged / "candidate.meta.json").read_text())
        assert meta["schema_version"] == 2
        assert meta["attempt_id"] == "501:1"
        assert meta["exit"] == "completed"
        assert meta["usage"]["input_tokens"] == 42
        assert meta["usage"]["completeness"] == "aggregate"

    def test_cli_emit_meta_derives_the_profile_from_the_checkout(self, tmp_path: Path, monkeypatch):
        """The emit step runs in the bare checkout: the meta's
        profile_digest is derived from the cwd's own lock (A18) and the
        bootstrap marker file is read from its default .forge/ path."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "uv.lock").write_text('[[package]]\nname = "ruff"\nversion = "0.15.7"\n')
        staged = tmp_path / "forge-output"
        staged.mkdir()
        (staged / "candidate.diff").write_bytes(b"diff --git\n")
        control = tmp_path / ".forge"
        control.mkdir()
        (control / "exit").write_text("completed\n")
        (control / "bootstrap").write_text("ok\n")

        rc = main(["--emit-meta", "--forge-run-id", "d" * 32])

        assert rc == 0
        meta = json.loads((staged / "candidate.meta.json").read_text())
        assert meta["bootstrap"] == "ok"
        assert re.fullmatch(r"[0-9a-f]{64}", meta["profile_digest"])

    def test_cli_emit_meta_fails_loud_on_a_missing_diff(self, tmp_path: Path, monkeypatch):
        """No staged diff → rc 1: the upload's if-no-files-found then turns
        the lane into an infrastructure-class failure instead of shipping a
        partial artifact."""
        monkeypatch.chdir(tmp_path)

        rc = main(["--emit-meta", "--forge-run-id", "d" * 32])

        assert rc == 1


# ----------------------------------------------------------------------
# main(): the CLI contract
# ----------------------------------------------------------------------


@pytest.fixture()
def lane(tmp_path: Path) -> Path:
    """A workspace shaped like the Actions lane: brief + control dir."""
    (tmp_path / ".forge").mkdir()
    (tmp_path / BRIEF).write_text("# the plan\n")
    return tmp_path


def run_main(lane: Path, monkeypatch: pytest.MonkeyPatch, driver_script: str, **kwargs):
    """Run main() inside *lane* with *driver_script* as the rendered script."""
    monkeypatch.chdir(lane)
    monkeypatch.setattr("forge.harness_entry.render_driver_script", lambda *a, **k: driver_script)
    argv = ["--exit-file", ".forge/exit"]
    for name, value in kwargs.items():
        argv += [f"--{name}", value]
    return main(argv)


class TestMain:
    def test_successful_driver_writes_completed_and_usage(self, lane, monkeypatch):
        script = (
            "echo 'some agent output' >&2\n"
            "printf '%s\\n' "
            '\'{"type":"result","usage":{"input_tokens":42,"output_tokens":7}}\' '
            ">> .forge/events.jsonl\n"
        )

        rc = run_main(lane, monkeypatch, script, driver="claude-code", model="m", brief=BRIEF)

        assert rc == 0
        assert (lane / ".forge" / "exit").read_text().strip() == "completed"
        usage = json.loads((lane / ".forge" / "usage.json").read_text())
        assert usage["input_tokens"] == 42
        assert usage["completeness"] == "aggregate"

    def test_failing_driver_writes_failed_but_still_exits_zero(self, lane, monkeypatch):
        """The lane's audit trail outranks the step's color: the workflow's
        candidate steps run ``if: always()`` and forge classifies from the
        meta exit — the GitLab templates' exact contract."""
        script = "echo 'agent exploded' >&2\nexit 3\n"

        rc = run_main(lane, monkeypatch, script, driver="claude-code", brief=BRIEF)

        assert rc == 0
        assert (lane / ".forge" / "exit").read_text().strip() == "failed"

    def test_missing_brief_fails_before_running_anything(self, lane, monkeypatch):
        rc = run_main(
            lane, monkeypatch, "echo should never run", driver="claude-code", brief=".forge/nope.md"
        )

        assert rc == 1
        assert (lane / ".forge" / "exit").read_text().strip() == "failed"

    def test_unknown_driver_fails_before_running_anything(self, lane, monkeypatch):
        rc = run_main(lane, monkeypatch, "echo should never run", driver="codex", brief=BRIEF)

        assert rc == 1
        assert (lane / ".forge" / "exit").read_text().strip() == "failed"

    def test_env_fallbacks_mirror_the_workflow_template(self, lane, monkeypatch):
        """The template passes FORGE_DRIVER / FORGE_MODEL / FORGE_BRIEF /
        FORGE_EXIT_FILE as env — the CLI must accept those names."""

        monkeypatch.chdir(lane)
        monkeypatch.setenv("FORGE_DRIVER", "opencode")
        monkeypatch.setenv("FORGE_MODEL", "zai/glm")
        monkeypatch.setenv("FORGE_BRIEF", BRIEF)
        monkeypatch.setenv("FORGE_EXIT_FILE", ".forge/exit")
        monkeypatch.setattr("forge.harness_entry.render_driver_script", lambda *a, **k: "true")

        rc = main([])

        assert rc == 0
        assert (lane / ".forge" / "exit").read_text().strip() == "completed"

    def test_sdk_lane_exit_classifies_and_keeps_the_lane_usage_receipt(self, lane, monkeypatch):
        """The SDK lanes' "driver" is forge.lane_driver: its nonzero exit
        classifies the run failed (through the same .forge/exit contract),
        and the usage receipt IT wrote is never clobbered by a zeroed
        parse of an event log nothing tee'd into."""
        script = (
            "printf '%s\\n' "
            '\'{"input_tokens": 11, "output_tokens": 7, "driver": "codex-sdk-lane", '
            '"completeness": "unknown", "source": "session.usage.updated"}\' '
            "> .forge/usage.json\n"
            "exit 1\n"
        )

        rc = run_main(lane, monkeypatch, script, driver="codex-sdk-lane", brief=BRIEF)

        assert rc == 0  # the lane's audit trail outranks the step's color
        assert (lane / ".forge" / "exit").read_text().strip() == "failed"
        usage = json.loads((lane / ".forge" / "usage.json").read_text())
        assert usage["input_tokens"] == 11
        assert usage["completeness"] == "unknown"


# ----------------------------------------------------------------------
# Minimal lane credentials in the rendered scripts (R15): the grok lane
# consumes the provider-native subscription blob; no other driver's
# script ever touches another provider's credential.
# ----------------------------------------------------------------------


class TestGrokLaneCredential:
    def test_grok_writes_the_subscription_auth_blob(self):
        """FORGE_GROK_AUTH (full ~/.grok/auth.json contents, the GitLab
        template contract) lands in ~/.grok/auth.json, owner-only."""
        script = render_driver_script("grok-build", "m", BRIEF)

        assert 'printf "%s" "$FORGE_GROK_AUTH" > ~/.grok/auth.json' in script
        assert "chmod 600 ~/.grok/auth.json" in script
        # Guarded: an unauthenticated lane still runs (the driver reports
        # its own auth failure), it never breaks on an empty write.
        assert 'if [ -n "$FORGE_GROK_AUTH" ]; then' in script

    def test_no_other_driver_touches_another_providers_credential(self):
        """Capability/credential pairs: only the grok script consumes
        FORGE_GROK_AUTH, and no script consumes the workflow's other
        provider credentials at all (env gating is the template's job)."""
        for driver, forbidden in (
            ("claude-code", ("FORGE_GROK_AUTH", "COPILOT_GITHUB_TOKEN", "XAI_API_KEY")),
            ("opencode", ("FORGE_GROK_AUTH", "COPILOT_GITHUB_TOKEN", "XAI_API_KEY")),
            ("copilot", ("FORGE_GROK_AUTH", "XAI_API_KEY")),
            ("grok-build", ("COPILOT_GITHUB_TOKEN", "XAI_API_KEY")),
            ("codex-sdk-lane", ("FORGE_GROK_AUTH", "COPILOT_GITHUB_TOKEN", "XAI_API_KEY")),
            ("opencode-sdk-lane", ("FORGE_GROK_AUTH", "COPILOT_GITHUB_TOKEN", "XAI_API_KEY")),
        ):
            script = render_driver_script(driver, "m", BRIEF)
            for name in forbidden:
                assert name not in script, (driver, name)

    def test_the_sdk_lanes_own_credentials_are_guarded_or_absent(self):
        """codex-sdk-lane consumes ONLY its own optional auth blob; the
        opencode lane consumes no credential at all in the rendered script
        (the provider key rides the ambient env into the lane runner)."""
        codex = render_driver_script("codex-sdk-lane", "m", BRIEF)
        assert 'if [ -n "$FORGE_CODEX_AUTH" ]; then' in codex
        opencode = render_driver_script("opencode-sdk-lane", "m", BRIEF)
        for name in ("FORGE_CODEX_AUTH", "ZAI_API_KEY", "OPENCODE_PROVIDER_API_KEY"):
            assert name not in opencode, name


class TestDriverVersionWiring:
    def test_env_pins_flow_resolved_into_the_render(self, lane: Path, monkeypatch):
        """FORGE_DRIVER_VERSIONS (the same-named repo VARIABLE, passed
        through by the workflow) resolves into the full pin map: overrides
        win, defaults fill the rest."""
        monkeypatch.chdir(lane)
        seen: dict = {}

        def fake_render(driver, model, brief, **kwargs):
            seen.update(driver=driver, **kwargs)
            return "true"

        monkeypatch.setattr("forge.harness_entry.render_driver_script", fake_render)
        monkeypatch.setenv("FORGE_DRIVER_VERSIONS", '{"grok-build": "1.0.30"}')

        rc = main(["--driver", "grok-build", "--exit-file", ".forge/exit"])

        assert rc == 0
        assert seen["driver_versions"]["grok-build"] == "1.0.30"
        assert seen["driver_versions"]["claude-code"] == DEFAULT_DRIVER_VERSIONS["claude-code"]

    def test_broken_env_pins_fail_the_lane_before_any_driver_runs(self, lane: Path, monkeypatch):
        """Fail-closed (the MCP-parse posture): a typo in the pins must
        never downgrade the lane to an unpinned install."""
        monkeypatch.chdir(lane)
        monkeypatch.setenv("FORGE_DRIVER_VERSIONS", "{not json")

        rc = main(["--driver", "claude-code", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (lane / ".forge" / "exit").read_text().strip() == "failed"

    def test_unset_pins_run_on_the_known_good_defaults(self, lane: Path, monkeypatch):
        monkeypatch.chdir(lane)
        seen: dict = {}

        def fake_render(driver, model, brief, **kwargs):
            seen.update(**kwargs)
            return "true"

        monkeypatch.setattr("forge.harness_entry.render_driver_script", fake_render)
        monkeypatch.delenv("FORGE_DRIVER_VERSIONS", raising=False)

        rc = main(["--driver", "opencode", "--exit-file", ".forge/exit"])

        assert rc == 0
        assert seen["driver_versions"] == DEFAULT_DRIVER_VERSIONS


class TestQualityGateAllowlist:
    """The brief + AGENTS.md tell the agent to run the repo's own quality
    gates — every driver must be ALLOWED to execute them (LIVE-found: `make
    lint`, `uv run ruff`, venv python and `set -o pipefail &&` compounds
    were denied and the agent burned turns on permission prompts). The
    mechanical commit/push deny still wins everywhere."""

    _GATES = ("make", "uv", "set", "ruff", "mypy", "pytest")

    def test_claude_allowlist_carries_the_full_gate_set(self):
        script = render_driver_script("claude-code", "m", BRIEF)
        for gate in self._GATES:
            assert f"Bash({gate}:*)" in script, gate
        # analysis pipelines: every segment must be allowlisted (LIVE-found:
        # `... | awk 'length > 100'` and sed substitutions were denied)
        for util in ("awk", "sed", "sort", "cut", "tr", "find"):
            assert f"Bash({util}:*)" in script, util
        # bypass mode makes redirects/--add-dir moot (everything allowed
        # except the mechanical deny)
        assert "--permission-mode bypassPermissions" in script
        # isolated ephemeral config: no auto-memory bleed across runs on
        # reused runners (the MEMORY.md index auto-loads into context)
        assert 'CLAUDE_CONFIG_DIR="$(mktemp -d /tmp/claude-lane-config.XXXXXX)"' in script
        # venv forms (harmless where the venv does not exist — a normal
        # tool result beats a permission denial)
        assert "Bash(.venv/bin/python:*)" in script
        # mechanical deny intact
        assert '--disallowedTools "Bash(git commit:*)" "Bash(git push:*)"' in script

    def test_grok_grants_carry_the_full_gate_set(self):
        script = render_driver_script("grok-build", "m", BRIEF)
        for gate in self._GATES:
            assert f"--allow 'Bash({gate}:*)'" in script, gate
        assert "--deny 'Bash(git commit:*)'" in script

    def test_copilot_grants_carry_the_full_gate_set(self):
        script = render_driver_script("copilot", "m", BRIEF)
        for gate in self._GATES:
            assert f"--allow-tool 'shell({gate}:*)'" in script, gate
        assert "--deny-tool 'shell(git commit)'" in script


# ----------------------------------------------------------------------
# A09: rendered permission lists are TOKENIZED. Adjacent Python string
# literals without commas concatenate silently — "Bash(python3:*)"
# "Bash(python:*)" "Bash(.venv/bin/python:*)" once rendered as ONE merged
# rule the driver could never match. Every list in every rendered script
# must carry each rule as its own standalone token.
# ----------------------------------------------------------------------

#: Signatures of GLUED rules (a rule tail meeting the next rule/flag with
#: no separator between). None may appear in any rendered script.
_GLUED_SIGNATURES = (
    "*)Bash(",  # claude comma-list glue: Bash(a:*)Bash(b:*)
    ")*shell(",  # copilot glue: shell(a)shell(b)
    "*)'--allow",  # grok flag glue: 'Bash(a:*)'--allow 'Bash(b:*)'
    "*)'--deny",
    "*)'--allow-tool",  # copilot flag glue
    "*)'--deny-tool",
)


def _claude_allowed_tools_tokens(script: str) -> list[str]:
    """The claude ``--allowedTools`` value, split into its rule tokens."""
    line = next(line for line in script.splitlines() if "--allowedTools" in line)
    raw = line.strip().removeprefix("--allowedTools ").removesuffix(" \\").strip()
    value = shlex.split(raw)[0]
    return value.split(",")


class TestPermissionListsAreTokenized:
    def test_claude_allowed_tools_is_a_comma_list_of_standalone_rules(self):
        """The rendered allowTools is EXACTLY the rule tuple, one rule per
        comma token — the glued python3/python/venv rules exist as separate
        entries again."""
        script = render_driver_script("claude-code", "m", BRIEF)
        tokens = _claude_allowed_tools_tokens(script)

        # Full equality: any glued pair would shorten this list.
        assert tokens == list(_CLAUDE_TOOL_RULES)
        # ...and the rules the review called out are standalone members:
        for rule in (
            "Bash(python3:*)",
            "Bash(python:*)",
            "Bash(.venv/bin/python:*)",
            "Bash(make:*)",
            "Bash(uv:*)",
            "Bash(awk:*)",
            "Bash(set:*)",
        ):
            assert rule in tokens, rule
        # Every token is ONE rule — never a concatenation.
        assert all(token.count("Bash(") == 1 for token in tokens)

    def test_claude_mcp_grants_join_as_plain_comma_tokens(self):
        """MCP grants ride the same explicit-comma serialization as PLAIN
        names (the GitLab contract). The old per-name shlex.quote shipped
        literal quote characters INSIDE the value — rules named
        'mcp__x__*' (with quotes) that could never match."""
        servers = {"context7": {"type": "http", "url": "https://mcp.example.com/mcp"}}
        script = render_driver_script("claude-code", "m", BRIEF, mcp_servers=servers)
        tokens = _claude_allowed_tools_tokens(script)

        assert tokens[: len(_CLAUDE_TOOL_RULES)] == list(_CLAUDE_TOOL_RULES)
        assert tokens[len(_CLAUDE_TOOL_RULES) :] == ["mcp__context7__*", "mcp__context7"]
        line = next(line for line in script.splitlines() if "--allowedTools" in line)
        raw = line.strip().removeprefix("--allowedTools ").removesuffix(" \\").strip()
        assert "'" not in shlex.split(raw)[0]  # no quote characters inside the value

    def test_no_rendered_script_carries_a_glued_rule(self):
        """Every driver, with and without MCP servers: no glued artifact
        anywhere in the rendered script."""
        for driver in DRIVERS:
            for servers in ({}, {"context7": {"type": "http", "url": "https://mcp.example.com"}}):
                script = render_driver_script(driver, "m", BRIEF, mcp_servers=servers)
                for signature in _GLUED_SIGNATURES:
                    assert signature not in script, (driver, bool(servers), signature)

    def test_grok_and_copilot_flags_each_carry_exactly_one_rule(self):
        """The audited flag lists (grok --allow/--deny, copilot
        --allow-tool/--deny-tool): every flag value is ONE rule — the
        separator class never regresses there either."""
        grok = render_driver_script("grok-build", "m", BRIEF)
        grok_values = re.findall(r"--(?:allow|deny) '([^']*)'", grok)
        assert grok_values, "grok renders no grants?"
        assert all(value.count("Bash(") == 1 for value in grok_values)

        copilot = render_driver_script("copilot", "m", BRIEF)
        copilot_values = re.findall(r"--(?:allow|deny)-tool '([^']*)'", copilot)
        assert copilot_values, "copilot renders no grants?"
        # 'read,write' is the documented two-grant comma-list; everything
        # else is exactly one shell(...) rule.
        for value in copilot_values:
            assert value == "read,write" or value.count("shell(") == 1, value


class TestRenderBriefAzureEnforcedB04:
    """The ENFORCED AzDO brief (B04): with plan_note_id + envelope_digest +
    spec_digest dispatched, the brief comes from EXACTLY the addressed
    comment's approved sections re-verified against the frozen digest — an
    edited comment/work item fails the render CLOSED (rc 1 + .forge/exit
    failed), never the live-item heuristic, never a fallback brief."""

    ENV = {
        "FORGE_AZDO_ORG_URL": ORG_URL,
        "FORGE_AZDO_PROJECT": AZDO_PROJECT,
        "FORGE_AZDO_READ_TOKEN": READ_TOKEN,  # noqa: S105 — fake
        "FORGE_ISSUE_NUMBER": str(WORK_ITEM),
        "FORGE_RUN_ID": "abcd" * 8,
    }

    def _comment_with(self, sections: str) -> str:
        return f"## Forge plan — run `abcd`\n\n{sections}\n---\n\n**Plan digest:** `x`"

    def _install(self, monkeypatch, requests: list, *, comment_override: str = "") -> None:
        from forge.harnesses.brief_envelope import (
            build_brief_envelope,
            render_approved_sections,
        )

        sections = render_approved_sections(
            task_title="Add a widget",
            task_description="Make the widget.",
            plan_text="1. do it",
        )
        envelope = build_brief_envelope(
            run_id=self.ENV["FORGE_RUN_ID"],
            task_title="Add a widget",
            task_description="Make the widget.",
            plan_text="1. do it",
            spec_digest="spec-digest-1",
        )
        monkeypatch.setenv("FORGE_PLAN_NOTE_ID", "77")
        monkeypatch.setenv("FORGE_ENVELOPE_DIGEST", envelope["envelope_digest"])
        monkeypatch.setenv("FORGE_SPEC_DIGEST", "spec-digest-1")
        comment_text = comment_override or self._comment_with(sections)
        base = f"{ORG_URL}/{AZDO_PROJECT}/_apis/wit/workItems/{WORK_ITEM}"
        install_witFake(
            monkeypatch,
            {f"{base}/comments/77?": {"text": comment_text}},
            requests,
        )

    def test_enforced_render_writes_the_verified_brief(self, tmp_path, monkeypatch):
        import requests as _  # noqa: F401 — ensure import path only

        monkeypatch.chdir(tmp_path)
        for k, v in self.ENV.items():
            monkeypatch.setenv(k, v)

        requests: list = []
        self._install(monkeypatch, requests)

        rc = main(["--render-brief-azure"])

        assert rc == 0
        brief = (tmp_path / ".forge" / "brief.md").read_text()
        assert "Add a widget" in brief
        assert "1. do it" in brief

    def test_an_edited_comment_after_approval_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for k, v in self.ENV.items():
            monkeypatch.setenv(k, v)
        requests: list = []
        # the SAME addressed comment, its bytes EDITED after approval
        self._install(
            monkeypatch, requests, comment_override=self._comment_with("1. do something ELSE")
        )

        rc = main(["--render-brief-azure", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (tmp_path / ".forge" / "exit").read_text().strip() == "failed"
        assert not (tmp_path / ".forge" / "brief.md").exists()


class TestCommandReceiptsC10:
    def test_the_wrapper_receipts_file_rides_the_meta(self, tmp_path, monkeypatch):
        """C10: .forge/commands.tsv (argv<TAB>exit<TAB>report rows) lands in
        the meta's observed_execution — proof a command RAN, which the
        declared profile can never claim."""

        monkeypatch.chdir(tmp_path)
        (tmp_path / "forge-output").mkdir()
        (tmp_path / "forge-output" / "candidate.diff").write_text("+x\n")
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "exit").write_text("completed\n")
        (tmp_path / ".forge" / "commands.tsv").write_text(
            "pytest -q\t0\t\nruff check src\t2\truff.log\n"
        )

        meta = emit_candidate_meta(
            run_id="a" * 32, attempt_base_oid="b" * 40, driver="claude-code", model="m"
        )

        commands = list(meta["observed_execution"]["commands"])
        assert ("pytest -q", 0, "") in commands or ["pytest -q", 0, ""] in commands
        assert ("ruff check src", 2, "ruff.log") in commands or [
            "ruff check src",
            2,
            "ruff.log",
        ] in commands

    def test_a_malformed_receipts_file_claims_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "forge-output").mkdir()
        (tmp_path / "forge-output" / "candidate.diff").write_text("+x\n")
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "exit").write_text("completed\n")
        (tmp_path / ".forge" / "commands.tsv").write_text("garbage line\npytest\tnotanint\t\n")

        meta = emit_candidate_meta(
            run_id="a" * 32, attempt_base_oid="b" * 40, driver="claude-code", model="m"
        )

        assert not meta["observed_execution"]["commands"]
