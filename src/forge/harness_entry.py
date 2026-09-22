"""forge harness entry point — the Actions lane's driver runner (Stage E3b).

The "Run harness driver" step of ``ci/templates/forge-harness.github.yml``
installs forge from a pinned ref and runs ``python -m forge.harness_entry``.
This module is the ENTIRE forge surface inside the ephemeral runner:

- it renders the SHARED implementation brief
  (:func:`forge.harnesses.prompt.render_brief` — one prompt builder for
  both the Actions and the GitLab lanes) into ``.forge/brief.md``; the
  brief file the agent reads IS the prompt, and the per-CLI ``-p``
  invocation stays the short pointer :data:`forge.harnesses.prompt.TASK_PROMPT`;
- it renders the per-driver invocation (claude-code | grok-build |
  opencode | copilot | codex-sdk-lane | opencode-sdk-lane)
  from the SAME contract the GitLab templates implement
  (``ci/templates/*.gitlab-ci.yml``; interface ground truth:
  ``docs/research/2026-09-13-harness-interfaces.md``) — per-CLI FLAGS live here, the
  PROMPT is shared; the two SDK-lane ids provision their CLI and hand
  over to ``forge.lane_driver`` (the interactive driver twin of the
  scripted ``-p`` calls — no prompt pointer, no event tee, the lane
  runner writes the meta/usage receipts itself);
- it runs the driver unattended (proposal-only: the agent is told to leave
  its changes in the working tree — it cannot commit or push, the lane has
  no write credential);
- it aggregates the driver's usage receipts into ``.forge/usage.json``
  (unknown stays unknown, never zero — F22 lite) and writes ``.forge/exit``
  (``completed`` | ``failed``), decoupled from the agent's own output so
  the workflow can still upload the candidate artifact ``if: always()``;
- it builds the candidate meta artifact (``--emit-meta``, R16/R23): schema
  v2 identity + a sha256 digest binding the meta to the staged diff bytes
  + the usage receipt inlined, so the control plane gets identity,
  integrity, and spend with the candidate — written into the non-hidden
  ``forge-output/`` staging directory the workflow uploads
  (upload-artifact@v4 excludes hidden files by default and drops
  dot-directories before traversal — A08). A18 adds two additive audit
  fields: ``bootstrap`` (the lane's environment-bootstrap classification
  from ``.forge/bootstrap`` — a FAILED bootstrap is infrastructure/config,
  never code repair) and ``profile_digest`` (the execution profile derived
  from THIS checkout — :mod:`forge.runs.execution_profile` — the executed
  twin of the digest frozen into the approved spec).

Brief transport (the dispatch-input size question, decided): the lane
FETCHES its own brief content from the forge API — the approved plan is
ALREADY posted as the plan comment (E3a/AZ-2 posted it), so
``--render-brief`` reads the approved bytes + that comment with the
runner's read-only ``GITHUB_TOKEN`` (:func:`fetch_issue_context`), and
``--render-brief-azure`` reads the Azure DevOps work item + its plan
comment with a repo-owner-provisioned read-only token
(:func:`fetch_workitem`, FORGE_AZDO_READ_TOKEN — never forge's PAT). No
dispatch input ever carries plan text, so no input size limit binds.
R05 interim: the control plane journals the plan comment's id when it
posts it and dispatches it as the ``plan_note_id`` input — the lane then
fetches EXACTLY that comment (:envvar:`FORGE_PLAN_NOTE_ID`, validated
fail-closed) instead of heuristically scanning the thread.
A03 (approved bytes, not current bytes): identity alone was not enough —
the comment body could be edited after ``/go`` and the issue body was read
LIVE. The control plane now freezes a BriefEnvelope at approval (the task
title/description + plan text, each sha256-digested, bound by an
``envelope_digest`` over run_id + the bytes + the spec digest) and
dispatches it as ``envelope_digest`` + ``spec_digest``. The enforced lane
extracts the APPROVED sections from the bound comment
(:func:`forge.harnesses.brief_envelope.extract_approved_sections`),
re-computes the digest over exactly those bytes and FAILS CLOSED on
mismatch ("approved brief bytes changed after approval (re-approval
required)"). The live issue is never read for the task text on the
enforced path — the brief renders the digest-verified frozen bytes. Only
when the envelope inputs are absent (legacy replay / other repos) does the
old live-read posture run, loudly unenforced.
Alternatives kept for parity: a pre-provisioned ``.forge/brief.md``
works as-is (the workflow owner may ship it however they like).

Driver versions (R15): the driver CLIs install PINNED, never off the
moving npm dist-tag — a CLI release can silently change lane behavior
(flags, permission semantics, event schema) between the plan gate and
the run. ``FORGE_DRIVER_VERSIONS`` (JSON object driver → version; the
workflow template passes the same-named repo VARIABLE through) overrides
:data:`DEFAULT_DRIVER_VERSIONS` per driver, and the literal ``"latest"``
keeps the unpinned install. Every preamble echoes the installed
``<cli> --version`` into the job log either way, so pin drift is visible,
never silent.

Lane credentials are capability/credential PAIRS (BYOK note): a
provider-native subscription (a Claude seat, a Grok coding plan, a
Copilot seat) is NOT an interchangeable API key — each driver consumes
exactly the credential its own unattended contract names (claude-code:
``ANTHROPIC_*``/ZAI via the Anthropic-compatible coding endpoint,
grok-build: ``FORGE_GROK_AUTH`` written to ``~/.grok/auth.json``,
opencode: ``ZAI_API_KEY``, copilot: ``COPILOT_GITHUB_TOKEN``), and the
workflow template renders only the SELECTED driver's secrets. The
enforcement point is the env gating in the template, nothing deeper:
swapping a subscription for an API key is a configuration decision the
lane never detects or polices.

Stdlib + the pure prompt builder only: no forge database, no forge
credentials — the lane runs forge's CODE, never forge's state.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
import re
import shlex
import subprocess
from typing import Any
import sys
from html import unescape
from pathlib import Path

from forge.harnesses.prompt import (
    TASK_PROMPT,
    BriefContext,
    BriefPolicy,
    render_brief as render_shared_brief,
)
from forge.harnesses.brief_envelope import (
    BriefEnvelopeError,
    extract_approved_sections,
    verify_brief_envelope,
)
from forge.harnesses.mcp import (
    McpConfigError,
    for_claude,
    for_copilot,
    for_grok,
    for_opencode,
    parse_servers,
)

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
# never match) — never concatenate rule literals again.
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
# Serialized ONLY at the render site via the explicit
# ",".join((*_CLAUDE_TOOL_RULES, *mcp_rules)) — never by literal
# concatenation (A09).
#: The SDK-lane drivers: the agent is driven by ``forge.lane_driver``
#: (the REAL interactive driver clients), not a scripted ``-p`` call —
#: the rendered script only provisions the CLI and hands the lane over
#: (the Actions/AzDO mirror of the codex/opencode GitLab sdk-lane
#: templates).
LANE_DRIVERS = ("claude-sdk-lane", "codex-sdk-lane", "opencode-sdk-lane")

#: The SCRIPTED drivers: a rendered one-shot CLI invocation with the
#: shared ``-p`` prompt pointer and the tee'd event stream.
SCRIPTED_DRIVERS = ("claude-code", "grok-build", "opencode", "copilot")

#: Drivers understood by this entry point (the shipped multi-harness set):
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
}

#: A version/dist-tag token safe to splice into an npm install spec
#: (semver, dist-tags like ``latest``). Anything else is refused — the
#: pin lands in a shell command and must never carry metacharacters.
_DRIVER_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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


def _npm_pin(package: str, version: str) -> str:
    """One pinned global npm install (R15): ``package@version`` — the
    literal ``latest`` version resolves to the unpinned dist-tag."""
    return f"npm install -g --no-fund --no-audit {package}@{version}"


def render_brief(issue_text: str, plan_text: str) -> str:
    """Render the quality brief via the SHARED builder (single source).

    Thin adapter over :func:`forge.harnesses.prompt.render_brief` in the
    proposal-only ``ci_lane`` output contract; *issue_text* is the issue
    body snapshot, *plan_text* the approved plan (verbatim). Conventions
    files (AGENTS.md / CLAUDE.md) are detected in the current working
    directory — the checkout the agent will work in. When the ADR-0022
    ``FORGE_HARNESS_MCP`` variable carries the ``codegraph`` server, the
    brief also directs the agent to the code-graph tools (single source of
    truth: the same variable that provisions the server in the driver).
    """
    try:
        mcp_names = set(parse_servers(os.environ.get("FORGE_HARNESS_MCP")))
    except McpConfigError:
        mcp_names = set()
    return render_shared_brief(
        BriefContext(
            plan=plan_text,
            issue_body=issue_text,
            driver=os.environ.get("FORGE_DRIVER", ""),
            model=os.environ.get("FORGE_HARNESS_MODEL", ""),
        ),
        lane="ci_lane",
        repo_root=Path.cwd(),
        policy=BriefPolicy(codegraph="codegraph" in mcp_names),
    )


#: The forge App bot logins a plan comment may carry (GitHub App bot logins
#: render with a ``[bot]`` suffix; comparison strips it, case-insensitively).
#: Used by the legacy scan and as the tamper-guard default when
#: ``FORGE_GITHUB_BOT_LOGIN`` is not configured.
_FORGE_BOT_LOGINS = ("forge", "forcewake-forge")

#: The plan header every forge plan comment carries
#: (``GitHubRunService._plan_comment`` / ``ForgeRunService`` equivalent).
_PLAN_HEADER = "## Forge plan"


class PlanBindingError(Exception):
    """A bound plan comment failed validation — the lane refuses the brief.

    Fail-closed (R05): when the lane addresses the EXACT plan comment by id,
    anything unexpected about that comment (missing, wrong author, wrong
    run) aborts the brief render with a non-zero exit instead of silently
    binding some other comment's text. Fail-closed on CONTENT too (A03):
    when the dispatch carries the approved BriefEnvelope digest, an edited
    comment body (or a tampered digest) fails the same way — the lane
    executes the approved bytes, never the comment's current ones.
    """


def _normalize_login(login: str) -> str:
    """Lower-cased login with GitHub's ``[bot]`` App suffix stripped."""
    return (login or "").strip().lower().removesuffix("[bot]")


def _author_is_forge(author: str, expected_login: str = "") -> bool:
    """Whether *author* is an acceptable forge bot identity.

    With *expected_login* set (``FORGE_GITHUB_BOT_LOGIN``) only that login
    passes; without it, the shipped App logins (:data:`_FORGE_BOT_LOGINS`)
    pass. Tamper-guard use only — see :func:`validate_plan_comment`.
    """
    left = _normalize_login(author)
    if not left:
        return False
    if expected_login:
        return left == _normalize_login(expected_login)
    return left in _FORGE_BOT_LOGINS


def validate_plan_comment(comment: dict, *, run_id: str = "", expected_login: str = "") -> str:
    """Validate a BOUND plan comment and return its body (fail-closed).

    R05 interim transport: the control plane journals the plan comment's id
    when it posts the approved plan and dispatches it as ``plan_note_id``;
    the lane fetches EXACTLY that comment, so the BINDING comes from the
    addressed id — never from identity or substring discovery. The checks
    here are tamper guards on top of that binding:

    - the author login must be the forge bot (``FORGE_GITHUB_BOT_LOGIN``
      when configured, else the shipped App logins) — a substituted comment
      id must not silently bind content another author posted;
    - the body must carry the ``## Forge plan`` header;
    - when the lane knows its forge run id (the ``run_id`` dispatch input,
      ``FORGE_RUN_ID``), the body must contain that exact id — another
      run's plan can never ride this lane's brief.

    Any violation raises :class:`PlanBindingError` (the caller exits
    non-zero); the validated body is returned verbatim.
    """
    body = str(comment.get("body") or "")
    author = str((comment.get("user") or {}).get("login") or "")
    if not _author_is_forge(author, expected_login):
        expected = expected_login or " | ".join(_FORGE_BOT_LOGINS)
        raise PlanBindingError(
            f"plan comment {comment.get('id')} author @{author or '<none>'} is not the "
            f"forge bot identity (@{expected}) — refusing to bind the brief"
        )
    if _PLAN_HEADER not in body:
        raise PlanBindingError(
            f"plan comment {comment.get('id')} carries no {_PLAN_HEADER!r} header — "
            "refusing to bind the brief"
        )
    if run_id and run_id not in body:
        raise PlanBindingError(
            f"plan comment {comment.get('id')} does not mention run {run_id[:8]} — "
            "refusing to bind another run's plan"
        )
    return body


def _github_get_json(url: str, token: str) -> Any:
    """One authenticated GET against the GitHub REST API (stdlib only)."""
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "forge-harness-entry",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def fetch_plan_comment(repo: str, note_id: int, token: str) -> dict:
    """Fetch the EXACT comment ``/repos/{owner}/{repo}/issues/comments/{id}``.

    The bound transport's only plan source: no thread listing, no scanning,
    no identity heuristic — GitHub returns the one comment the control
    plane journaled. Network/API failures raise (urllib ``OSError``
    family) and fail the lane closed.
    """
    return _github_get_json(f"https://api.github.com/repos/{repo}/issues/comments/{note_id}", token)


def fetch_issue_context(
    repo: str,
    issue_number: int,
    token: str,
    *,
    plan_note_id: int = 0,
    run_id: str = "",
    envelope_digest: str = "",
    spec_digest: str = "",
) -> tuple[str, str]:
    """Fetch (task text, forge plan comment bytes) for the brief render.

    Uses only the stdlib: the lane runs forge's code without forge's
    dependencies.

    ENFORCED path (A03 — *plan_note_id* + *envelope_digest* + *spec_digest*
    all dispatched): the plan is EXACTLY the addressed comment
    (:func:`fetch_plan_comment`, tamper guards via
    :func:`validate_plan_comment`), the approved task + plan sections are
    extracted from its body
    (:func:`forge.harnesses.brief_envelope.extract_approved_sections`) and
    re-verified against the dispatched envelope digest
    (:func:`forge.harnesses.brief_envelope.verify_brief_envelope`) — any
    mismatch raises :class:`PlanBindingError` ("approved brief bytes changed
    after approval (re-approval required)"). The returned task text is the
    frozen title + description from the verified sections; the LIVE issue
    is never fetched — an issue edit after approval cannot touch the brief.

    LEGACY path (any envelope input absent): the pre-A03 posture — the plan
    comes from the bound comment (validated, fail-closed) when
    *plan_note_id* is present, else from the loud unenforced heuristic scan;
    the task text is the LIVE issue body. The caller logs that the envelope
    binding is NOT enforced.
    """
    if plan_note_id and envelope_digest and spec_digest:
        body = validate_plan_comment(
            fetch_plan_comment(repo, plan_note_id, token),
            run_id=run_id,
            # The tamper-guard identity: the repo's configured forge App
            # login (the shipped App logins apply when unset).
            expected_login=os.environ.get("FORGE_GITHUB_BOT_LOGIN", "").strip(),
        )
        try:
            task_title, task_description, plan = extract_approved_sections(body)
            verify_brief_envelope(
                envelope_digest,
                run_id=run_id,
                task_title=task_title,
                task_description=task_description,
                plan_text=plan,
                spec_digest=spec_digest,
            )
        except BriefEnvelopeError as exc:
            raise PlanBindingError(str(exc)) from exc
        return f"{task_title}\n{task_description}", plan
    issue = _github_get_json(f"https://api.github.com/repos/{repo}/issues/{issue_number}", token)
    body = str(issue.get("body") or "")
    if plan_note_id:
        return body, validate_plan_comment(
            fetch_plan_comment(repo, plan_note_id, token),
            run_id=run_id,
            expected_login=os.environ.get("FORGE_GITHUB_BOT_LOGIN", "").strip(),
        )
    comments = _github_get_json(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments?per_page=100",
        token,
    )
    plan_text = ""
    for comment in reversed(comments if isinstance(comments, list) else []):
        author = (comment.get("user") or {}).get("login", "")
        text = str(comment.get("body") or "")
        if _author_is_forge(author) and "plan" in text.lower():
            plan_text = text
            break
    return body, plan_text


#: Work items read at GA 7.1; WIT comments only exist as the
#: ``7.1-preview.4`` stripe (docs/research/2026-09-15-azure-devops.md §1.3/§5.1).
_AZDO_WIT_API_VERSION = "7.1"
_AZDO_COMMENTS_API_VERSION = "7.1-preview.4"

_HTML_TAG_RE = re.compile(r"<[^>]*>")


def _strip_html(html: str) -> str:
    """Defensive HTML → text: tags out, entities unescaped, whitespace
    collapsed (work-item fields are HTML; it never belongs in a prompt)."""
    text = _HTML_TAG_RE.sub(" ", html or "")
    return " ".join(unescape(text).split())


def _is_bot_identity(identity: str, bot_name: str) -> bool:
    """Case-insensitive identity match (``uniqueName``, its local part, or a
    bare display name) — the same tolerance the gateway's bot-loop guard
    uses for AzDO identities."""
    left = (identity or "").strip().lower()
    bot = (bot_name or "").strip().lower()
    if not left or not bot:
        return False
    return left == bot or left.split("@", 1)[0] == bot


def fetch_workitem(
    org_url: str,
    project: str,
    work_item_id: int,
    token: str,
    *,
    bot_name: str = "forge-bot",
) -> tuple[str, str]:
    """Fetch (work-item body, forge plan comment) from Azure DevOps.

    The AzDO brief transport, mirroring :func:`fetch_issue_context`:
    stdlib only (the lane runs forge's code without forge's dependencies),
    Basic auth ``":" + token`` — empty username, colon prefix
    (docs/research/2026-09-15-azure-devops.md §1.2). The work item is read at
    ``api-version=7.1``, its comments at the ``7.1-preview.4`` stripe with
    ``format=markdown`` (the only stripe that family has at 7.1).

    ``System.Description`` is HTML and is stripped defensively; the body
    is the title plus the description (both live in separate fields, unlike
    GitHub's single issue body). The plan comment is the latest comment by
    the bot identity that contains the plan heading — the SAME heuristic
    as :func:`fetch_issue_context`.
    """
    import base64
    import urllib.request

    def _get(url: str) -> Any:
        encoded = base64.b64encode(f":{token}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Basic {encoded}",
                "Accept": "application/json",
                "User-Agent": "forge-harness-entry",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    base = f"{org_url.rstrip('/')}/{project}/_apis/wit/workItems/{work_item_id}"
    item = _get(f"{base}?api-version={_AZDO_WIT_API_VERSION}")
    fields = item.get("fields") if isinstance(item, dict) else {}
    fields = fields if isinstance(fields, dict) else {}
    title = str(fields.get("System.Title") or "")
    description = _strip_html(str(fields.get("System.Description") or ""))
    body = f"{title}\n\n{description}".strip()

    comments = _get(f"{base}/comments?api-version={_AZDO_COMMENTS_API_VERSION}&format=markdown")
    entries = comments.get("comments") if isinstance(comments, dict) else comments
    plan_text = ""
    for comment in reversed(entries if isinstance(entries, list) else []):
        author = comment.get("createdBy") if isinstance(comment, dict) else {}
        author = author if isinstance(author, dict) else {}
        identity = str(author.get("uniqueName") or author.get("displayName") or "")
        text = str(comment.get("text") or "") if isinstance(comment, dict) else ""
        if _is_bot_identity(identity, bot_name) and "plan" in text.lower():
            plan_text = text
            break
    return body, plan_text


def fetch_workitem_comment(
    org_url: str,
    project: str,
    work_item_id: int,
    comment_id: int,
    token: str,
) -> str:
    """Fetch ONE work-item comment by its id — the addressed plan transport.

    B04: the control plane journals the plan comment's ``commentId`` and
    dispatches it as ``plan_note_id``; the lane reads EXACTLY that comment
    (stdlib only, same Basic-auth shape as :func:`fetch_workitem`).
    """
    import base64
    import urllib.request

    encoded = base64.b64encode(f":{token}".encode("utf-8")).decode("ascii")
    url = (
        f"{org_url.rstrip('/')}/{project}/_apis/wit/workItems/{work_item_id}"
        f"/comments/{comment_id}"
        f"?api-version={_AZDO_COMMENTS_API_VERSION}&format=markdown"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "User-Agent": "forge-harness-entry",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read())
    if not isinstance(data, dict):
        raise PlanBindingError(f"plan comment {comment_id}: unexpected payload shape")
    return str(data.get("text") or "")


def render_driver_script(
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

    *mcp_servers* (parsed ``FORGE_HARNESS_MCP``) is rendered per driver per
    :mod:`forge.harnesses.mcp`: claude always runs with
    ``--mcp-config <file> --strict-mcp-config`` (empty map = only the
    repo's own MCP configs are locked out — injection-surface reduction);
    grok/copilot get their config files written; opencode's servers ride
    in the same ``OPENCODE_CONFIG_CONTENT`` as the permission map.

    *driver_versions* (resolved ``FORGE_DRIVER_VERSIONS``,
    :func:`resolve_driver_versions`) pins the npm installs: each preamble
    installs ``package@<version>`` and then echoes the installed
    ``<cli> --version`` into the job log (R15: pin drift is visible, never
    silent). The literal ``latest`` keeps the unpinned dist-tag install.

    Rendered per driver from the GitLab templates' contract:

    - ``claude-code`` — headless print mode, stream-json events, no external
      settings (prompt-injection surface reduction), bypassPermissions PLUS
      the R5 mechanical deny: commit/push are ``--disallowedTools`` (holds
      even under bypass — deny beats every permission mode; the lane's real
      boundary is no write credentials + push FORBIDDEN + trusted publisher)
      ``--permission-prompts none`` guarantees no interactive prompt, and
      the vendor timeout budgets keep long tool calls from dying mid-run;
    - ``grok-build`` — the hardened npm preamble first: the wrapper declares
      its platform binary as an optionalDependency, so a flaky registry
      silently skips it and the CLI hangs forever at startup. Both packages
      are installed explicitly, with retries (verified live, template
      comment); the platform binary follows the PINNED wrapper (the version
      is read back from the binary just installed). The lane's ONLY grok
      credential is the provider-native subscription auth blob
      (``FORGE_GROK_AUTH`` → ``~/.grok/auth.json``, the GitLab template
      contract) — an API key is a different capability and is never
      requested here (R15 BYOK: capability/credential pairs).
      ``--always-approve`` is required or headless grok HANGS.
      ``--trust`` loads project rules headlessly and ``--deny`` rules beat
      always-approve (R5: commit/push denied mechanically, not just asked);
    - ``opencode`` — ``run --auto`` (approves what the permission config
      does not deny; the ephemeral runner is the execution profile). The
      permission map rides in via ``OPENCODE_CONFIG_CONTENT``: commit/push
      denied mechanically, and the two "ask"-by-default keys
      (``external_directory``, ``doom_loop``) are allowed so the headless
      run cannot hang on a prompt (R5). The model is config-owned here, not
      CLI-owned (mirror of the GitLab template, which routes it through
      opencode.json).
    - ``copilot`` — GitHub Copilot CLI in ``-p`` mode (completes and exits):
      scoped grants (``read,write`` + ``shell(git:*)``) so nothing else can
      prompt, and the mechanical ``--deny-tool`` on commit/push — documented
      Copilot rule: deny beats every allow, including ``--allow-all``.
      Auth rides on ``COPILOT_GITHUB_TOKEN`` (fine-grained PAT with the
      "Copilot Requests" permission); no parseable usage receipt, unknown
      stays unknown.

    The SDK-lane drivers (:data:`LANE_DRIVERS`) render no ``-p``
    invocation at all — the agent is driven by forge's own interactive
    lane runner, so the script only provisions the CLI and hands over
    (the npm preamble + version echo still apply; MCP servers are NOT
    consumed on this path — the driver clients provision none):

    - ``codex-sdk-lane`` — npm ``@openai/codex`` (the lane runner spawns
      ``codex app-server``); the OPTIONAL provider-native ChatGPT-login
      blob (``FORGE_CODEX_AUTH`` → ``~/.codex/auth.json``, the grok
      lane's posture) beside the inherited ``OPENAI_API_KEY`` /
      ``CODEX_API_KEY`` env; model routed via ``CODEX_MODEL``, the
      sandbox is the workspace-write + approvalPolicy-never recipe the
      driver factory pins.
    - ``opencode-sdk-lane`` — npm ``opencode-ai``; the mechanical
      commit/push deny + headless-hang allows ride via
      ``OPENCODE_CONFIG_CONTENT`` (the serve child reads it), permission
      prompts are answered ``once`` (LIVE-found: ``reject`` starves every
      tool call in a task lane), and a ``provider/model`` route splits
      into ``OPENCODE_PROVIDER_ID``/``OPENCODE_MODEL_ID``.

    For the scripted drivers, the ``-p`` prompt is the shared SHORT
    pointer (:data:`TASK_PROMPT`) — the brief file at *brief_path*
    carries the whole contract. The script streams the driver's
    normalized event log into the job log AND tees it to *events_file*;
    ``pipefail`` keeps the driver's exit code so a nonzero agent exit
    classifies the run as failed WITHOUT aborting the audit trail (the
    workflow uploads artifacts ``if: always()``). The SDK lanes need no
    tee — ``forge.lane_driver`` writes the meta + usage receipts itself,
    and a nonzero exit classifies the run exactly the same way.
    """
    quoted_prompt = shlex.quote(TASK_PROMPT)
    events = shlex.quote(events_file)
    servers = mcp_servers or {}
    pins = dict(DEFAULT_DRIVER_VERSIONS)
    pins.update(driver_versions or {})
    for name, pin in pins.items():
        if not _DRIVER_VERSION_RE.match(pin):
            raise ValueError(
                f"bad driver version pin for {name!r}: {pin!r} "
                "(letters, digits, dot, underscore, dash only)"
            )

    if driver == "claude-code":
        preamble = (
            "for attempt in 1 2 3; do\n"
            f"  {_npm_pin('@anthropic-ai/claude-code', pins['claude-code'])} && break\n"
            '  echo "npm install of claude-code failed (attempt $attempt), retrying..."\n'
            "  sleep $((attempt * 5))\n"
            "done\n"
            "# R15: the resolved CLI version lands in the job log — pin\n"
            "# drift is visible, never silent.\n"
            "claude --version\n"
        )
        mcp_file = "/tmp/forge-mcp.json"
        mcp_provision = (
            "# MCP (ADR-0022): config ONLY from the CI variable — strict mode\n"
            "# locks out the repo's own .mcp.json (injection surface).\n"
            f"cat > {shlex.quote(mcp_file)} <<'FORGE_MCP_EOF'\n"
            f"{for_claude(servers)}\n"
            "FORGE_MCP_EOF\n"
        )
        # A09: MCP grants ride the SAME explicit-comma serialization as the
        # shell rules — one plain rule string per grant, per server (the
        # GitLab contract), never pre-quoted: the whole list is shell-
        # quoted ONCE below, and an inner quote used to ship rules named
        # 'mcp__x__*' WITH the quote characters (unmatchable).
        mcp_rules: list[str] = []
        for name in servers:
            mcp_rules.append(f"mcp__{name}__*")
            mcp_rules.append(f"mcp__{name}")
        allowed_tools = ",".join((*_CLAUDE_TOOL_RULES, *mcp_rules))
        model_flag = f" --model {shlex.quote(model)}" if model else ""
        invocation = (
            f"claude -p {quoted_prompt}{model_flag} \\\n"
            f"  --allowedTools {shlex.quote(allowed_tools)} \\\n"
            '  --disallowedTools "Bash(git commit:*)" "Bash(git push:*)" \\\n'
            "  --permission-prompts none \\\n"
            # bypassPermissions, NOT acceptEdits + allowlist: the allowlist
            # whack-a-mole is unfixable in principle (LIVE: three waves —
            # quality gates, pipeline segments like awk/sed, then ANY
            # redirection such as `python3 -m pytest 2>&1` poisoned segment
            # matching). The lane's real security boundary is elsewhere:
            # no write credentials, push FORBIDDEN at the remote, output as
            # an artifact validated by the trusted publisher. The mechanical
            # commit/push deny still applies (deny beats bypass).
            "  --permission-mode bypassPermissions \\\n"
            "  --max-turns 200 \\\n"
            f"  --mcp-config {shlex.quote(mcp_file)} --strict-mcp-config \\\n"
            "  --setting-sources '' --output-format stream-json --verbose 2>&1"
        )
        return (
            "# Vendor timeout budgets (R5): long tool calls and API turns\n"
            "# must not die at the client default mid-run.\n"
            "export API_TIMEOUT_MS=3000000 BASH_DEFAULT_TIMEOUT_MS=300000"
            " BASH_MAX_TIMEOUT_MS=600000\n"
            "# Ephemeral isolated config: claude's auto-memory is pointless in\n"
            "# a proposal-only lane (the brief IS this run's memory) and\n"
            "# HAZARDOUS on reused runners — the MEMORY.md index auto-loads into\n"
            "# context, so a stale index from ANOTHER run on the same VM would\n"
            "# poison this run (LIVE-found: the agent wrote memory/MEMORY.md).\n"
            "# A fresh config dir per lane guarantees a cold start; lane auth\n"
            "# rides on env vars, so nothing stored is lost.\n"
            'export CLAUDE_CONFIG_DIR="$(mktemp -d /tmp/claude-lane-config.XXXXXX)"\n'
            "# Repair re-dispatches are GUIDED fixes (bounded failure context\n"
            "# rides in the brief) — deep per-turn thinking is the lane's\n"
            "# dominant wall-time cost (LIVE: 17% of turns >40s ≈ half the\n"
            "# run), so repair cycles cap the thinking budget. First cycles\n"
            "# think freely.\n"
            'if [ -n "$FORGE_REPAIR_CONTEXT" ]; then\n'
            '  export MAX_THINKING_TOKENS="${FORGE_MAX_THINKING_TOKENS:-8000}"\n'
            "fi\n"
            + mcp_provision
            + preamble
            + f"{invocation} | tee -a {events} | $FORGE_FILTER_PIPE"
        )

    if driver == "grok-build":
        preamble = (
            "for attempt in 1 2 3; do\n"
            f"  {_npm_pin('@xai-official/grok', pins['grok-build'])} && break\n"
            '  echo "npm install of grok failed (attempt $attempt), retrying..."\n'
            "  sleep $((attempt * 5))\n"
            "done\n"
            "# The platform binary follows the PINNED wrapper: GROK_VER is\n"
            "# read back from the binary just installed (a pin on the\n"
            "# wrapper alone would leave the optionalDependency floating).\n"
            "GROK_VER=\"$(grok --version | awk '{print $2}')\"\n"
            'npm install -g --no-fund --no-audit "@xai-official/grok-linux-x64@${GROK_VER}" \\\n'
            "  || npm install -g --no-fund --no-audit @xai-official/grok-linux-x64\n"
            "test -d /usr/local/lib/node_modules/@xai-official/grok-linux-x64\n"
            "# R15: the resolved CLI version lands in the job log — pin\n"
            "# drift is visible, never silent.\n"
            "grok --version\n"
        )
        credential = (
            "# R15 minimal lane credentials: the ONLY grok credential is the\n"
            "# provider-native subscription auth blob (FORGE_GROK_AUTH — the\n"
            "# full ~/.grok/auth.json contents, the GitLab template\n"
            "# contract). An API key is a different capability and is never\n"
            "# requested here (BYOK is per capability/credential pair, not\n"
            "# interchangeable).\n"
            "mkdir -p ~/.grok\n"
            'if [ -n "$FORGE_GROK_AUTH" ]; then\n'
            '  printf "%s" "$FORGE_GROK_AUTH" > ~/.grok/auth.json\n'
            "  chmod 600 ~/.grok/auth.json\n"
            "fi\n"
        )
        mcp_provision = (
            (
                "# MCP (ADR-0022): claude-shaped mcpServers in Grok's settings.\n"
                "mkdir -p ~/.grok\n"
                "cat > ~/.grok/settings.json <<'FORGE_MCP_EOF'\n"
                f"{for_grok(servers)}\n"
                "FORGE_MCP_EOF\n"
            )
            if servers
            else ""
        )
        invocation = (
            "grok --no-auto-update --always-approve --no-alt-screen \\\n"
            "  --trust --max-turns 200 \\\n"
            "  --allow 'Bash(uv run pytest:*)' --allow 'Bash(pytest:*)' \\\n"
            "  --allow 'Bash(uv run ruff:*)' --allow 'Bash(uv run mypy:*)' \\\n"
            "  --allow 'Bash(python3:*)' --allow 'Bash(python:*)' \\\n"
            "  --allow 'Bash(pip install:*)' \\\n"
            # The repo's own quality gates (LIVE-found: make lint / bare
            # ruff / venv python were denied and burned turns):
            "  --allow 'Bash(uv:*)' --allow 'Bash(make:*)' --allow 'Bash(set:*)' \\\n"
            "  --allow 'Bash(ruff:*)' --allow 'Bash(mypy:*)' \\\n"
            "  --allow 'Bash(.venv/bin/python:*)' --allow 'Bash(.venv/bin/ruff:*)' \\\n"
            "  --allow 'Bash(awk:*)' --allow 'Bash(sed:*)' --allow 'Bash(sort:*)' \\\n"
            "  --allow 'Bash(cut:*)' --allow 'Bash(tr:*)' --allow 'Bash(find:*)' \\\n"
            "  --deny 'Bash(git commit:*)' --deny 'Bash(git push:*)' \\\n"
            "  --output-format streaming-json \\\n"
            f"  --debug-file {shlex.quote(debug_log)} \\\n"
            f"  -p {quoted_prompt} 2>&1"
        )
        return (
            preamble
            + credential
            + mcp_provision
            + f"{invocation} | tee -a {events} | $FORGE_FILTER_PIPE"
        )

    if driver == "opencode":
        preamble = (
            "for attempt in 1 2 3; do\n"
            f"  {_npm_pin('opencode-ai', pins['opencode'])} && break\n"
            '  echo "npm install of opencode failed (attempt $attempt), retrying..."\n'
            "  sleep $((attempt * 5))\n"
            "done\n"
            "# R15: the resolved CLI version lands in the job log — pin\n"
            "# drift is visible, never silent.\n"
            "opencode --version\n"
        )
        permission_config = json.dumps(
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
        invocation = f"opencode run --auto --format json {quoted_prompt} 2>&1"
        return (
            preamble + "# The mechanical deny rides in via the documented\n"
            "# config-injection env (merges over global/project config).\n"
            f"export OPENCODE_CONFIG_CONTENT={shlex.quote(permission_config)}\n"
            f"{invocation} | tee -a {events} | $FORGE_FILTER_PIPE"
        )

    if driver == "copilot":
        preamble = (
            "for attempt in 1 2 3; do\n"
            f"  {_npm_pin('@github/copilot', pins['copilot'])} && break\n"
            '  echo "npm install of copilot failed (attempt $attempt), retrying..."\n'
            "  sleep $((attempt * 5))\n"
            "done\n"
            "# R15: the resolved CLI version lands in the job log — pin\n"
            "# drift is visible, never silent.\n"
            "copilot --version\n"
        )
        mcp_provision = (
            (
                "# MCP (ADR-0022): documented Copilot CLI config location.\n"
                "mkdir -p ~/.copilot\n"
                "cat > ~/.copilot/mcp-config.json <<'FORGE_MCP_EOF'\n"
                f"{for_copilot(servers)}\n"
                "FORGE_MCP_EOF\n"
            )
            if servers
            else ""
        )
        mcp_grants = "".join(f" --allow-tool {shlex.quote(name)}" for name in servers)
        model_flag = f" --model {shlex.quote(model)}" if model else ""
        invocation = (
            f"copilot -p {quoted_prompt}{model_flag} \\\n"
            "  --allow-tool 'read,write' \\\n"
            "  --allow-tool 'shell(git:*)' \\\n"
            "  --allow-tool 'shell(uv run pytest:*)' \\\n"
            "  --allow-tool 'shell(pytest:*)' \\\n"
            "  --allow-tool 'shell(uv run ruff:*)' \\\n"
            "  --allow-tool 'shell(uv run mypy:*)' \\\n"
            "  --allow-tool 'shell(uv:*)' --allow-tool 'shell(make:*)' \\\n"
            "  --allow-tool 'shell(set:*)' --allow-tool 'shell(ruff:*)' \\\n"
            "  --allow-tool 'shell(mypy:*)' \\\n"
            "  --allow-tool 'shell(awk:*)' --allow-tool 'shell(sed:*)' \\\n"
            "  --allow-tool 'shell(sort:*)' --allow-tool 'shell(cut:*)' \\\n"
            f"{mcp_grants}"
            "  --deny-tool 'shell(git commit)' --deny-tool 'shell(git push)' 2>&1"
        )
        return preamble + mcp_provision + f"{invocation} | tee -a {events} | $FORGE_FILTER_PIPE"

    if driver == "claude-sdk-lane":
        preamble = (
            "for attempt in 1 2 3; do\n"
            f"  {_npm_pin('@anthropic-ai/claude-code', pins['claude-sdk-lane'])} && break\n"
            '  echo "npm install of claude-code failed (attempt $attempt), retrying..."\n'
            "  sleep $((attempt * 5))\n"
            "done\n"
            "claude --version\n"
            "# The bootstrap installs forge WITHOUT extras; the claude lane\n"
            "# needs the interactive extra's SDK (LIVE-found on the Actions\n"
            "# runner: sdk_missing with a green bootstrap).\n"
            "pip install --quiet 'claude-agent-sdk>=0.2.118'\n"
            "# GitLab docker executors run as root; claude refuses the bypass\n"
            "# posture for root unless told it is sandboxed (ADR-0002).\n"
            'export IS_SANDBOX="${IS_SANDBOX:-1}"\n'
            "# NXT-10 outbound leg: the steering attach dials the control\n"
            "# plane when the dispatch carried the pair — the work-scoped\n"
            "# lane token rides the job env (never a control-plane secret);\n"
            "# exported empty when unset so the lane honestly stays local.\n"
            'export FORGE_LANE_CONTROL_URL="${FORGE_LANE_CONTROL_URL:-}"\n'
            'export FORGE_LANE_CONTROL_TOKEN="${FORGE_LANE_CONTROL_TOKEN:-}"\n'
        )
        # The SDK lane drives the REAL claude-agent-sdk client (EXE-02):
        # gateway env rides the ambient environment (the driver merges
        # options.env over it); the runner writes the candidate artifacts
        # itself; a nonzero exit classifies through the same .forge/exit.
        invocation = "python -m forge.lane_driver --driver claude"
        return preamble + invocation

    if driver == "codex-sdk-lane":
        preamble = (
            "for attempt in 1 2 3; do\n"
            f"  {_npm_pin('@openai/codex', pins['codex-sdk-lane'])} && break\n"
            '  echo "npm install of codex failed (attempt $attempt), retrying..."\n'
            "  sleep $((attempt * 5))\n"
            "done\n"
            "# R15: the resolved CLI version lands in the job log — pin\n"
            "# drift is visible, never silent.\n"
            "codex --version\n"
        )
        credential = (
            "# OPTIONAL provider-native credential: the ChatGPT-login auth\n"
            "# blob (OAuth tokens cannot ride an API-key env). Guarded: an\n"
            "# unauthenticated lane still runs and reports its own failure.\n"
            "mkdir -p ~/.codex\n"
            'if [ -n "$FORGE_CODEX_AUTH" ]; then\n'
            '  printf "%s" "$FORGE_CODEX_AUTH" > ~/.codex/auth.json\n'
            "  chmod 600 ~/.codex/auth.json\n"
            "fi\n"
        )
        model_export = f"export CODEX_MODEL={shlex.quote(model)}\n" if model else ""
        # The SDK lane's "invocation" is forge's own lane runner (EXE-02):
        # it spawns `codex app-server` (auth inherited from the ambient
        # env), drives ONE thread to turn/completed and writes the meta +
        # usage receipts itself — no -p prompt, no event tee, the runner's
        # nonzero exit classifies the run through the same .forge/exit.
        invocation = (
            f'{model_export}export CODEX_CWD="$PWD"\npython -m forge.lane_driver --driver codex'
        )
        return preamble + credential + invocation

    if driver == "opencode-sdk-lane":
        preamble = (
            "for attempt in 1 2 3; do\n"
            f"  {_npm_pin('opencode-ai', pins['opencode-sdk-lane'])} && break\n"
            '  echo "npm install of opencode failed (attempt $attempt), retrying..."\n'
            "  sleep $((attempt * 5))\n"
            "done\n"
            "# R15: the resolved CLI version lands in the job log — pin\n"
            "# drift is visible, never silent.\n"
            "opencode --version\n"
        )
        permission_config = json.dumps(
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
        # A "provider/model" route splits; a bare model is the model id
        # (the provider stays whatever the ambient env configured).
        if model and "/" in model:
            provider_id, _, model_id = model.partition("/")
            model_exports = (
                f"export OPENCODE_PROVIDER_ID={shlex.quote(provider_id)}\n" if provider_id else ""
            ) + (f"export OPENCODE_MODEL_ID={shlex.quote(model_id)}\n" if model_id else "")
        elif model:
            model_exports = f"export OPENCODE_MODEL_ID={shlex.quote(model)}\n"
        else:
            model_exports = ""
        # The lane runner spawns the serve process itself (lane-local,
        # loopback, password-pinned); the config-injection env rides the
        # ambient environment INTO that child (the spawner merges it), so
        # the mechanical deny reaches the server.
        invocation = (
            f"export OPENCODE_CONFIG_CONTENT={shlex.quote(permission_config)}\n"
            f"{model_exports}"
            'export OPENCODE_SERVE_CWD="$PWD"\n'
            # LIVE-found: the factory default "reject" starves every tool
            # call in a task lane — "once" grants per request.
            'export OPENCODE_PERMISSION_RESPONSE="${OPENCODE_PERMISSION_RESPONSE:-once}"\n'
            "python -m forge.lane_driver --driver opencode"
        )
        return preamble + invocation

    raise ValueError(f"unknown driver {driver!r} (expected one of {', '.join(DRIVERS)})")


def parse_usage(driver: str, event_log: str) -> dict | None:
    """Aggregate usage receipts out of the driver's NDJSON event log.

    - claude-code: per-turn ``result`` messages carry ``usage`` — summed
      (the aggregate is the sum; there is no run-level receipt).
    - grok-build: per-response ``usage`` events, EXCEPT the final ``end``
      event already carries the run aggregate — it wins when present
      (summing both would double-count).
    - opencode: no parseable stdout receipt — None (unknown, never zero).

    Anything unparseable is skipped; token counts that never appear stay
    None. Returns a :mod:`forge.runs.candidate`-compatible meta ``usage``
    shape (input_tokens / cached_input_tokens / output_tokens).
    """

    def _token(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    sums: dict[str, int | None] = {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
    }
    end_usage: dict | None = None

    for line in event_log.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        raw_usage = event.get("usage")
        payload: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        if driver == "grok-build" and etype == "end":
            # The run aggregate — wins over the per-response receipts.
            end_usage = payload or {}
            continue
        if driver == "claude-code" and etype == "result":
            source = payload  # per-turn receipt on the result
        elif driver == "grok-build" and etype == "usage":
            source = payload  # per-response boundary receipt
        else:
            continue
        for key, mapped in (
            ("input_tokens", "input_tokens"),
            ("cache_read_input_tokens", "cached_input_tokens"),
            ("output_tokens", "output_tokens"),
        ):
            value = _token(source.get(key))
            if value is not None:
                sums[mapped] = (sums[mapped] or 0) + value

    if driver == "grok-build" and end_usage is not None:
        end: dict[str, Any] = end_usage
        sums = {
            "input_tokens": _token(end.get("input_tokens")),
            "cached_input_tokens": _token(end.get("cache_read_input_tokens")),
            "output_tokens": _token(end.get("output_tokens")),
        }

    if all(value is None for value in sums.values()):
        return None
    return {
        **sums,
        "driver": driver,
        "completeness": "aggregate",
        "source": "stream-json",
    }


#: Candidate meta schema version emitted by :func:`emit_candidate_meta` and
#: accepted by the control plane
#: (:mod:`forge.execution.github_actions` — v1 is the historical schemaless
#: shape, v2 adds attempt identity + a diff digest + the usage receipt;
#: A18 adds the additive ``bootstrap`` classification and the executed
#: ``profile_digest`` audit fields without bumping the schema).
META_SCHEMA_VERSION = 2


def _attempt_identity() -> str:
    """GitHub's own attempt identity, ``<run_id>:<run_attempt>``.

    The runner injects both env vars on every job, so the lane gets its
    attempt identity for free (no new dispatch input); empty outside
    Actions (CLI/tests) — the meta then carries no identity rather than a
    fabricated one.
    """
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    return f"{run_id}:{attempt}" if run_id and attempt else ""


def _load_json_object(path: Path) -> dict | None:
    """Read *path* as a JSON object, or None.

    Missing, unparsable, or non-object content is None — the usage receipt
    is audit metadata, never worth failing the emit step over (unknown
    stays unknown, never zero).
    """
    try:
        data = json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_command_receipts(path: Path) -> list[tuple[str, int, str]]:
    """The trusted wrapper's command receipts (C10): TSV rows
    ``argv_head<TAB>exit<TAB>report`` — absent/unreadable means NO claim
    (never fabricated)."""
    if not path.is_file():
        return []
    receipts: list[tuple[str, int, str]] = []
    for raw in path.read_text(errors="replace").splitlines():
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        argv_head = parts[0].strip()
        try:
            code = int(parts[1])
        except ValueError:
            continue
        report = parts[2].strip() if len(parts) > 2 else ""
        if argv_head:
            receipts.append((argv_head, code, report))
    return receipts


def emit_candidate_meta(
    *,
    run_id: str,
    attempt_base_oid: str,
    driver: str,
    model: str,
    diff_file: str = "forge-output/candidate.diff",
    meta_file: str = "forge-output/candidate.meta.json",
    exit_file: str = ".forge/exit",
    usage_file: str = ".forge/usage.json",
    bootstrap_file: str = ".forge/bootstrap",
    profile_digest: str = "",
) -> dict:
    """Build and write the v2 ``candidate.meta.json`` beside the staged diff.

    The "Emit candidate artifact" step's contract (R16/R23): schema_version
    2, the forge run id + GitHub attempt identity, the frozen attempt base,
    the driver/model route, the driver's exit classification, a sha256
    digest BINDING the meta to the exact candidate.diff bytes (the control
    plane re-checks it after download), and the aggregated usage receipt
    from ``.forge/usage.json`` inlined as ``usage`` so spend reaches the
    control plane with the candidate. A18 adds two additive audit fields:
    ``bootstrap`` — the lane's environment-bootstrap classification the
    template wrote to ``.forge/bootstrap`` (``ok`` | ``failed``; a FAILED
    bootstrap is infrastructure/config, never code repair) — and
    ``profile_digest`` — the sha256 of the execution profile derived from
    THIS checkout (forge.runs.execution_profile), the executed twin of the
    digest frozen into the approved spec. Returns the written meta dict.
    """
    # Imported lazily: the profile module is pure stdlib, but importing it
    # initializes the forge.runs package — never worth paying on the
    # driver-run path, only here, in the emit step.
    from forge.runs.execution_profile import (
        BOOTSTRAP_STATUS_FAILED,
        BOOTSTRAP_STATUS_OK,
        observed_execution,
    )

    exit_path = Path(exit_file)
    exit_status = (
        exit_path.read_text(errors="replace").strip() if exit_path.is_file() else "unknown"
    )
    bootstrap_path = Path(bootstrap_file)
    bootstrap_status = (
        bootstrap_path.read_text(errors="replace").strip() if bootstrap_path.is_file() else ""
    )
    if bootstrap_status not in (BOOTSTRAP_STATUS_OK, BOOTSTRAP_STATUS_FAILED):
        bootstrap_status = ""  # unknown stays unknown — pre-A18 lanes carry no marker
    diff_bytes = Path(diff_file).read_bytes()
    meta = {
        "schema_version": META_SCHEMA_VERSION,
        "run_id": run_id,
        "attempt_id": _attempt_identity(),
        "attempt_base_oid": attempt_base_oid,
        "driver": driver,
        "model": model,
        "exit": exit_status,
        "bootstrap": bootstrap_status,
        "manifest_digest": f"sha256:{hashlib.sha256(diff_bytes).hexdigest()}",
        "usage": _load_json_object(Path(usage_file)),
        "profile_digest": str(profile_digest or "").strip().lower(),
        # NXT-11: the lane runner's steering journal + episode timing ride
        # the meta (additive — ABSENT when the lane ran with steering off).
        # The lane_driver writes them to .forge/steering.json; the emit step
        # passes them through UNTOUCHED (LIVE-found: the v2 meta rebuilt the
        # dict from scratch and dropped both keys on every SDK-lane run).
        **(
            {"steering_journal": lane_extras["steering_journal"]}
            if (lane_extras := _load_json_object(Path(".forge/steering.json")) or {}).get(
                "steering_journal"
            )
            else {}
        ),
        **({"episode": lane_extras["episode"]} if lane_extras.get("episode") else {}),
        **({"wip_restore": lane_extras["wip_restore"]} if lane_extras.get("wip_restore") else {}),
        # B12: the OBSERVED execution — what this lane actually did. The
        # declared profile (digest above) is allowance/expectation; a
        # command being allowed is not evidence it ran. Additive v2 field.
        "observed_execution": asdict(
            observed_execution(
                driver=driver,
                exit_status=exit_status,
                usage_completeness=(
                    (_load_json_object(Path(usage_file)) or {}).get("completeness") or "unknown"
                ),
                candidate_changed=len(diff_bytes) > 0,
                # C10: the trusted wrapper's receipts — one TSV row per
                # executed command: argv_head<TAB>exit<TAB>report_file.
                commands=_load_command_receipts(Path(".forge/commands.tsv")),
                # D08: workspace-file receipts are SELF-REPORTED (the agent
                # can write that file) — telemetry, never gate evidence.
                receipts_producer="self_reported",
            )
        ),
    }
    output = Path(meta_file)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return meta


def main(argv: list[str] | None = None) -> int:
    """Entry point: provision the brief, run the driver, write usage + exit."""
    parser = argparse.ArgumentParser(
        prog="python -m forge.harness_entry",
        description="Run a coding-agent harness driver unattended (forge Actions lane).",
    )
    parser.add_argument("--driver", default=None, help="claude-code | grok-build | opencode")
    parser.add_argument("--model", default=None, help="model route from the approved RunSpec")
    parser.add_argument("--brief", default=None, help="path of the task brief file")
    parser.add_argument("--exit-file", default=None, help="where to write completed|failed")
    parser.add_argument("--usage-file", default=".forge/usage.json", help="usage receipt path")
    parser.add_argument(
        "--events-file", default=".forge/events.jsonl", help="normalized event log path"
    )
    parser.add_argument(
        "--emit-meta",
        action="store_true",
        help="write the v2 candidate meta beside the staged diff (emit step)",
    )
    parser.add_argument("--forge-run-id", default=None, help="forge run id for --emit-meta")
    parser.add_argument(
        "--attempt-base-oid", default=None, help="frozen attempt base for --emit-meta"
    )
    parser.add_argument(
        "--diff-file",
        default="forge-output/candidate.diff",
        help="staged candidate diff path for --emit-meta",
    )
    parser.add_argument(
        "--meta-file",
        default="forge-output/candidate.meta.json",
        help="candidate meta output path for --emit-meta",
    )
    parser.add_argument(
        "--bootstrap-file",
        default=".forge/bootstrap",
        help="lane environment-bootstrap status file for --emit-meta (ok|failed)",
    )
    parser.add_argument(
        "--render-brief",
        action="store_true",
        help="fetch issue + forge plan from GitHub and render the quality brief",
    )
    parser.add_argument("--repo", default=None, help="owner/name for --render-brief")
    parser.add_argument("--issue", type=int, default=None, help="issue number for --render-brief")
    parser.add_argument("--github-token", default=None, help="token for --render-brief")
    parser.add_argument(
        "--plan-note-id",
        default=None,
        help=(
            "issue comment id of the approved forge plan for --render-brief "
            "(defaults to $FORGE_PLAN_NOTE_ID; exact-comment binding, fail-closed)"
        ),
    )
    parser.add_argument(
        "--envelope-digest",
        default=None,
        help=(
            "approved BriefEnvelope digest for --render-brief (defaults to "
            "$FORGE_ENVELOPE_DIGEST; A03 content binding, fail-closed)"
        ),
    )
    parser.add_argument(
        "--spec-digest",
        default=None,
        help=(
            "frozen RunSpec digest bound by the envelope for --render-brief "
            "(defaults to $FORGE_SPEC_DIGEST)"
        ),
    )
    parser.add_argument(
        "--render-brief-azure",
        action="store_true",
        help="fetch work item + forge plan from Azure DevOps and render the quality brief",
    )
    parser.add_argument(
        "--org-url", default=None, help="org/collection URL for --render-brief-azure"
    )
    parser.add_argument("--project", default=None, help="AzDO project for --render-brief-azure")
    parser.add_argument(
        "--read-token", default=None, help="read-only work-item token for --render-brief-azure"
    )
    parser.add_argument(
        "--mcp",
        default=None,
        help="canonical mcpServers JSON (defaults to $FORGE_HARNESS_MCP)",
    )
    args = parser.parse_args(argv)

    import os

    driver = (args.driver or os.environ.get("FORGE_DRIVER") or "").strip()
    model = (
        args.model or os.environ.get("FORGE_HARNESS_MODEL") or os.environ.get("FORGE_MODEL") or ""
    ).strip()
    brief = args.brief or os.environ.get("FORGE_BRIEF") or ".forge/brief.md"
    exit_file = Path(args.exit_file or os.environ.get("FORGE_EXIT_FILE") or ".forge/exit")

    def _finish(status: str, message: str = "") -> int:
        exit_file.parent.mkdir(parents=True, exist_ok=True)
        exit_file.write_text(status + "\n")
        if message:
            print(message, file=sys.stderr)
        return 0 if status == "completed" else 1

    if args.render_brief:
        repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
        token = args.github_token or os.environ.get("GITHUB_TOKEN", "")
        issue_number = args.issue or int(os.environ.get("FORGE_ISSUE_NUMBER") or 0)
        if not (repo and issue_number and token):
            return _finish(
                "failed",
                "harness_entry: --render-brief needs --repo/--issue/GITHUB_TOKEN",
            )
        # R05 interim transport: bind the brief to the EXACT approved plan
        # comment. The control plane journals the plan comment's id when it
        # posts the approved plan and dispatches it as plan_note_id; the
        # lane then fetches that one comment (validated, fail-closed) — no
        # scan, no identity heuristic. Empty on a legacy replay: the scan
        # below is the loud, UNENFORCED fallback.
        plan_note_raw = str(
            args.plan_note_id
            if args.plan_note_id is not None
            else os.environ.get("FORGE_PLAN_NOTE_ID") or ""
        ).strip()
        run_id = (os.environ.get("FORGE_RUN_ID") or "").strip()
        # A03: the approved-brief-bytes binding — the dispatched envelope +
        # spec digests turn the bound-comment transport into a content
        # check. Any of them absent (legacy replay) keeps the pre-A03
        # posture: task bytes read LIVE from the issue, loudly unenforced.
        envelope_digest = str(
            args.envelope_digest
            if args.envelope_digest is not None
            else os.environ.get("FORGE_ENVELOPE_DIGEST") or ""
        ).strip()
        spec_digest_raw = (
            args.spec_digest
            if args.spec_digest is not None
            else os.environ.get("FORGE_SPEC_DIGEST") or ""
        )
        spec_digest = str(spec_digest_raw).strip()
        try:
            plan_note_id = int(plan_note_raw) if plan_note_raw else 0
        except ValueError:
            return _finish("failed", f"harness_entry: bad plan note id {plan_note_raw!r}")
        if not plan_note_id:
            print(
                "harness_entry: FORGE_PLAN_NOTE_ID absent — plan-comment binding NOT "
                "enforced; falling back to the legacy plan-comment scan",
                file=sys.stderr,
            )
        elif not (envelope_digest and spec_digest):
            print(
                "harness_entry: FORGE_ENVELOPE_DIGEST/FORGE_SPEC_DIGEST absent (legacy "
                "replay) — approved-brief-bytes binding NOT enforced; task text is read "
                "LIVE from the issue",
                file=sys.stderr,
            )
        try:
            body, plan = fetch_issue_context(
                repo,
                issue_number,
                token,
                plan_note_id=plan_note_id,
                run_id=run_id,
                envelope_digest=envelope_digest,
                spec_digest=spec_digest,
            )
        except PlanBindingError as exc:
            return _finish("failed", f"harness_entry: {exc}")
        except OSError as exc:
            # HTTPError/URLError included: a missing/unreadable bound comment
            # must fail the lane, never render a brief without its plan.
            return _finish("failed", f"harness_entry: plan comment fetch failed: {exc}")
        brief_text = render_brief(body, plan)
        repair_context = os.environ.get("FORGE_REPAIR_CONTEXT", "")
        if repair_context.strip():
            # Bounded verification-failure context on a repair re-dispatch
            # (ADR-0008): the agent fixes its own candidate against the
            # named failing checks instead of re-proposing blind.
            brief_text += (
                "\n\n## Repair context — previous candidate failed verification\n\n"
                f"{repair_context[:2000]}\n"
            )
        brief_path = Path(args.brief or ".forge/brief.md")
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(brief_text)
        print(f"harness_entry: brief rendered at {brief_path}")
        return 0

    if args.render_brief_azure:
        org_url = args.org_url or os.environ.get("FORGE_AZDO_ORG_URL", "")
        project = args.project or os.environ.get("FORGE_AZDO_PROJECT", "")
        token = args.read_token or os.environ.get("FORGE_AZDO_READ_TOKEN", "")
        bot_name = os.environ.get("FORGE_AZDO_BOT_NAME", "forge-bot")
        issue_number = args.issue or int(os.environ.get("FORGE_ISSUE_NUMBER") or 0)
        if not (org_url and project and issue_number and token):
            return _finish(
                "failed",
                "harness_entry: --render-brief-azure needs --org-url/--project/"
                "--issue/FORGE_AZDO_READ_TOKEN",
            )
        # B04/A03: the ENFORCED approved-bytes transport — when the control
        # plane dispatched plan_note_id + envelope_digest + spec_digest, the
        # brief comes from EXACTLY the addressed comment's approved
        # sections, re-verified against the frozen envelope digest. A
        # work-item edit, a substituted second plan comment or a tampered
        # comment fails the lane CLOSED — never the live-item heuristic,
        # never a fallback brief. Inputs absent (legacy dispatch) keep the
        # loud UNENFORCED scan below.
        run_id = (os.environ.get("FORGE_RUN_ID") or "").strip()
        plan_note_raw = (os.environ.get("FORGE_PLAN_NOTE_ID") or "").strip()
        envelope_digest = (os.environ.get("FORGE_ENVELOPE_DIGEST") or "").strip()
        spec_digest = (os.environ.get("FORGE_SPEC_DIGEST") or "").strip()
        try:
            plan_note_id = int(plan_note_raw) if plan_note_raw else 0
        except ValueError:
            return _finish("failed", f"harness_entry: bad plan note id {plan_note_raw!r}")
        brief_path = Path(args.brief or ".forge/brief.md")
        if plan_note_id and envelope_digest and spec_digest:
            try:
                comment_text = fetch_workitem_comment(
                    org_url, project, issue_number, plan_note_id, token
                )
                from forge.harnesses.brief_envelope import (
                    BriefEnvelopeError,
                    extract_approved_sections,
                    verify_brief_envelope,
                )

                task_title, task_description, plan = extract_approved_sections(comment_text)
                verify_brief_envelope(
                    envelope_digest,
                    run_id=run_id,
                    task_title=task_title,
                    task_description=task_description,
                    plan_text=plan,
                    spec_digest=spec_digest,
                )
            except (PlanBindingError, BriefEnvelopeError) as exc:
                return _finish("failed", f"harness_entry: {exc}")
            except OSError as exc:
                return _finish("failed", f"harness_entry: plan comment fetch failed: {exc}")
            brief_text = render_brief(f"{task_title}\n{task_description}", plan)
            repair_context = os.environ.get("FORGE_REPAIR_CONTEXT", "")
            if repair_context.strip():
                brief_text += (
                    "\n\n## Repair context — previous candidate failed verification\n\n"
                    f"{repair_context[:2000]}\n"
                )
            brief_path.parent.mkdir(parents=True, exist_ok=True)
            brief_path.write_text(brief_text)
            print(f"harness_entry: brief rendered (envelope-verified) at {brief_path}")
            return 0
        print(
            "harness_entry: FORGE_PLAN_NOTE_ID/ENVELOPE_DIGEST/SPEC_DIGEST absent "
            "(legacy dispatch) — approved-brief-bytes binding NOT enforced; "
            "reading the LIVE work item",
            file=sys.stderr,
        )
        body, plan = fetch_workitem(org_url, project, issue_number, token, bot_name=bot_name)
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(render_brief(body, plan))
        print(f"harness_entry: brief rendered at {brief_path}")
        return 0

    if args.emit_meta:
        # The emit step's meta build (R16/R23): identity is passed through
        # as dispatched (no DRIVERS validation — this is an audit record,
        # not a driver invocation); a missing diff fails LOUD (rc 1) so the
        # upload's if-no-files-found turns it into an infrastructure-class
        # lane failure instead of a silent partial artifact.
        # A18: the executed execution profile is derived from THIS checkout
        # and its digest rides the meta — the honest twin of the digest the
        # gate froze into the approved spec. Best-effort: a derivation
        # failure degrades to an empty digest, never fails the emit step.
        profile_digest = ""
        try:
            from forge.runs.execution_profile import (
                LocalRepoSource,
                derive_from_repo as _derive_profile,
            )

            profile_digest = _derive_profile(LocalRepoSource(Path.cwd())).profile_digest
        except Exception as exc:  # noqa: BLE001 — audit metadata, never lane-fatal
            print(
                f"harness_entry: execution profile unavailable ({exc}) — meta carries no digest",
                file=sys.stderr,
            )
        try:
            emit_candidate_meta(
                run_id=args.forge_run_id or "",
                attempt_base_oid=args.attempt_base_oid or "",
                driver=driver,
                model=model,
                diff_file=args.diff_file,
                meta_file=args.meta_file,
                exit_file=str(exit_file),
                usage_file=args.usage_file,
                bootstrap_file=args.bootstrap_file,
                profile_digest=profile_digest,
            )
        except OSError as exc:
            print(f"harness_entry: emit-meta failed ({exc})", file=sys.stderr)
            return 1
        print(f"harness_entry: candidate meta written at {args.meta_file}")
        return 0

    if driver not in DRIVERS:
        return _finish("failed", f"harness_entry: unknown driver {driver!r}")
    if not Path(brief).is_file():
        return _finish("failed", f"harness_entry: brief file {brief!r} is missing")

    mcp_raw = args.mcp if args.mcp is not None else os.environ.get("FORGE_HARNESS_MCP")
    if mcp_raw is not None and mcp_raw.startswith("$(") and mcp_raw.endswith(")"):
        # AzDO lane reality (LIVE-found, ADR-0024): an UNDEFINED pipeline
        # variable reaches the process as its literal "$(NAME)" — treat it
        # as unset instead of failing the fail-closed parse on junk.
        mcp_raw = None
    try:
        mcp_servers = parse_servers(mcp_raw)
    except McpConfigError as exc:
        # Fail-closed: a broken MCP config refuses the lane rather than
        # silently running without the servers the task may depend on.
        return _finish("failed", f"harness_entry: {exc}")

    # R15: the per-driver CLI version pins (same-named repo VARIABLE,
    # passed through by the workflow template). Fail-closed like the MCP
    # parse: a typo in the pins must never downgrade the lane to an
    # unpinned (or shell-interpreted) install.
    try:
        driver_versions = resolve_driver_versions(os.environ.get("FORGE_DRIVER_VERSIONS"))
    except ValueError as exc:
        return _finish("failed", f"harness_entry: {exc}")

    try:
        script = render_driver_script(
            driver,
            model,
            brief,
            events_file=args.events_file,
            mcp_servers=mcp_servers,
            driver_versions=driver_versions,
        )
    except ValueError as exc:
        return _finish("failed", f"harness_entry: {exc}")

    # The universal log filter rides from the same pinned forge ref the
    # lane trusts (raw.githubusercontent over the pinned ref); if it cannot
    # be fetched the driver still runs — the log degrades to raw output,
    # never blocks the candidate.
    filter_ref = os.environ.get("FORGE_PINNED_REF", "main")
    filter_url = (
        "https://raw.githubusercontent.com/forcewake/forge/"
        f"{filter_ref}/ci/templates/harness-log-filter.mjs"
    )
    try:
        import urllib.request

        urllib.request.urlretrieve(filter_url, "/tmp/harness-log-filter.mjs")
        os.environ["FORGE_FILTER_PIPE"] = f'node /tmp/harness-log-filter.mjs "{driver}"'
    except Exception as exc:
        print(f"harness_entry: log filter unavailable ({exc}) — raw output", file=sys.stderr)
        os.environ["FORGE_FILTER_PIPE"] = "cat"

    completed = subprocess.run(  # noqa: S603, S602 — fixed argv, lane-local script
        ["/bin/bash", "-o", "pipefail", "-c", script]
    )
    status = "completed" if completed.returncode == 0 else "failed"

    # The usage receipt comes from the tee'd event log, never from a
    # harness claim outside it — EXCEPT on the SDK lanes, where the lane
    # runner already wrote .forge/usage.json from the driver client's own
    # receipts: that file is the authority and is never clobbered with a
    # zeroed parse of an event log the lane runner never wrote.
    usage = None
    events_path = Path(args.events_file)
    if driver not in LANE_DRIVERS:
        if events_path.is_file():
            usage = parse_usage(driver, events_path.read_text(errors="replace"))
        usage_path = Path(args.usage_file)
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        usage_path.write_text(json.dumps(usage, indent=2, sort_keys=True) + "\n")

    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.write_text(status + "\n")
    print(f"harness_entry: driver {driver} exit={completed.returncode}", file=sys.stderr)
    # ALWAYS exit 0 once the lane completed its duties: the workflow's
    # candidate steps run ``if: always()`` and forge classifies the run
    # from the artifact's meta exit — a red step here would rob the audit
    # trail, exactly the failure mode the GitLab templates avoid.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
