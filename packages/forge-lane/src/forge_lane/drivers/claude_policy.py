"""Fixed credential boundaries plus the exact frozen write and MCP grants."""

import os
import re
from pathlib import Path
from typing import Any

from forge_protocol import ExecutionPolicy
from forge_protocol.spec import FrozenWriteScope

from ..mcp import claude_tools
from ..write_scope import writable

TOOLS = ("Read", "Write", "Edit", "Grep", "Glob", "Bash")
DENIED = ("Agent", "Task", "WebFetch", "WebSearch", "NotebookEdit", "Skill")
_BASH = re.compile(r"^(?:(?:uv|python|python3)(?: -m| run) )?(?:pytest|ruff)(?: [\w./:=@+-]+)*$")
PRIVATE_PARTS = frozenset(
    {".git", ".forge", ".github", ".claude", ".copilot", ".ssh", ".aws", ".kube"}
)
PRIVATE_NAMES = frozenset(
    {
        ".forge.yml",
        ".mcp.json",
        ".gitconfig",
        ".gitmodules",
        ".gitlab-ci.yml",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "codeowners",
        "credentials",
        "id_rsa",
        "id_ed25519",
    }
)


class FilePolicy:
    def __init__(
        self,
        workspace: Path,
        sdk: Any,
        *,
        scope: FrozenWriteScope | None = None,
        execution: ExecutionPolicy = ExecutionPolicy(),
    ):
        self.workspace = workspace.resolve()
        self.sdk = sdk
        self.denials: set[str] = set()
        self.scope = scope
        self.mcp_tools = claude_tools(execution)

    def permitted(self, name: str, arguments: dict[str, Any]) -> bool:
        if name in self.mcp_tools and isinstance(arguments, dict):
            return True
        if name in {"Grep", "Glob"}:
            return self._search_permitted(arguments)
        if name == "Bash":
            return self._bash_permitted(arguments)
        if name not in TOOLS or not isinstance(arguments.get("file_path"), str):
            return False
        raw = Path(arguments["file_path"])
        target = raw if raw.is_absolute() else self.workspace / raw
        try:
            lexical = target.relative_to(self.workspace)
            resolved = target.resolve().relative_to(self.workspace)
        except (OSError, RuntimeError, ValueError):
            return False
        for path in (lexical, resolved):
            parts = [part.casefold() for part in path.parts]
            if any(part in PRIVATE_PARTS or part == ".." for part in parts):
                return False
            if any(
                part.startswith(".env") or part.endswith((".pem", ".key")) or part in PRIVATE_NAMES
                for part in parts
            ):
                return False
            if (
                name != "Read"
                and self.scope is not None
                and not writable(self.scope, path.as_posix())
            ):
                return False
        return bool(lexical.parts)

    def _inside(self, raw: str) -> bool:
        return self.permitted("Read", {"file_path": raw or "."})

    def _search_permitted(self, arguments: dict[str, Any]) -> bool:
        path = arguments.get("path", arguments.get("file_path", "."))
        if path in ("", "."):
            return True
        return isinstance(path, str) and self._inside(path)

    def _bash_permitted(self, arguments: dict[str, Any]) -> bool:
        command = arguments.get("command")
        if not isinstance(command, str) or any(char in command for char in "\n;&|`$<>\\"):
            return False
        return _BASH.fullmatch(command) is not None

    async def permission(self, name: str, arguments: dict[str, Any], context: Any) -> Any:
        if not self.permitted(name, arguments):
            self.denials.add("tool_policy_refused")
            return self.sdk.PermissionResultDeny(
                message="Tool or path is outside the approved lane capability", interrupt=True
            )
        return self.sdk.PermissionResultAllow(updated_input=arguments)

    async def pre_tool(
        self, event: dict[str, Any], tool_id: str | None, context: Any
    ) -> dict[str, Any]:
        if self.permitted(event.get("tool_name", ""), event.get("tool_input", {})):
            return {"continue_": True}
        self.denials.add("tool_policy_refused")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Tool or path is outside the approved lane capability",
            }
        }

    def inventory(self) -> str:
        paths = []
        for directory, directories, files in os.walk(self.workspace, followlinks=False):
            directories[:] = sorted(
                name
                for name in directories
                if name.casefold() not in PRIVATE_PARTS
                and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(files):
                path = Path(directory) / name
                if self.permitted("Read", {"file_path": str(path)}):
                    paths.append(str(path.relative_to(self.workspace)))
                    if len(paths) >= 1000:
                        return "\n".join(paths) + "\n[inventory capped]"
        return "\n".join(paths)


def child_environment(config: Path, gateway: str, key: str) -> dict[str, str]:
    retained = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "SYSTEMROOT",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
    env = {name: value if name in retained else "" for name, value in os.environ.items()}
    env.update(
        HOME=str(config),
        XDG_CONFIG_HOME=str(config),
        XDG_CACHE_HOME=str(config / "cache"),
        CLAUDE_CONFIG_DIR=str(config / "claude"),
        ANTHROPIC_BASE_URL=gateway.rstrip("/"),
        ANTHROPIC_AUTH_TOKEN=key,
        ANTHROPIC_API_KEY="",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS="1",
        CLAUDE_CODE_DISABLE_FAST_MODE="1",
        ENABLE_TOOL_SEARCH="false",
    )
    return env


def redact(text: str, secret: str) -> str:
    text = text.replace(secret, "[REDACTED]") if secret else text
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", text)
    return re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+", r"\1[REDACTED]", text)[
        :4096
    ]
