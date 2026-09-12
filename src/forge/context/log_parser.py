from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ParsedJobLog:
    """Structured representation of a parsed CI job log."""

    full_length: int  # Original line count
    truncated: bool = False
    error_section: str = ""  # Lines around the failure
    exit_code: int | None = None
    error_type: str | None = None  # test_failure, dependency, docker, timeout, ...
    key_lines: list[str] = field(default_factory=list)
    tail: str = ""  # Last 20 lines as fallback context


# (pattern, error_type) — order matters: first match wins
_ERROR_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Test failures
    (re.compile(r"FAILED|FAILURES|tests? failed", re.IGNORECASE), "test_failure"),
    (re.compile(r"AssertionError|AssertionFailedError|assert .+ =="), "test_failure"),
    (re.compile(r"^E\s+\w+Error:", re.MULTILINE), "test_failure"),
    (re.compile(r"^\d+ (failing|failed)", re.MULTILINE), "test_failure"),
    (re.compile(r"rspec .*\d+ failures?", re.IGNORECASE), "test_failure"),
    # Dependency issues
    (re.compile(r"ModuleNotFoundError|ImportError"), "dependency"),
    (re.compile(r"npm ERR!|npm error", re.IGNORECASE), "dependency"),
    (re.compile(r"pip install.*failed|Could not find a version", re.IGNORECASE), "dependency"),
    (
        re.compile(r"Could not resolve dependencies|dependency resolution failed", re.IGNORECASE),
        "dependency",
    ),
    (re.compile(r"go: .+: no matching versions", re.IGNORECASE), "dependency"),
    # Docker issues
    (
        re.compile(r"docker build.*failed|Cannot connect to the Docker daemon", re.IGNORECASE),
        "docker",
    ),
    (re.compile(r"pull access denied|image .+ not found", re.IGNORECASE), "docker"),
    (re.compile(r"error during connect.*docker", re.IGNORECASE), "docker"),
    # Timeout
    (re.compile(r"Job exceeded time limit|Timed out|TimeoutError", re.IGNORECASE), "timeout"),
    (re.compile(r"execution expired|deadline exceeded", re.IGNORECASE), "timeout"),
    # Syntax / compilation errors
    (re.compile(r"SyntaxError|IndentationError"), "syntax"),
    (re.compile(r"error\[E\d+\]:", re.IGNORECASE), "syntax"),  # Rust
    (re.compile(r"error: aborting due to \d+ previous error", re.IGNORECASE), "syntax"),  # Rust
    (re.compile(r"\.go:\d+:\d+:.*"), "syntax"),  # Go compilation
    (re.compile(r"BUILD FAILURE|compilation failed", re.IGNORECASE), "syntax"),
    # Permission issues
    (re.compile(r"Permission denied|EACCES", re.IGNORECASE), "permission"),
    (re.compile(r"403 Forbidden", re.IGNORECASE), "permission"),
    # Network issues
    (re.compile(r"Connection refused|ETIMEDOUT|ECONNREFUSED", re.IGNORECASE), "network"),
    (re.compile(r"Could not resolve host|Name or service not known", re.IGNORECASE), "network"),
    (re.compile(r"SSL certificate problem|certificate verify failed", re.IGNORECASE), "network"),
    # CI config issues
    (re.compile(r"\.gitlab-ci\.yml.*error|invalid value for keyword", re.IGNORECASE), "config"),
    (re.compile(r"jobs config should contain", re.IGNORECASE), "config"),
]

_EXIT_CODE_PATTERN = re.compile(
    r"exit code (\d+)|exited with (\d+)|ERROR: Job failed: exit status (\d+)"
)

# Lines that indicate important output
_KEY_LINE_PATTERNS = [
    re.compile(r"^(ERROR|FATAL|FAILED|FAILURE|error|fatal)", re.IGNORECASE),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"^E\s+"),  # pytest error lines
    re.compile(r"^\s*raise \w+"),
    re.compile(r"SUMMARY|RESULT|Total:", re.IGNORECASE),
]

_CONTEXT_WINDOW = 25  # Lines before and after an error indicator
_TAIL_LINES = 20
_HEAD_LINES = 10


def parse_job_log(raw_log: str, max_lines: int = 300) -> ParsedJobLog:
    """Parse a raw CI job log into structured sections.

    Strategy:
    1. Find error/failure indicators
    2. Extract a window of lines around each error indicator
    3. Include the last 20 lines (often contain summaries)
    4. Include the first 10 lines (setup info)
    5. Classify error type
    6. Truncate to fit line budget
    """
    lines = raw_log.splitlines()
    full_length = len(lines)

    if not lines:
        return ParsedJobLog(full_length=0)

    # Extract tail (last N lines — often has the summary)
    tail_lines = lines[-_TAIL_LINES:] if len(lines) > _TAIL_LINES else lines
    tail = "\n".join(tail_lines)

    # Find exit code
    exit_code = _extract_exit_code(raw_log)

    # Classify error type
    error_type = _classify_error(raw_log)

    # Find error indicator line numbers
    error_line_nums = _find_error_lines(lines)

    # Extract key lines
    key_lines = _extract_key_lines(lines)

    # Build error section: windows around each error indicator
    error_section_lines: list[str] = []
    included: set[int] = set()

    # Always include head
    for i in range(min(_HEAD_LINES, len(lines))):
        if i not in included:
            included.add(i)

    # Windows around error indicators
    for line_num in error_line_nums:
        start = max(0, line_num - _CONTEXT_WINDOW)
        end = min(len(lines), line_num + _CONTEXT_WINDOW + 1)
        for i in range(start, end):
            if i not in included:
                included.add(i)

    # Always include tail
    tail_start = max(0, len(lines) - _TAIL_LINES)
    for i in range(tail_start, len(lines)):
        if i not in included:
            included.add(i)

    # Build the error section in order, with markers for gaps
    sorted_indices = sorted(included)
    prev = -1
    for idx in sorted_indices:
        if prev >= 0 and idx > prev + 1:
            error_section_lines.append(f"... ({idx - prev - 1} lines omitted) ...")
        error_section_lines.append(lines[idx])
        prev = idx

    # Truncate if over budget
    truncated = len(error_section_lines) > max_lines
    if truncated:
        error_section_lines = error_section_lines[:max_lines]
        error_section_lines.append("... (truncated) ...")

    error_section = "\n".join(error_section_lines)

    return ParsedJobLog(
        full_length=full_length,
        truncated=truncated or len(included) < full_length,
        error_section=error_section,
        exit_code=exit_code,
        error_type=error_type,
        key_lines=key_lines[:20],  # Cap at 20 key lines
        tail=tail,
    )


def format_parsed_log(parsed: ParsedJobLog) -> str:
    """Format a ParsedJobLog into a compact string for LLM consumption."""
    parts: list[str] = []

    if parsed.error_type:
        parts.append(f"**Error Type:** {parsed.error_type}")
    if parsed.exit_code is not None:
        parts.append(f"**Exit Code:** {parsed.exit_code}")
    parts.append(f"**Log Length:** {parsed.full_length} lines (truncated: {parsed.truncated})")

    if parsed.key_lines:
        parts.append("\n**Key Lines:**")
        for line in parsed.key_lines:
            parts.append(f"  {line}")

    if parsed.error_section:
        parts.append(f"\n**Log Output:**\n```\n{parsed.error_section}\n```")
    elif parsed.tail:
        parts.append(f"\n**Log Tail:**\n```\n{parsed.tail}\n```")

    return "\n".join(parts)


def _extract_exit_code(text: str) -> int | None:
    """Extract exit code from log text."""
    match = _EXIT_CODE_PATTERN.search(text)
    if match:
        for group in match.groups():
            if group is not None:
                try:
                    return int(group)
                except ValueError:
                    pass
    return None


def _classify_error(text: str) -> str | None:
    """Classify the error type based on pattern matching."""
    for pattern, error_type in _ERROR_PATTERNS:
        if pattern.search(text):
            return error_type
    return "unknown"


def _find_error_lines(lines: list[str]) -> list[int]:
    """Find line numbers that contain error indicators."""
    error_lines: list[int] = []
    for i, line in enumerate(lines):
        for pattern, _ in _ERROR_PATTERNS:
            if pattern.search(line):
                error_lines.append(i)
                break
    return error_lines


def _extract_key_lines(lines: list[str]) -> list[str]:
    """Extract the most important lines from the log."""
    key: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in _KEY_LINE_PATTERNS:
            if pattern.search(stripped):
                key.append(stripped)
                break
    return key
