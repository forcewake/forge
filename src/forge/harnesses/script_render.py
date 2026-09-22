"""Render the lane driver scripts from package-data ``.sh`` templates.

The driver scripts are REAL shell files shipped in the wheel under
``forge/harnesses/scripts/`` and read via :mod:`importlib.resources`
(never ``__file__`` joins — those work in editable installs and fail in
real wheels). What the renderer does is TOKEN SUBSTITUTION ONLY:

- placeholders are ``@@TOKEN@@`` (stdlib :class:`string.Template` with a
  custom pattern — the default ``$`` delimiter would collide with the
  shell's own ``$``);
- the token table carries the variable parts (version pins, the quoted
  prompt pointer, model routing, MCP provisioning) as VALUES, never as
  textual surgery on the script;
- optional blocks (MCP provisioning, credential blobs) arrive as tokens
  whose value is the rendered fragment or the empty string — a token
  alone on a line with an empty value consumes its own line, so absence
  never leaves a blank line behind;
- conditionals live in the SCRIPT at runtime (``${VAR:-default}``,
  ``if [ -n "$VAR" ]``), not in the renderer — render-time branching is
  what ``bash -n``/shellcheck can never see.

No Jinja (or any engine) on purpose: the seven per-driver render arms
this module replaces were Python string-concatenation blobs whose
adjacent-literal glue bugs (A09) and quoting regressions were invisible
to every shell tool; as ``.sh`` files they are lintable
(``tests/test_script_rendering.py`` runs ``bash -n`` over every rendered
combination) and byte-pinned against golden fixtures.

The fail-closed version-pin guard rides WITH the renderer (the pin lands
in a shell command): :data:`_DRIVER_VERSION_RE` refuses any pin outside
``[A-Za-z0-9._-]`` before a single byte is substituted.
"""

from __future__ import annotations

import json
import re
import shlex
import string
from importlib.resources import files as _resource_files
from typing import Any

from forge.harnesses.mcp import for_claude, for_copilot, for_grok, for_opencode
from forge.harnesses.prompt import TASK_PROMPT

#: Claude's scoped shell allowlist (same posture as the GitLab template:
#: edits auto-accepted, shell limited to read-only git).
# The quality bar demands the agent RUN the tests (ADR-0008): the
# allowlist carries the test/lint commands per runner reality — hosted
# Actions runners have NO .venv (forge installs into the interpreter via
# pip; LIVE-found: ".venv/bin/python" patterns denied everything and the
# agent flailed). Read-only utils are allowed too: Claude Code requires
# EVERY segment of a compound command to be allowed, and exploration
# commands (ls|grep|head) otherwise deny the whole pipeline. Writes stay
# denied: commit/push are --disallowedTools and the push URL is FORBIDDEN.
# A09: ONE rule per literal, serialized by an explicit ",".join. Adjacent
# string literals inside a parenthesized concatenation silently GLUE when
# a comma is missing (LIVE-found: "Bash(python3:*)" "Bash(python:*)"
# "Bash(.venv/bin/python:*)" rendered as ONE merged rule the driver could
# never match) — the rule list now lives as one tuple in one file, and
# the .sh template cannot glue what it never splits.
_CLAUDE_TOOL_RULES: tuple[str, ...] = (
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git -C * diff:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(grep:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(which:*)",
    # Read-only text processing (LIVE-found: `... | awk 'length > 100'` and
    # `sed 's/^+//'` in analysis pipelines were DENIED — every pipeline
    # segment must be allowlisted, not just the head command):
    "Bash(awk:*)",
    "Bash(sed:*)",
    "Bash(sort:*)",
    "Bash(uniq:*)",
    "Bash(cut:*)",
    "Bash(tr:*)",
    "Bash(find:*)",
    "Bash(diff:*)",
    "Bash(basename:*)",
    "Bash(dirname:*)",
    "Bash(realpath:*)",
    "Bash(python3:*)",
    "Bash(python:*)",
    "Bash(.venv/bin/python:*)",
    "Bash(./.venv/bin/python:*)",
    "Bash(.venv/bin/pytest:*)",
    "Bash(.venv/bin/ruff:*)",
    "Bash(.venv/bin/mypy:*)",
    "Bash(./.venv/bin/pytest:*)",
    "Bash(./.venv/bin/ruff:*)",
    "Bash(./.venv/bin/mypy:*)",
    "Bash(pip install:*)",
    "Bash(pip list)",
    "Bash(pip show:*)",
    "Bash(pip3 install:*)",
    # The repo's own quality gates (AGENTS.md / brief quality bar tell the
    # agent to run them — LIVE-found: `make lint`, `uv run ruff`, bare
    # pytest/ruff/mypy and `set -o pipefail &&` compounds were all DENIED
    # and the agent burned turns flailing against permission prompts):
    "Bash(pytest:*)",
    "Bash(ruff:*)",
    "Bash(mypy:*)",
    "Bash(uv:*)",
    "Bash(make:*)",
    "Bash(set:*)",
)
# Serialized ONLY at the token-table site via the explicit
# ",".join((*_CLAUDE_TOOL_RULES, *mcp_rules)) — never by literal
# concatenation (A09).
#: R28-21: the .NET lane's tool grants on top of the claude allowlist —
#: the quality bar demands the agent RUN the pinned, locked .NET pipeline
#: itself (restore/build/test), so the dotnet verbs are allowlisted beside
#: the read-only git rules. commit/push stay mechanically denied.
_DOTNET_TOOL_RULES: tuple[str, ...] = (
    "Bash(dotnet --version)",
    "Bash(dotnet restore:*)",
    "Bash(dotnet build:*)",
    "Bash(dotnet test:*)",
    "Bash(dotnet format:*)",
    "Bash(dotnet tool:*)",
    "Bash(dotnet nuget:*)",
)
#: The SDK-lane drivers: the agent is driven by ``forge.lane_driver``
#: (the REAL interactive driver clients), not a scripted ``-p`` call —
#: the rendered script only provisions the CLI and hands the lane over
#: (the Actions/AzDO mirror of the codex/opencode GitLab sdk-lane
#: templates).
LANE_DRIVERS = ("claude-sdk-lane", "codex-sdk-lane", "opencode-sdk-lane", "copilot-sdk-lane")

#: The SCRIPTED drivers: a rendered one-shot CLI invocation with the
#: shared ``-p`` prompt pointer and the tee'd event stream.
SCRIPTED_DRIVERS = ("claude-code", "grok-build", "opencode", "copilot", "dotnet-lane")

#: Drivers understood by the harness entry point (the shipped set):
#: the scripted drivers above plus the SDK-lane drivers.
DRIVERS = SCRIPTED_DRIVERS + LANE_DRIVERS

#: R15 known-good driver CLI versions: an unpinned install rides the npm
#: ``latest`` dist-tag, so a CLI release can silently change lane behavior
#: between the plan gate and the run. The install preambles pin
#: ``@<version>``; each value cites its source and is refreshed
#: deliberately, never by an automated bump.
DEFAULT_DRIVER_VERSIONS: dict[str, str] = {
    # npm registry latest at the R15 slice (2026-09-17); the lane needs
    # --permission-prompts none, documented v2.1.259+.
    "claude-code": "2.1.276",
    # GROUND TRUTH 2026-09-13 (docs/research/2026-09-13-harness-interfaces.md §3):
    # verified against the installed CLI.
    "grok-build": "1.0.30",
    # npm registry latest at the R15 slice (2026-09-17).
    "opencode": "1.18.31",
    # npm registry latest at the R15 slice (2026-09-17).
    "copilot": "1.0.86",
    # LIVE-verified 2026-09-21 (docs/research/2026-09-21-codex-app-server.md LIVE
    # CORRECTION + codex-live.json): the app-server wire the codex driver
    # client speaks — the sandbox spellings and turn-completion semantics
    # this lane depends on.
    # LIVE-verified 2026-09-21 (claude-live.json: "claude 2.1.273 (Claude
    # Code)") — the SDK lane drives the bundled CLI.
    "claude-sdk-lane": "2.1.273",
    "codex-sdk-lane": "0.153.4",
    # LIVE-verified 2026-09-21 (opencode-live.json: "opencode v2.0.10") —
    # the serve wire layer the opencode driver client targets.
    "opencode-sdk-lane": "2.0.10",
    # copilot-sdk-lane: the SAME 1.0.86 pin the scripted copilot lane
    # carries (the 2026-09-17 registry slice; npm latest is 1.0.88 today).
    # NO live smoke has verified the ACP wire the driver client speaks —
    # ACP is public preview and subject to change, so this pin moves only
    # with a deliberate re-smoke (the NXT-27 doctrine).
    "copilot-sdk-lane": "1.0.86",
    # R28-21: the .NET lane drives the SAME claude CLI the claude-code arm
    # renders (the lane's own runtime pin is the digest-pinned dotnet SDK
    # image + global.json + nuget.lock.json — see
    # docs/harnesses/dotnet-lane.md); the npm pin mirrors claude-code's.
    "dotnet-lane": "2.1.276",
}

#: A version/dist-tag token safe to splice into an npm install spec
#: (semver, dist-tags like ``latest``). Anything else is refused — the
#: pin lands in a shell command and must never carry metacharacters.
_DRIVER_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: driver → (npm package, the name the retry loop's failure echo carries)
#: — the install/echo surface (the CLI binary itself is named in each
#: driver template's ``--version`` line).
_NPM_TARGETS: dict[str, tuple[str, str]] = {
    "claude-code": ("@anthropic-ai/claude-code", "claude-code"),
    "grok-build": ("@xai-official/grok", "grok"),
    "opencode": ("opencode-ai", "opencode"),
    "copilot": ("@github/copilot", "copilot"),
    "claude-sdk-lane": ("@anthropic-ai/claude-code", "claude-code"),
    "codex-sdk-lane": ("@openai/codex", "codex"),
    "opencode-sdk-lane": ("opencode-ai", "opencode"),
    "copilot-sdk-lane": ("@github/copilot", "copilot"),
    # R28-21: the .NET lane's agent is the claude CLI (it rides the forge
    # gateway); the reproducible .NET runtime comes from the pinned SDK
    # image, not from npm.
    "dotnet-lane": ("@anthropic-ai/claude-code", "claude"),
}

#: The scripted drivers that get an MCP config file written, →
#: (header comment, optional ``mkdir`` line, config path, JSON renderer
#: from :mod:`forge.harnesses.mcp`). claude-code is absent from the
#: optionality rule: its config is ALWAYS written (empty map = only the
#: repo's own MCP configs are locked out — injection-surface reduction).
_MCP_TARGETS: dict[str, tuple[str, str, str, Any]] = {
    "claude-code": (
        (
            "# MCP (ADR-0022): config ONLY from the CI variable — strict mode\n"
            "# locks out the repo's own .mcp.json (injection surface)."
        ),
        "",
        "/tmp/forge-mcp.json",
        for_claude,
    ),
    "grok-build": (
        "# MCP (ADR-0022): claude-shaped mcpServers in Grok's settings.",
        "mkdir -p ~/.grok",
        "~/.grok/settings.json",
        for_grok,
    ),
    "copilot": (
        "# MCP (ADR-0022): documented Copilot CLI config location.",
        "mkdir -p ~/.copilot",
        "~/.copilot/mcp-config.json",
        for_copilot,
    ),
}


def resolve_driver_versions(raw: str | None) -> dict[str, str]:
    """The effective per-driver CLI version pins (R15).

    *raw* is the ``FORGE_DRIVER_VERSIONS`` JSON object (driver → version)
    — the workflow template passes the same-named repo VARIABLE through.
    Absent/empty → :data:`DEFAULT_DRIVER_VERSIONS`; a per-driver override
    wins; the literal ``"latest"`` keeps the unpinned install. Fail-closed
    (same posture as the MCP parse): malformed JSON, a non-object, an
    unknown driver id, or a version with characters outside
    ``[A-Za-z0-9._-]`` raises ``ValueError`` — a typo must never silently
    downgrade the lane to an unpinned (or shell-interpreted) install.
    """
    pins = dict(DEFAULT_DRIVER_VERSIONS)
    text = str(raw or "").strip()
    if not text:
        return pins
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_DRIVER_VERSIONS is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("FORGE_DRIVER_VERSIONS must be a JSON object of driver → version")
    for name, version in data.items():
        if name not in DEFAULT_DRIVER_VERSIONS:
            raise ValueError(
                f"FORGE_DRIVER_VERSIONS names unknown driver {name!r} "
                f"(expected one of {', '.join(sorted(DEFAULT_DRIVER_VERSIONS))})"
            )
        pin = str(version).strip()
        if not _DRIVER_VERSION_RE.match(pin):
            raise ValueError(
                f"FORGE_DRIVER_VERSIONS[{name!r}]: bad version {pin!r} "
                "(letters, digits, dot, underscore, dash only)"
            )
        pins[name] = pin
    return pins


class _TokenTemplate(string.Template):
    """:class:`string.Template` over ``@@TOKEN@@`` placeholders.

    The custom pattern replaces the default ``$``-delimiter grammar
    wholesale (``$`` belongs to the shell): a token is an uppercase
    ``@@NAME@@``; a dangling ``@@`` with no name is INVALID and fails the
    render fail-closed. The ``braced``/``escaped`` groups never match —
    they exist because ``Template.substitute`` reads them. ``flags = 0``
    drops the base class's IGNORECASE (tokens are UPPERCASE only).
    """

    flags = 0
    pattern = (
        r"@@(?P<named>[A-Z_][A-Z0-9_]*)@@"
        r"|(?P<braced>(?!))"
        r"|(?P<escaped>(?!))"
        r"|(?P<invalid>@@)"
    )


def _template_text(relative: str) -> str:
    """One package-data template's bytes (py313 importlib.resources idiom)."""
    resource = _resource_files("forge.harnesses") / "scripts" / relative
    return resource.read_text(encoding="utf-8")


def _strip_one_trailing_newline(text: str) -> str:
    return text[:-1] if text.endswith("\n") else text


def _apply(relative: str, tokens: dict[str, str]) -> str:
    """Substitute *tokens* into the ``scripts/<relative>`` template.

    A token whose value is empty AND which stands alone on a line
    consumes its own line first (an absent optional block must never
    leave a blank line behind); then :meth:`string.Template.substitute`
    does the single-pass substitution — unknown tokens in a template
    fail closed, and substituted VALUES are never rescanned (shell
    ``$``/``$(...)`` in values pass through untouched).
    """
    text = _template_text(relative)
    for token, value in tokens.items():
        if not value:
            text = re.sub(rf"(?m)^@@{token}@@(?:\n|\Z)", "", text)
    try:
        return _TokenTemplate(text).substitute(tokens)
    except KeyError as exc:
        raise ValueError(f"scripts/{relative}: unbound token {exc}") from exc


def _fragment(relative: str, tokens: dict[str, str]) -> str:
    """A rendered common fragment: exactly one trailing newline is
    stripped — the driver template's own line break terminates the
    fragment's last line."""
    return _strip_one_trailing_newline(_apply(relative, tokens))


def _npm_pin_fragment(package: str, version: str, label: str) -> str:
    """The retry-pinned global npm install preamble (R15). ``PIN`` carries
    the separating ``@`` so ``package@version`` assembles without a bare
    ``@`` between two tokens; the literal ``latest`` resolves to the
    unpinned dist-tag."""
    return _fragment("common/npm-pin.sh", {"PACKAGE": package, "PIN": f"@{version}", "CLI": label})


def _credential_fragment(env_var: str, dest_dir: str) -> str:
    """The guarded credential landing: ``<env>`` → ``<dir>/auth.json``,
    owner-only; absent env still runs (the driver reports its own auth
    failure)."""
    return _fragment("common/credential-blob.sh", {"ENV_VAR": env_var, "DEST": dest_dir})


def _mcp_fragment(driver: str, servers: dict[str, dict[str, Any]]) -> str:
    """The per-driver MCP config-file provisioning block (ADR-0022)."""
    comment, mkdir_line, dest, render_json = _MCP_TARGETS[driver]
    return _fragment(
        "common/mcp-heredoc.sh",
        {"COMMENT": comment, "MKDIR": mkdir_line, "DEST": dest, "JSON": render_json(servers)},
    )


def render(
    driver: str,
    model: str,
    brief_path: str,
    *,
    events_file: str = ".forge/events.jsonl",
    debug_log: str = ".forge/grok-debug.log",
    mcp_servers: dict | None = None,
    driver_versions: dict[str, str] | None = None,
) -> str:
    """The bash script that provisions and invokes *driver* unattended.

    Thin over the shipped templates: resolves the pin table, validates it
    fail-closed (the pins land in shell commands), builds the token table
    (version pins, the shared ``-p`` prompt pointer, model routing, MCP
    provisioning) and substitutes it into
    ``scripts/drivers/<driver>.sh``. *brief_path* is accepted for the
    harness-entry signature; the prompt itself is the shared SHORT
    pointer (:data:`TASK_PROMPT`) — the brief file carries the contract.

    See :func:`forge.harness_entry.render_driver_script` (the delegate)
    for the full per-driver contract documentation.
    """
    servers = mcp_servers or {}
    pins = dict(DEFAULT_DRIVER_VERSIONS)
    pins.update(driver_versions or {})
    for name, pin in pins.items():
        if not _DRIVER_VERSION_RE.match(pin):
            raise ValueError(
                f"bad driver version pin for {name!r}: {pin!r} "
                "(letters, digits, dot, underscore, dash only)"
            )
    if driver not in _NPM_TARGETS:
        raise ValueError(f"unknown driver {driver!r} (expected one of {', '.join(DRIVERS)})")

    package, label = _NPM_TARGETS[driver]
    # Every token is seeded (absent parts are the empty string); the
    # per-driver table below fills what its template consumes.
    tokens: dict[str, str] = {
        "NPM_PIN": _npm_pin_fragment(package, pins[driver], label),
        "QUOTED_PROMPT": shlex.quote(TASK_PROMPT),
        "EVENTS": shlex.quote(events_file),
        "DEBUG_LOG": shlex.quote(debug_log),
        "MODEL_FLAG": f" --model {shlex.quote(model)}" if model else "",
        "ALLOWED_TOOLS": "",
        "OPENCODE_CONFIG": "",
        "MCP_PROVISION": "",
        "MCP_GRANTS": "",
        "CREDENTIAL": "",
        "CODEX_MODEL_EXPORT": "",
        "OPENCODE_MODEL_EXPORTS": "",
    }

    if driver == "claude-code":
        # A09: MCP grants ride the SAME explicit-comma serialization as
        # the shell rules — one plain rule string per grant, per server
        # (the GitLab contract), never pre-quoted: the whole list is
        # shell-quoted ONCE below, and an inner quote used to ship rules
        # named 'mcp__x__*' WITH the quote characters (unmatchable).
        mcp_rules: list[str] = []
        for name in servers:
            mcp_rules.append(f"mcp__{name}__*")
            mcp_rules.append(f"mcp__{name}")
        tokens["ALLOWED_TOOLS"] = shlex.quote(",".join((*_CLAUDE_TOOL_RULES, *mcp_rules)))
        # claude ALWAYS gets its config file (empty map = strict isolation
        # holds); --permission-mode bypassPermissions, NOT acceptEdits +
        # allowlist: the allowlist whack-a-mole is unfixable in principle
        # (LIVE: three waves — quality gates, pipeline segments like
        # awk/sed, then ANY redirection such as `python3 -m pytest 2>&1`
        # poisoned segment matching). The lane's real security boundary
        # is elsewhere: no write credentials, push FORBIDDEN at the
        # remote, output as an artifact validated by the trusted
        # publisher. The mechanical commit/push deny still applies (deny
        # beats bypass).
        tokens["MCP_PROVISION"] = _mcp_fragment(driver, servers)
    elif driver == "grok-build":
        tokens["CREDENTIAL"] = _credential_fragment("FORGE_GROK_AUTH", "~/.grok")
        tokens["MCP_PROVISION"] = _mcp_fragment(driver, servers) if servers else ""
    elif driver == "opencode":
        tokens["OPENCODE_CONFIG"] = shlex.quote(
            json.dumps(
                {
                    "permission": {
                        "bash": {
                            "git commit *": "deny",
                            "git push *": "deny",
                            "*": "allow",
                        },
                        # "ask"-by-default keys hang a headless run (R5).
                        "external_directory": "allow",
                        "doom_loop": "allow",
                    },
                    # MCP (ADR-0022): schema-translated (http -> remote).
                    **({"mcp": json.loads(for_opencode(servers))} if servers else {}),
                }
            )
        )
    elif driver == "copilot":
        tokens["MCP_PROVISION"] = _mcp_fragment(driver, servers) if servers else ""
        tokens["MCP_GRANTS"] = "".join(f" --allow-tool {shlex.quote(name)}" for name in servers)
    elif driver == "dotnet-lane":
        # R28-21: the .NET lane's AGENT is the claude CLI — same grants,
        # same strict MCP posture as the claude-code arm (plus the dotnet
        # tool rules) — and its template appends the reproducible .NET
        # verification tail (locked restore/build, TRX tests).
        mcp_rules: list[str] = []
        for name in servers:
            mcp_rules.append(f"mcp__{name}__*")
            mcp_rules.append(f"mcp__{name}")
        tokens["ALLOWED_TOOLS"] = shlex.quote(
            ",".join((*_CLAUDE_TOOL_RULES, *_DOTNET_TOOL_RULES, *mcp_rules))
        )
        tokens["MCP_PROVISION"] = _mcp_fragment("claude-code", servers)
    elif driver == "claude-sdk-lane":
        pass  # npm pin + handover only — the template is fully static
    elif driver == "codex-sdk-lane":
        tokens["CREDENTIAL"] = _credential_fragment("FORGE_CODEX_AUTH", "~/.codex")
        tokens["CODEX_MODEL_EXPORT"] = f"export CODEX_MODEL={shlex.quote(model)}\n" if model else ""
    elif driver == "opencode-sdk-lane":
        tokens["OPENCODE_CONFIG"] = shlex.quote(
            json.dumps(
                {
                    "permission": {
                        "bash": {
                            "git commit *": "deny",
                            "git push *": "deny",
                            "*": "allow",
                        },
                        # "ask"-by-default keys hang a headless run (R5).
                        "external_directory": "allow",
                        "doom_loop": "allow",
                    },
                }
            )
        )
        # A "provider/model" route splits; a bare model is the model id
        # (the provider stays whatever the ambient env configured).
        # LIVE-found (factory default): a "reject" permission response
        # starves every tool call in a task lane — the template answers
        # "once".
        if model and "/" in model:
            provider_id, _, model_id = model.partition("/")
            tokens["OPENCODE_MODEL_EXPORTS"] = (
                f"export OPENCODE_PROVIDER_ID={shlex.quote(provider_id)}\n" if provider_id else ""
            ) + (f"export OPENCODE_MODEL_ID={shlex.quote(model_id)}\n" if model_id else "")
        elif model:
            tokens["OPENCODE_MODEL_EXPORTS"] = f"export OPENCODE_MODEL_ID={shlex.quote(model)}\n"
    elif driver == "copilot-sdk-lane":
        # npm pin + handover only — the template is fully static. Auth
        # rides the ambient COPILOT_GITHUB_TOKEN (the ACP child reads the
        # env itself); the mechanical commit/push posture lives in the
        # driver client's spawn flags, not in this script.
        pass

    return _strip_one_trailing_newline(_apply(f"drivers/{driver}.sh", tokens))
