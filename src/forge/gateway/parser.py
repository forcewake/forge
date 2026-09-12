from __future__ import annotations

import logging

from forge.gitlab.events import (
    GitLabEvent,
    IssueEvent,
    JobEvent,
    MergeRequestEvent,
    NoteEvent,
    PipelineEvent,
    PushEvent,
)

logger = logging.getLogger(__name__)

_EVENT_MAP: dict[str, type[GitLabEvent]] = {
    "Merge Request Hook": MergeRequestEvent,
    "Note Hook": NoteEvent,
    "Pipeline Hook": PipelineEvent,
    "Push Hook": PushEvent,
    "Issue Hook": IssueEvent,
    "Job Hook": JobEvent,
}

# Maps object_kind values to event model classes. Used by the worker to
# deserialize events from the task queue without duplicating the mapping.
EVENT_KIND_MAP: dict[str, type[GitLabEvent]] = {
    "merge_request": MergeRequestEvent,
    "note": NoteEvent,
    "pipeline": PipelineEvent,
    "push": PushEvent,
    "issue": IssueEvent,
    "build": JobEvent,
}


def parse_webhook(event_header: str, payload: dict) -> GitLabEvent:
    """Parse raw webhook payload into a typed event model.

    Args:
        event_header: Value of the X-Gitlab-Event header.
        payload: Raw JSON body from the webhook.

    Returns:
        A typed GitLabEvent subclass, or the base GitLabEvent for unknown types.

    Raises:
        pydantic.ValidationError: If the payload is malformed.
    """
    model_cls = _EVENT_MAP.get(event_header)

    if model_cls is None:
        logger.warning("Unknown GitLab event type: %s", event_header)
        return GitLabEvent.model_validate(payload)

    return model_cls.model_validate(payload)
