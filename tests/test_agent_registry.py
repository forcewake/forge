import tempfile
from pathlib import Path

from forge.agents.registry import AgentRegistry, _parse_definition, _parse_triggers


class TestParseTriggers:
    def test_single_event_with_action(self):
        raw = {"events": ["merge_request.open"]}
        triggers = _parse_triggers(raw)
        assert len(triggers) == 1
        assert triggers[0].event == "merge_request"
        assert triggers[0].actions == ["open"]

    def test_multiple_actions_merged(self):
        raw = {"events": ["merge_request.open", "merge_request.update"]}
        triggers = _parse_triggers(raw)
        assert len(triggers) == 1
        assert triggers[0].event == "merge_request"
        assert set(triggers[0].actions) == {"open", "update"}

    def test_event_without_action(self):
        raw = {"events": ["pipeline"]}
        triggers = _parse_triggers(raw)
        assert len(triggers) == 1
        assert triggers[0].event == "pipeline"
        assert triggers[0].actions == []

    def test_mention_flag(self):
        raw = {"events": ["note"], "mention": True}
        triggers = _parse_triggers(raw)
        assert triggers[0].mention is True

    def test_empty_events(self):
        triggers = _parse_triggers({})
        assert triggers == []


class TestParseDefinition:
    def test_minimal_definition(self):
        data = {"name": "test-agent"}
        defn = _parse_definition(data)
        assert defn.name == "test-agent"
        assert defn.version == "1.0"
        assert defn.model_alias == "default"
        assert defn.triggers == []

    def test_full_definition(self):
        data = {
            "name": "code-reviewer",
            "version": "2.0",
            "description": "Reviews code",
            "trigger": {
                "events": ["merge_request.open"],
                "cooldown": 60,
                "skip_draft": False,
            },
            "model": {"default": "strong"},
            "system_prompt": "You are a reviewer for {project_path}.",
            "context": {"needs_diff": True},
            "output": {"format": "json"},
            "actions": {"inline_comments": True, "summary_note": True},
        }
        defn = _parse_definition(data)
        assert defn.name == "code-reviewer"
        assert defn.version == "2.0"
        assert defn.model_alias == "strong"
        assert len(defn.triggers) == 1
        assert defn.settings["cooldown"] == 60
        assert defn.settings["skip_draft"] is False
        assert defn.actions["inline_comments"] is True

    def test_model_as_string(self):
        data = {"name": "test", "model": "fast"}
        defn = _parse_definition(data)
        assert defn.model_alias == "fast"


class TestAgentRegistry:
    def test_load_from_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yml = Path(tmpdir) / "test-agent.yml"
            yml.write_text(
                "name: test-agent\n"
                "description: A test agent\n"
                "trigger:\n"
                "  events:\n"
                "    - merge_request.open\n"
            )
            registry = AgentRegistry(tmpdir)
            registry.load()
            assert registry.get("test-agent") is not None
            assert len(registry.all()) == 1

    def test_invalid_yaml_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad = Path(tmpdir) / "bad.yml"
            bad.write_text("not: a: valid: yaml: [[[")
            good = Path(tmpdir) / "good.yml"
            good.write_text("name: good-agent\n")
            registry = AgentRegistry(tmpdir)
            registry.load()
            assert registry.get("good-agent") is not None
            assert len(registry.all()) == 1

    def test_missing_name_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yml = Path(tmpdir) / "no-name.yml"
            yml.write_text("description: Missing name field\n")
            registry = AgentRegistry(tmpdir)
            registry.load()
            assert len(registry.all()) == 0

    def test_missing_directory(self):
        registry = AgentRegistry("/nonexistent/path")
        registry.load()  # Should not raise
        assert len(registry.all()) == 0

    def test_get_nonexistent(self):
        registry = AgentRegistry("/nonexistent/path")
        assert registry.get("nope") is None

    def test_loads_real_code_reviewer_yml(self):
        """Verify the actual agents/code-reviewer.yml loads correctly."""
        agents_dir = Path(__file__).parent.parent / "agents"
        if not agents_dir.exists():
            return  # Skip if not available
        registry = AgentRegistry(agents_dir)
        registry.load()
        cr = registry.get("code-reviewer")
        assert cr is not None
        assert cr.name == "code-reviewer"
        assert cr.model_alias == "code"
        assert len(cr.triggers) >= 1
        assert cr.triggers[0].event == "merge_request"
        assert "open" in cr.triggers[0].actions
