"""Project configuration is independent of the legacy orchestration package."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_project_config_reader_has_a_shared_home():
    module = importlib.import_module("forge.project_config")
    assert module.ProjectConfig.__module__ == "forge.project_config"
    assert module.ConfigReadResult.__module__ == "forge.project_config"
    assert callable(module.read_project_config)


def test_project_config_has_no_legacy_importers():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "src/forge/orchestrator/project_config.py").exists()
    old_name = "forge.orchestrator." + "project_config"
    remaining = [
        str(path.relative_to(root))
        for directory in ("src", "tests")
        for path in (root / directory).rglob("*.py")
        if old_name in path.read_text(encoding="utf-8")
    ]
    assert not remaining, remaining


@pytest.mark.parametrize("content", ["neighbors: wrong-shape\n", "neighbors: [1]\n"])
def test_malformed_neighbor_authorization_is_rejected(content):
    module = importlib.import_module("forge.project_config")
    with pytest.raises(ValueError, match="neighbors"):
        module._parse_project_config(content)


async def test_default_triage_runner_passes_shared_config_and_preserves_failure(
    monkeypatch, test_settings, tmp_path
):
    from forge.agents.models import AgentResult, SecurityTriageResult
    from forge.agents.security_triage import SecurityTriageAgent
    from forge.config import ForgeConfig
    from forge.findings.models import SecurityFinding
    from forge.findings.triage import _default_triage_runner

    project_config = importlib.import_module("forge.project_config").ProjectConfig
    settings = test_settings.model_copy(
        update={"FORGE_AGENTS_DIR": str(Path(__file__).resolve().parents[1] / "agents")}
    )
    finding = SecurityFinding(
        provider="gitlab",
        scope="42",
        source="gitlab_sast",
        fingerprint="f" * 64,
        severity="high",
        title="SQL injection",
        path="app/db.py",
        line=7,
    )
    diagnosis = SecurityTriageResult(summary="Use a parameterized query", risk_level="high")
    outcomes = [
        AgentResult(success=True, security_triage=diagnosis),
        AgentResult(success=True),
    ]
    constructed = []

    async def run(agent):
        constructed.append(agent)
        assert type(agent.project_config) is project_config
        assert agent.project_config.implement_paths == []
        assert agent.project_config.review_rules == []
        assert agent.project_config.enabled_agents is None
        assert agent.context.project_path == "group/project"
        assert agent.context.event_type == "security_triage"
        assert agent.context.security_reports[0].findings[0].id == finding.fingerprint
        assert "SQL injection" in agent._build_user_message()
        assert agent.definition.name == "security-triage"
        assert agent.model.id == "openai/strong"
        assert agent.gitlab is None
        return outcomes.pop(0)

    monkeypatch.setattr(SecurityTriageAgent, "run", run)
    config = ForgeConfig(tmp_path / "missing-forge.yml")
    result = await _default_triage_runner(settings, config, [finding], project_path="group/project")
    assert result is diagnosis
    constructed[0].project_config.review_rules.append("first call only")
    with pytest.raises(RuntimeError, match="security-triage agent failed: no output"):
        await _default_triage_runner(settings, config, [finding], project_path="group/project")
    assert len(constructed) == 2
    assert constructed[0].project_config is not constructed[1].project_config


async def test_default_debug_runner_passes_shared_config_and_preserves_failure(
    monkeypatch, test_settings, tmp_path
):
    from forge.agents.models import AgentResult, PipelineDebugResult
    from forge.agents.pipeline_debugger import PipelineDebuggerAgent
    from forge.config import ForgeConfig
    from forge.gitlab.schemas import Job
    from forge.reactive.ci_debug import _default_debug_runner

    project_config = importlib.import_module("forge.project_config").ProjectConfig
    settings = test_settings.model_copy(
        update={"FORGE_AGENTS_DIR": str(Path(__file__).resolve().parents[1] / "agents")}
    )
    jobs = [
        Job(id=11, name="unit-tests", stage="test", status="failed"),
        Job(id=12, name="missing-log", stage="test", status="failed"),
    ]
    logs = {11: "ValueError: incompatible dependency\n"}
    diagnosis = PipelineDebugResult(summary="Align the dependency versions")
    outcomes = [
        AgentResult(success=True, pipeline_debug=diagnosis),
        AgentResult(success=False, error="model failed"),
    ]
    constructed = []

    async def run(agent):
        constructed.append(agent)
        assert type(agent.project_config) is project_config
        assert agent.project_config.implement_paths == []
        assert agent.project_config.review_rules == []
        assert agent.project_config.enabled_agents is None
        assert agent.context.project_path == "group/project"
        assert agent.context.event_type == "pipeline"
        assert agent.context.failed_jobs == jobs
        assert agent.context.job_logs == logs
        prompt = agent._build_user_message()
        assert "ValueError: incompatible dependency" in prompt
        assert "missing-log" in prompt and "(no log available)" in prompt
        assert agent.definition.name == "pipeline-debugger"
        assert agent.model.id == "openai/strong"
        assert agent.gitlab is None
        return outcomes.pop(0)

    monkeypatch.setattr(PipelineDebuggerAgent, "run", run)
    config = ForgeConfig(tmp_path / "missing-forge.yml")
    result = await _default_debug_runner(settings, config, jobs, logs, project_path="group/project")
    assert result is diagnosis
    constructed[0].project_config.review_rules.append("first call only")
    with pytest.raises(RuntimeError, match="pipeline-debugger agent failed: model failed"):
        await _default_debug_runner(settings, config, jobs, logs, project_path="group/project")
    assert len(constructed) == 2
    assert constructed[0].project_config is not constructed[1].project_config
