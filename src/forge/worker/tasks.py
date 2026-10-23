from __future__ import annotations


from forge.worker.queue import Task


def create_run_command_task(metadata: dict, note_id: int | str = 0, priority: int = 0) -> Task:
    """Create a Task for the durable run service (implement and /go notes)."""
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
