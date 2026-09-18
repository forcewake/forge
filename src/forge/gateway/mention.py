from __future__ import annotations

import re
from dataclasses import dataclass

KNOWN_COMMANDS = frozenset(
    {
        "/explain",
        "/review",
        "/debug",
        "/security",
        "/help",
        "/summarize",
    }
)


@dataclass(frozen=True)
class MentionInfo:
    """Parsed @mention data from a GitLab note body."""

    raw_text: str
    mention_text: str
    slash_command: str | None
    command_args: str | None
    is_mention: bool


def extract_mention(
    note_body: str,
    mention_pattern: str = "@forge",
    extra_commands: frozenset[str] | None = None,
) -> MentionInfo:
    """Extract @mention and optional slash command from a note body.

    Args:
        note_body: Full text of the GitLab note/comment.
        mention_pattern: The mention trigger string (e.g. ``@forge``).

    Returns:
        A ``MentionInfo`` describing what was found.

    Examples:
        >>> extract_mention("@forge explain this function")
        MentionInfo(... mention_text='explain this function', slash_command=None ...)

        >>> extract_mention("@forge /review focus on error handling")
        MentionInfo(... slash_command='/review', command_args='focus on error handling' ...)

        >>> extract_mention("Thanks @forge")
        MentionInfo(... is_mention=True, mention_text='' ...)

        >>> extract_mention("No mention here")
        MentionInfo(... is_mention=False ...)
    """
    pattern = re.compile(re.escape(mention_pattern), re.IGNORECASE)
    match = pattern.search(note_body)

    if not match:
        return MentionInfo(
            raw_text=note_body,
            mention_text="",
            slash_command=None,
            command_args=None,
            is_mention=False,
        )

    # Text after the mention pattern
    after = note_body[match.end() :].strip()

    # Check for a slash command. Hyphenated commands (`/why-blocked`, R29)
    # parse as ONE command token; acceptance is still gated by the known
    # command set below, so an unknown hyphenated token falls through to the
    # plain-message path exactly as before.
    slash_match = re.match(r"(/[\w-]+)\s*(.*)", after, re.DOTALL)
    if slash_match:
        command = slash_match.group(1).lower()
        args_text = slash_match.group(2).strip()
        all_commands = KNOWN_COMMANDS | extra_commands if extra_commands else KNOWN_COMMANDS
        if command in all_commands:
            return MentionInfo(
                raw_text=note_body,
                mention_text=args_text,
                slash_command=command,
                command_args=args_text or None,
                is_mention=True,
            )

    # No slash command — everything after the mention is the message
    return MentionInfo(
        raw_text=note_body,
        mention_text=after,
        slash_command=None,
        command_args=None,
        is_mention=True,
    )
