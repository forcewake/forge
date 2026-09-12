from forge.gitlab.events import (
    MergeRequestEvent,
    MRObjectAttributes,
    NoteEvent,
    NoteObjectAttributes,
    PipelineEvent,
    PipelineObjectAttributes,
    ProjectInfo,
    UserInfo,
)
from forge.worker.tasks import compute_fingerprint


def _project(pid: int = 42) -> ProjectInfo:
    return ProjectInfo(
        id=pid,
        name="test",
        path_with_namespace="group/test",
        web_url="https://gitlab.test/group/test",
    )


def _user() -> UserInfo:
    return UserInfo(id=1, name="Test", username="testuser")


def _mr_event(action: str = "open", iid: int = 10, project_id: int = 42) -> MergeRequestEvent:
    return MergeRequestEvent(
        object_kind="merge_request",
        user=_user(),
        project=_project(project_id),
        object_attributes=MRObjectAttributes(
            id=100,
            iid=iid,
            title="Test MR",
            action=action,
        ),
    )


def _note_event(note: str = "hello", project_id: int = 42) -> NoteEvent:
    return NoteEvent(
        object_kind="note",
        user=_user(),
        project=_project(project_id),
        object_attributes=NoteObjectAttributes(id=200, note=note),
    )


def _pipeline_event(pipeline_id: int = 300, project_id: int = 42) -> PipelineEvent:
    return PipelineEvent(
        object_kind="pipeline",
        user=_user(),
        project=_project(project_id),
        object_attributes=PipelineObjectAttributes(id=pipeline_id),
    )


def test_fingerprint_deterministic():
    """Same event should always produce the same fingerprint."""
    event = _mr_event(action="open", iid=10)
    fp1 = compute_fingerprint(event)
    fp2 = compute_fingerprint(event)
    assert fp1 == fp2
    assert len(fp1) == 16  # Truncated sha256


def test_fingerprint_differs_for_different_actions():
    open_event = _mr_event(action="open", iid=10)
    update_event = _mr_event(action="update", iid=10)
    assert compute_fingerprint(open_event) != compute_fingerprint(update_event)


def test_fingerprint_differs_for_different_iids():
    mr1 = _mr_event(action="open", iid=10)
    mr2 = _mr_event(action="open", iid=20)
    assert compute_fingerprint(mr1) != compute_fingerprint(mr2)


def test_fingerprint_differs_for_different_projects():
    e1 = _mr_event(project_id=1)
    e2 = _mr_event(project_id=2)
    assert compute_fingerprint(e1) != compute_fingerprint(e2)


def test_fingerprint_differs_for_different_event_types():
    mr = _mr_event()
    note = _note_event()
    pipeline = _pipeline_event()
    fps = {compute_fingerprint(mr), compute_fingerprint(note), compute_fingerprint(pipeline)}
    assert len(fps) == 3  # All different
