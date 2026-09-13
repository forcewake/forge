from __future__ import annotations

import hashlib
from uuid import uuid4

from forge.gateway.parser import EVENT_KIND_MAP
from forge.gitlab.events import GitLabEvent
from forge.worker.queue import Task


def create_task(event: GitLabEvent, priority: int = 0) -> Task:
    """Create a Task from a GitLab webhook event.

    Serializes the Pydantic event model into a JSON-safe dict and generates
    a human-readable task ID.
    """
    project_id = event.project.id if event.project else 0
    target_iid = _extract_iid(event)
    short_id = uuid4().hex[:8]

    return Task(
        task_id=f"{project_id}:{event.object_kind}:{target_iid}:{short_id}",
        event_type=event.object_kind,
        event_data=event.model_dump(mode="json"),
        priority=priority,
    )


def deserialize_event(task: Task) -> GitLabEvent:
    """Reconstruct a typed GitLabEvent from a Task's stored data."""
    model_cls = EVENT_KIND_MAP.get(task.event_type)
    if model_cls is None:
        return GitLabEvent.model_validate(task.event_data)
    return model_cls.model_validate(task.event_data)


def compute_fingerprint(event: GitLabEvent) -> str:
    """Generate a dedup fingerprint for an event.

    Based on project ID, event type, target IID, and action.
    """
    project_id = event.project.id if event.project else 0
    target_iid = _extract_iid(event)
    action = _extract_action(event)

    raw = f"{project_id}:{event.object_kind}:{target_iid}:{action}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _extract_iid(event: GitLabEvent) -> int | str:
    """Extract the target IID (MR, issue, pipeline, etc.) from an event."""
    if hasattr(event, "object_attributes"):
        attrs = event.object_attributes
        if hasattr(attrs, "iid"):
            return attrs.iid
        if hasattr(attrs, "id"):
            return attrs.id
    if hasattr(event, "merge_request") and event.merge_request:
        mr = event.merge_request
        if hasattr(mr, "iid") and mr.iid:
            return mr.iid
    return 0


def create_flow_step_task(
    flow_id: str,
    step_index: int,
    flow_name: str,
    priority: int = 0,
) -> Task:
    """Create a Task for executing a flow step in the worker."""
    short_id = flow_id[:8]
    return Task(
        task_id=f"flow:{flow_name}:{short_id}:step-{step_index}",
        event_type="flow_step",
        event_data={},
        priority=priority,
        task_type="flow_step",
        metadata={"flow_id": flow_id, "step_index": step_index},
    )


def create_run_command_task(metadata: dict, note_id: int | str = 0, priority: int = 0) -> Task:
    """Create a Task for the durable run service (M1 /implement and /go notes).

    ``metadata`` carries the command ("start_run" | "go") plus the project,
    issue, note text and author needed by :meth:`forge.runs.RunService.run_command`.
    ``note_id`` gives the task a stable id so re-delivered webhooks map to the
    same task identity. ADR-0017: the persisted StepRun is the authority, so
    the task metadata also carries the command's inbox identity — the worker
    claims that step before executing (the task is only the wake-up).
    """
    from forge.worker.steps import command_source_event_id

    project_id = metadata.get("project_id", 0)
    issue_iid = metadata.get("issue_iid", 0)
    command = metadata.get("command", "unknown")
    payload = dict(metadata)
    payload.setdefault(
        "source_event_id",
        command_source_event_id(str(command), int(project_id or 0), note_id),
    )
    return Task(
        task_id=f"run:{command}:{project_id}:{issue_iid}:{note_id}",
        event_type="run_command",
        event_data={},
        priority=priority,
        task_type="run_command",
        metadata=payload,
    )


def _extract_action(event: GitLabEvent) -> str:
    """Extract the action from an event (e.g., 'open', 'update', 'close')."""
    if hasattr(event, "object_attributes"):
        attrs = event.object_attributes
        if hasattr(attrs, "action") and attrs.action:
            return attrs.action
    return ""
