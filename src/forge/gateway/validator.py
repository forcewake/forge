"""Webhook token validation and bot-loop prevention.

GitLab CE sends the configured secret as a plain value in the
``X-Gitlab-Token`` header.  We use ``hmac.compare_digest`` for
constant-time comparison to prevent timing side-channel attacks —
this is *not* HMAC signing, just a timing-safe equality check.
"""

from __future__ import annotations
import hmac

from fastapi import Header, HTTPException

from forge.gitlab.events import GitLabEvent, MergeRequestEvent


def validate_webhook_token(
    expected_secret: str,
    x_gitlab_token: str | None = Header(None),
) -> str:
    """Raise 401 if the token is missing or does not match."""
    if x_gitlab_token is None:
        raise HTTPException(status_code=401, detail="Missing X-Gitlab-Token header")
    if not hmac.compare_digest(x_gitlab_token, expected_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook token")
    return x_gitlab_token


def is_bot_event(event: GitLabEvent, bot_username: str) -> bool:
    """Return True if this event was authored by the bot itself."""
    # General check: top-level user field
    if event.user and event.user.username == bot_username:
        return True

    # MR update: check if last commit was authored by the bot
    if (
        isinstance(event, MergeRequestEvent)
        and event.object_attributes.action == "update"
        and event.object_attributes.last_commit
        and event.object_attributes.last_commit.author
        and event.object_attributes.last_commit.author.name == bot_username
    ):
        return True

    return False
