from __future__ import annotations


from forge.worker.queue import Task


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
