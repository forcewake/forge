import pytest

from forge.agents.registry import AgentDefinition, TriggerSpec
from forge.config import ForgeConfig
from forge.gitlab.events import (
    JobEvent,
    MergeRequestEvent,
    MRObjectAttributes,
    NoteEvent,
    NoteObjectAttributes,
    PipelineEvent,
    PipelineObjectAttributes,
)
from forge.orchestrator.matcher import match_agents
from forge.orchestrator.project_config import ProjectConfig


def _make_registry_stub(agents: list[AgentDefinition]):
    """Create a minimal registry-like object."""

    class StubRegistry:
        def all(self):
            return agents

    return StubRegistry()


def _make_mr_event(
    action: str = "open", draft: bool = False, title: str = "Fix bug"
) -> MergeRequestEvent:
    return MergeRequestEvent(
        object_kind="merge_request",
        object_attributes=MRObjectAttributes(
            id=1,
            iid=42,
            title=title,
            action=action,
            draft=draft,
        ),
    )


def _make_note_event(note_text: str = "Hello @forge") -> NoteEvent:
    return NoteEvent(
        object_kind="note",
        object_attributes=NoteObjectAttributes(
            id=1,
            note=note_text,
        ),
    )


def _make_agent(
    name: str = "code-reviewer",
    events: list[str] | None = None,
    mention: bool = False,
    skip_draft: bool = True,
    skip_wip: bool = True,
) -> AgentDefinition:
    if events is None:
        events = ["merge_request.open", "merge_request.update"]

    # Parse events into triggers
    merged: dict[str, TriggerSpec] = {}
    for ev in events:
        parts = ev.split(".", 1)
        event = parts[0]
        action = parts[1] if len(parts) > 1 else ""
        if event in merged:
            if action:
                merged[event].actions.append(action)
        else:
            merged[event] = TriggerSpec(
                event=event,
                actions=[action] if action else [],
                mention=mention,
            )

    return AgentDefinition(
        name=name,
        triggers=list(merged.values()),
        settings={"skip_draft": skip_draft, "skip_wip": skip_wip},
        actions={"inline_comments": True},
    )


@pytest.fixture()
def forge_config() -> ForgeConfig:
    return ForgeConfig(path="nonexistent.yml")


@pytest.fixture()
def project_config() -> ProjectConfig:
    return ProjectConfig()


class TestMatchAgents:
    def test_mr_open_matches(self, forge_config, project_config):
        agent = _make_agent()
        registry = _make_registry_stub([agent])
        event = _make_mr_event(action="open")
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 1
        assert matched[0].name == "code-reviewer"

    def test_mr_update_matches(self, forge_config, project_config):
        agent = _make_agent()
        registry = _make_registry_stub([agent])
        event = _make_mr_event(action="update")
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 1

    def test_mr_close_no_match(self, forge_config, project_config):
        agent = _make_agent()
        registry = _make_registry_stub([agent])
        event = _make_mr_event(action="close")
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 0

    def test_draft_mr_skipped(self, forge_config, project_config):
        agent = _make_agent(skip_draft=True)
        registry = _make_registry_stub([agent])
        event = _make_mr_event(action="open", draft=True)
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 0

    def test_draft_mr_not_skipped_when_disabled(self, forge_config, project_config):
        agent = _make_agent(skip_draft=False)
        registry = _make_registry_stub([agent])
        event = _make_mr_event(action="open", draft=True)
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 1

    def test_wip_mr_skipped(self, forge_config, project_config):
        agent = _make_agent(skip_wip=True)
        registry = _make_registry_stub([agent])
        event = _make_mr_event(action="open", title="WIP: work in progress")
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 0

    def test_note_with_mention_matches(self, forge_config, project_config):
        agent = _make_agent(name="chat-agent", events=["note"], mention=True)
        registry = _make_registry_stub([agent])
        event = _make_note_event(note_text="Hey @forge please review")
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 1

    def test_note_without_mention_no_match(self, forge_config, project_config):
        agent = _make_agent(name="chat-agent", events=["note"], mention=True)
        registry = _make_registry_stub([agent])
        event = _make_note_event(note_text="Just a regular comment")
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 0

    def test_disabled_agent_filtered(self, forge_config):
        agent = _make_agent()
        registry = _make_registry_stub([agent])
        project_config = ProjectConfig(disabled_agents=["code-reviewer"])
        event = _make_mr_event(action="open")
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 0

    def test_enabled_agents_filter(self, forge_config):
        agent1 = _make_agent(name="code-reviewer")
        agent2 = _make_agent(name="security-scanner")
        registry = _make_registry_stub([agent1, agent2])
        project_config = ProjectConfig(enabled_agents=["security-scanner"])
        event = _make_mr_event(action="open")
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 1
        assert matched[0].name == "security-scanner"

    def test_pipeline_failed_matches(self, forge_config, project_config):
        agent = AgentDefinition(
            name="pipeline-debugger",
            triggers=[TriggerSpec(event="pipeline", actions=["failed"])],
        )
        registry = _make_registry_stub([agent])
        event = PipelineEvent(
            object_kind="pipeline",
            object_attributes=PipelineObjectAttributes(id=10, status="failed"),
        )
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 1
        assert matched[0].name == "pipeline-debugger"

    def test_pipeline_success_no_match_for_failed_trigger(self, forge_config, project_config):
        agent = AgentDefinition(
            name="pipeline-debugger",
            triggers=[TriggerSpec(event="pipeline", actions=["failed"])],
        )
        registry = _make_registry_stub([agent])
        event = PipelineEvent(
            object_kind="pipeline",
            object_attributes=PipelineObjectAttributes(id=10, status="success"),
        )
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 0

    def test_job_event_matches_with_job_name(self, forge_config, project_config):
        agent = AgentDefinition(
            name="security-triage",
            triggers=[
                TriggerSpec(
                    event="build",
                    actions=["success"],
                    job_names=["semgrep-sast", "sast"],
                ),
            ],
        )
        registry = _make_registry_stub([agent])
        event = JobEvent(
            object_kind="build",
            build_name="semgrep-sast",
            build_status="success",
        )
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 1

    def test_job_event_wrong_name_no_match(self, forge_config, project_config):
        agent = AgentDefinition(
            name="security-triage",
            triggers=[
                TriggerSpec(
                    event="build",
                    actions=["success"],
                    job_names=["semgrep-sast"],
                ),
            ],
        )
        registry = _make_registry_stub([agent])
        event = JobEvent(
            object_kind="build",
            build_name="unit-test",
            build_status="success",
        )
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 0

    def test_job_event_wrong_status_no_match(self, forge_config, project_config):
        agent = AgentDefinition(
            name="security-triage",
            triggers=[
                TriggerSpec(
                    event="build",
                    actions=["success"],
                    job_names=["semgrep-sast"],
                ),
            ],
        )
        registry = _make_registry_stub([agent])
        event = JobEvent(
            object_kind="build",
            build_name="semgrep-sast",
            build_status="failed",
        )
        matched = match_agents(event, registry, project_config, forge_config)
        assert len(matched) == 0
