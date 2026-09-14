"""Evidence policy (F23): one redaction canary at the durable boundary.

Free-text evidence that originates outside forge — CI job logs, harness
artifact metadata — passes through :meth:`EvidencePolicy.apply_policy`
before it enters a comment, the run row or a repair brief. Two simple
rules, no regex: values containing a configured deny pattern (a literal
substring such as ``glpat-``) are replaced with a ``[REDACTED:rule]``
placeholder, and the item is capped at ``FORGE_EVIDENCE_MAX_CHARS``.
Redaction is deterministic and idempotent; text without a deny match is
returned byte-for-byte unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge.config import Settings

#: Placeholder written over every redacted value. Deliberately generic —
#: it must never itself contain a deny pattern (the canary asserts that).
REDACTED_PLACEHOLDER = "[REDACTED:rule]"

#: Characters that may belong to the secret value following a deny match.
#: Anything else (whitespace, quotes, ``:``) ends the token.
_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-+=")

_DEFAULT_DENY_PATTERNS = ("glpat-", "ghs_", "sk-", "xai-")
_DEFAULT_MAX_CHARS = 8000


@dataclass(frozen=True)
class EvidencePolicy:
    """The effective evidence policy for one deployment (F23)."""

    deny_patterns: tuple[str, ...] = _DEFAULT_DENY_PATTERNS
    max_chars: int = _DEFAULT_MAX_CHARS

    @classmethod
    def from_settings(cls, settings: Settings) -> EvidencePolicy:
        """Build the policy from ``FORGE_EVIDENCE_*`` settings."""
        raw = str(getattr(settings, "FORGE_EVIDENCE_DENY_PATTERNS", "") or "")
        patterns = tuple(pattern.strip() for pattern in raw.split(",") if pattern.strip())
        max_chars = int(
            getattr(settings, "FORGE_EVIDENCE_MAX_CHARS", _DEFAULT_MAX_CHARS) or _DEFAULT_MAX_CHARS
        )
        return cls(deny_patterns=patterns or _DEFAULT_DENY_PATTERNS, max_chars=max_chars)

    def redact(self, text: str) -> str:
        """Replace deny-pattern values in *text* with the placeholder."""
        result = text
        for pattern in self.deny_patterns:
            result = _replace_all(result, pattern)
        return result

    def apply_policy(self, text: str) -> tuple[str, bool]:
        """Redact, then cap *text* at ``max_chars``.

        Returns the policy-bound text and whether anything was redacted
        (truncation alone is not redaction).
        """
        redacted = self.redact(text)
        changed = redacted != text
        if len(redacted) > self.max_chars:
            redacted = redacted[: self.max_chars]
        return redacted, changed


def _replace_all(text: str, pattern: str) -> str:
    """Replace every *pattern* match plus its trailing token chars."""
    pieces: list[str] = []
    pos = 0
    while True:
        start = text.find(pattern, pos)
        if start < 0:
            pieces.append(text[pos:])
            return "".join(pieces)
        end = start + len(pattern)
        while end < len(text) and text[end] in _TOKEN_CHARS:
            end += 1
        pieces.append(text[pos:start])
        pieces.append(REDACTED_PLACEHOLDER)
        pos = end
