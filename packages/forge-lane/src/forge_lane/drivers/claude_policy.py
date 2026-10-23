"""A fixed file-tool policy until the protocol carries approved typed policy."""

import os
import re
from pathlib import Path
from typing import Any

TOOLS = ("Read", "Write", "Edit")
DENIED = ("Bash", "Glob", "Grep", "Agent", "Task", "WebFetch", "WebSearch", "NotebookEdit", "Skill")
PRIVATE_PARTS = frozenset({".git", ".forge", ".github", ".claude", ".ssh", ".aws", ".kube"})
PRIVATE_NAMES = frozenset(
    {
        ".forge.yml",
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
    def __init__(self, workspace: Path, sdk: Any):
        self.workspace = workspace.resolve()
        self.sdk = sdk
        self.denials: set[str] = set()

    def permitted(self, name: str, arguments: dict[str, Any]) -> bool:
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
        return bool(lexical.parts)

    async def permission(self, name: str, arguments: dict[str, Any], context: Any):
        if not self.permitted(name, arguments):
            self.denials.add("tool_policy_refused")
            return self.sdk.PermissionResultDeny(
                message="Tool or path is outside the approved lane capability", interrupt=True
            )
        return self.sdk.PermissionResultAllow(updated_input=arguments)

    async def pre_tool(self, event: dict[str, Any], tool_id: str | None, context: Any):
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
    )
    return env


def redact(text: str, secret: str) -> str:
    text = text.replace(secret, "[REDACTED]") if secret else text
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", text)
    return re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+", r"\1[REDACTED]", text)[
        :4096
    ]
