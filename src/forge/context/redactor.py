from __future__ import annotations

import math
import re
from collections import Counter

# Each tuple: (human-readable label, regex pattern string)
BUILT_IN_PATTERNS: list[tuple[str, str]] = [
    ("aws_key", r"(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}"),
    ("aws_secret", r"(?i)aws_secret_access_key\s*[=:]\s*[A-Za-z0-9/+=]{40}"),
    ("gitlab_token", r"glpat-[0-9a-zA-Z_\-]{20,}"),
    ("github_token", r"gh[pousr]_[0-9a-zA-Z]{36,}"),
    ("generic_secret", r"(?i)(?:sk-[a-zA-Z0-9]{20,})"),
    (
        "generic_token",
        r"(?i)(?:token|api_key|apikey|secret)\s*[=:]\s*['\"]?[^\s'\"]{20,}",
    ),
    (
        "password",
        r"(?i)(?:password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\"]+",
    ),
    (
        "private_key",
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
    ),
    (
        "connection_string",
        r"(?i)(?:mysql|postgres(?:ql)?|mongodb(?:\+srv)?|redis|amqp)://[^\s]+:[^\s]+@[^\s]+",
    ),
]


class Redactor:
    """Redact known secret patterns and high-entropy strings from text."""

    _ASSIGNMENT_RE = re.compile(
        r"""(?:^|(?<=\s))    # start of line or after whitespace
            [A-Z_a-z][A-Z_a-z0-9]*  # variable name
            \s*[=:]\s*              # assignment operator
            ['\"]?                  # optional opening quote
            ([^\s'\"]{16,})         # capture: value (>= 16 non-space chars)
            """,
        re.VERBOSE | re.MULTILINE,
    )

    def __init__(
        self,
        extra_patterns: list[dict[str, str]] | None = None,
        entropy_threshold: float = 4.5,
    ) -> None:
        self._entropy_threshold = entropy_threshold

        # Compile all patterns: built-in + user-supplied
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        for label, pattern in BUILT_IN_PATTERNS:
            flags = re.MULTILINE | re.DOTALL if "PRIVATE KEY" in pattern else 0
            self._patterns.append((label, re.compile(pattern, flags)))

        if extra_patterns:
            for item in extra_patterns:
                name = item.get("name", "custom")
                pat = item.get("pattern", "")
                if pat:
                    self._patterns.append((name, re.compile(pat)))

    def redact(self, text: str) -> str:
        """Replace detected secrets with ``[REDACTED <type>]`` placeholders."""
        result = text
        for label, pattern in self._patterns:
            result = pattern.sub(f"[REDACTED {label}]", result)
        result = self._redact_high_entropy(result)
        return result

    def _redact_high_entropy(self, text: str) -> str:
        """Flag high-entropy strings that appear in assignment contexts."""

        def _replace(match: re.Match[str]) -> str:
            value = match.group(1)
            if self._shannon_entropy(value) > self._entropy_threshold:
                return match.group(0).replace(value, "[REDACTED high-entropy]")
            return match.group(0)

        return self._ASSIGNMENT_RE.sub(_replace, text)

    @staticmethod
    def _shannon_entropy(s: str) -> float:
        """Compute Shannon entropy (bits) of a string."""
        if not s:
            return 0.0
        freq = Counter(s)
        length = len(s)
        return -sum((count / length) * math.log2(count / length) for count in freq.values())
