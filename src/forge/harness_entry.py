"""forge harness entry point — the Actions lane's driver runner (Stage E3b).

The "Run harness driver" step of ``ci/templates/forge-harness.github.yml``
installs forge from a pinned ref and runs ``python -m forge.harness_entry``.
This module is the ENTIRE forge surface inside the ephemeral runner:

- it renders the SHARED implementation brief
  (:func:`forge.harnesses.prompt.render_brief` — one prompt builder for
  both the Actions and the GitLab lanes) into ``.forge/brief.md``; the
  brief file the agent reads IS the prompt, and the per-CLI ``-p``
  invocation stays the short pointer :data:`forge.harnesses.prompt.TASK_PROMPT`;
- it renders the per-driver invocation (claude-code | grok-build | opencode)
  from the SAME contract the GitLab templates implement
  (``ci/templates/*.gitlab-ci.yml``; interface ground truth:
  ``docs/research/harness-interfaces.md``) — per-CLI FLAGS live here, the
  PROMPT is shared;
- it runs the driver unattended (proposal-only: the agent is told to leave
  its changes in the working tree — it cannot commit or push, the lane has
  no write credential);
- it aggregates the driver's usage receipts into ``.forge/usage.json``
  (unknown stays unknown, never zero — F22 lite) and writes ``.forge/exit``
  (``completed`` | ``failed``), decoupled from the agent's own output so
  the workflow can still upload the candidate artifact ``if: always()``.

Brief transport (the dispatch-input size question, decided): the lane
FETCHES its own brief content from the GitHub API — the approved plan is
ALREADY on the issue as the forge plan comment (E3a posted it), so
``--render-brief`` reads the issue body + that comment with the runner's
read-only ``GITHUB_TOKEN`` (:func:`fetch_issue_context`). No
workflow_dispatch input ever carries plan text, so no input size limit
binds. Alternatives kept for parity: a pre-provisioned ``.forge/brief.md``
works as-is (the workflow owner may ship it however they like).

Stdlib + the pure prompt builder only: no forge database, no forge
credentials — the lane runs forge's CODE, never forge's state.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from forge.harnesses.prompt import (
    TASK_PROMPT,
    BriefContext,
    render_brief as render_shared_brief,
)

#: Claude's scoped shell allowlist (same posture as the GitLab template:
#: edits auto-accepted, shell limited to read-only git).
_CLAUDE_ALLOWED_TOOLS = "Bash(git status:*),Bash(git diff:*),Bash(git log:*)"

#: Drivers understood by this entry point (the shipped multi-harness set).
DRIVERS = ("claude-code", "grok-build", "opencode")


def render_brief(issue_text: str, plan_text: str) -> str:
    """Render the quality brief via the SHARED builder (single source).

    Thin adapter over :func:`forge.harnesses.prompt.render_brief` in the
    proposal-only ``ci_lane`` output contract; *issue_text* is the issue
    body snapshot, *plan_text* the approved plan (verbatim). Conventions
    files (AGENTS.md / CLAUDE.md) are detected in the current working
    directory — the checkout the agent will work in.
    """
    return render_shared_brief(
        BriefContext(
            plan=plan_text,
            issue_body=issue_text,
            driver=os.environ.get("FORGE_DRIVER", ""),
            model=os.environ.get("FORGE_HARNESS_MODEL", ""),
        ),
        lane="ci_lane",
        repo_root=Path.cwd(),
    )


def fetch_issue_context(repo: str, issue_number: int, token: str) -> tuple[str, str]:
    """Fetch (issue body, forge plan comment) via the GitHub REST API.

    Uses only the stdlib: the lane runs forge's code without forge's
    dependencies. The plan comment is the latest comment by the forge app
    author that contains the plan heading.
    """
    import json
    import urllib.request

    def _get(url: str) -> object:
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

    issue = _get(f"https://api.github.com/repos/{repo}/issues/{issue_number}")
    body = str(issue.get("body") or "")
    comments = _get(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments?per_page=100"
    )
    plan_text = ""
    for comment in reversed(comments if isinstance(comments, list) else []):
        author = (comment.get("user") or {}).get("login", "")
        text = str(comment.get("body") or "")
        if author in ("forge", "forcewake-forge") and "plan" in text.lower():
            plan_text = text
            break
    return body, plan_text


def render_driver_script(
    driver: str,
    model: str,
    brief_path: str,
    *,
    events_file: str = ".forge/events.jsonl",
    debug_log: str = ".forge/grok-debug.log",
) -> str:
    """The bash script that provisions and invokes *driver* unattended.

    Rendered per driver from the GitLab templates' contract:

    - ``claude-code`` — headless print mode, stream-json events, no external
      settings (prompt-injection surface reduction), acceptEdits with a
      git-only shell allowlist;
    - ``grok-build`` — the hardened npm preamble first: the wrapper declares
      its platform binary as an optionalDependency, so a flaky registry
      silently skips it and the CLI hangs forever at startup. Both packages
      are installed explicitly, with retries (verified live, template
      comment); ``--always-approve`` is required or headless grok HANGS;
    - ``opencode`` — ``run --auto`` (approves what the permission config
      does not deny; the ephemeral runner is the execution profile). The
      model is config-owned here, not CLI-owned (mirror of the GitLab
      template, which routes it through opencode.json).

    The ``-p`` prompt is the shared SHORT pointer (:data:`TASK_PROMPT`) —
    the brief file at *brief_path* carries the whole contract. The script
    streams the driver's normalized event log into the job log AND tees it
    to *events_file*; ``pipefail`` keeps the driver's exit code so a
    nonzero agent exit classifies the run as failed WITHOUT aborting the
    audit trail (the workflow uploads artifacts ``if: always()``).
    """
    quoted_prompt = shlex.quote(TASK_PROMPT)
    events = shlex.quote(events_file)

    if driver == "claude-code":
        model_flag = f" --model {shlex.quote(model)}" if model else ""
        invocation = (
            f"claude -p {quoted_prompt}{model_flag} \\\n"
            f"  --allowedTools {shlex.quote(_CLAUDE_ALLOWED_TOOLS)} \\\n"
            "  --permission-mode acceptEdits \\\n"
            "  --setting-sources '' --output-format stream-json --verbose 2>&1"
        )
        return f"{invocation} | tee -a {events}"

    if driver == "grok-build":
        preamble = (
            "for attempt in 1 2 3; do\n"
            "  npm install -g --no-fund --no-audit @xai-official/grok && break\n"
            '  echo "npm install of grok failed (attempt $attempt), retrying..."\n'
            "  sleep $((attempt * 5))\n"
            "done\n"
            "GROK_VER=\"$(grok --version | awk '{print $2}')\"\n"
            'npm install -g --no-fund --no-audit "@xai-official/grok-linux-x64@${GROK_VER}" \\\n'
            "  || npm install -g --no-fund --no-audit @xai-official/grok-linux-x64\n"
            "test -d /usr/local/lib/node_modules/@xai-official/grok-linux-x64\n"
            "grok --version\n"
        )
        invocation = (
            "grok --no-auto-update --always-approve --no-alt-screen \\\n"
            "  --output-format streaming-json \\\n"
            f"  --debug-file {shlex.quote(debug_log)} \\\n"
            f"  -p {quoted_prompt} 2>&1"
        )
        return preamble + f"{invocation} | tee -a {events}"

    if driver == "opencode":
        invocation = f"opencode run --auto {quoted_prompt} 2>&1"
        return f"{invocation} | tee -a {events}"

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

    sums = {"input_tokens": None, "cached_input_tokens": None, "output_tokens": None}
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
        payload = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        if driver == "grok-build" and etype == "end":
            # The run aggregate — wins over the per-response receipts.
            end_usage = payload or {}
            continue
        if driver == "claude-code" and etype == "result":
            source = payload  # per-turn receipt on the terminal result
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
        sums = {
            "input_tokens": _token(end_usage.get("input_tokens")),
            "cached_input_tokens": _token(end_usage.get("cache_read_input_tokens")),
            "output_tokens": _token(end_usage.get("output_tokens")),
        }

    if all(value is None for value in sums.values()):
        return None
    return {
        **sums,
        "driver": driver,
        "completeness": "aggregate",
        "source": "stream-json",
    }


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
        "--render-brief",
        action="store_true",
        help="fetch issue + forge plan from GitHub and render the quality brief",
    )
    parser.add_argument("--repo", default=None, help="owner/name for --render-brief")
    parser.add_argument("--issue", type=int, default=None, help="issue number for --render-brief")
    parser.add_argument("--github-token", default=None, help="token for --render-brief")
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
        body, plan = fetch_issue_context(repo, issue_number, token)
        brief_path = Path(args.brief or ".forge/brief.md")
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(render_brief(body, plan))
        print(f"harness_entry: brief rendered at {brief_path}")
        return 0

    if driver not in DRIVERS:
        return _finish("failed", f"harness_entry: unknown driver {driver!r}")
    if not Path(brief).is_file():
        return _finish("failed", f"harness_entry: brief file {brief!r} is missing")

    try:
        script = render_driver_script(driver, model, brief, events_file=args.events_file)
    except ValueError as exc:
        return _finish("failed", f"harness_entry: {exc}")

    completed = subprocess.run(  # noqa: S603, S602 — fixed argv, lane-local script
        ["/bin/bash", "-o", "pipefail", "-c", script]
    )
    status = "completed" if completed.returncode == 0 else "failed"

    # The usage receipt comes from the tee'd event log, never from a
    # harness claim outside it.
    usage = None
    events_path = Path(args.events_file)
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
