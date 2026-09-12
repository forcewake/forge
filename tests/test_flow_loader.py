from pathlib import Path

import pytest
import yaml

from forge.flows.loader import FlowLoader
from forge.flows.models import FlowAction, FlowStep


def _write_flow_yaml(dir_path: Path, name: str, data: dict) -> Path:
    path = dir_path / f"{name}.yml"
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


@pytest.fixture()
def flows_dir(tmp_path):
    return tmp_path


@pytest.fixture()
def sample_flow_data():
    return {
        "name": "test-flow",
        "version": "1.0",
        "description": "A test flow",
        "trigger": {
            "command": "@forge /test-flow",
            "target": "merge_request",
        },
        "timeout": 600,
        "steps": [
            {
                "name": "review",
                "agent": "code-reviewer",
                "output_key": "review",
                "timeout": 120,
            },
            {
                "name": "report",
                "type": "action",
                "action": "post_comment",
                "params": {"body": "Done: {review.summary}"},
            },
        ],
    }


def test_load_valid_yaml(flows_dir, sample_flow_data):
    _write_flow_yaml(flows_dir, "test-flow", sample_flow_data)
    loader = FlowLoader(flows_dir)
    loader.load()

    flow = loader.get("test-flow")
    assert flow is not None
    assert flow.name == "test-flow"
    assert flow.version == "1.0"
    assert flow.timeout == 600
    assert flow.trigger.command == "@forge /test-flow"
    assert flow.trigger.target == "merge_request"


def test_step_type_detection(flows_dir, sample_flow_data):
    _write_flow_yaml(flows_dir, "test-flow", sample_flow_data)
    loader = FlowLoader(flows_dir)
    loader.load()

    flow = loader.get("test-flow")
    assert isinstance(flow.steps[0], FlowStep)
    assert isinstance(flow.steps[1], FlowAction)


def test_flow_step_fields(flows_dir, sample_flow_data):
    _write_flow_yaml(flows_dir, "test-flow", sample_flow_data)
    loader = FlowLoader(flows_dir)
    loader.load()

    step = loader.get("test-flow").steps[0]
    assert isinstance(step, FlowStep)
    assert step.name == "review"
    assert step.agent == "code-reviewer"
    assert step.output_key == "review"
    assert step.timeout == 120
    assert step.on_failure == "abort"


def test_flow_action_fields(flows_dir, sample_flow_data):
    _write_flow_yaml(flows_dir, "test-flow", sample_flow_data)
    loader = FlowLoader(flows_dir)
    loader.load()

    action = loader.get("test-flow").steps[1]
    assert isinstance(action, FlowAction)
    assert action.action == "post_comment"
    assert action.params["body"] == "Done: {review.summary}"


def test_get_by_command(flows_dir, sample_flow_data):
    _write_flow_yaml(flows_dir, "test-flow", sample_flow_data)
    loader = FlowLoader(flows_dir)
    loader.load()

    flow = loader.get_by_command("/test-flow")
    assert flow is not None
    assert flow.name == "test-flow"


def test_get_by_command_not_found(flows_dir, sample_flow_data):
    _write_flow_yaml(flows_dir, "test-flow", sample_flow_data)
    loader = FlowLoader(flows_dir)
    loader.load()

    assert loader.get_by_command("/nonexistent") is None


def test_all_flows(flows_dir, sample_flow_data):
    _write_flow_yaml(flows_dir, "test-flow", sample_flow_data)
    sample_flow_data_2 = {
        **sample_flow_data,
        "name": "flow-2",
        "trigger": {"command": "@forge /flow2", "target": "issue"},
    }
    _write_flow_yaml(flows_dir, "flow-2", sample_flow_data_2)

    loader = FlowLoader(flows_dir)
    loader.load()

    assert len(loader.all()) == 2


def test_commands_frozenset(flows_dir, sample_flow_data):
    _write_flow_yaml(flows_dir, "test-flow", sample_flow_data)
    loader = FlowLoader(flows_dir)
    loader.load()

    cmds = loader.commands()
    assert isinstance(cmds, frozenset)
    assert "/test-flow" in cmds


def test_invalid_yaml_skipped(flows_dir, sample_flow_data):
    _write_flow_yaml(flows_dir, "good-flow", sample_flow_data)

    # Write an invalid YAML file (missing required 'name' key)
    bad_path = flows_dir / "bad-flow.yml"
    with open(bad_path, "w") as f:
        f.write("steps:\n  - no_name_field: true\n")

    loader = FlowLoader(flows_dir)
    loader.load()

    # Good flow should be loaded, bad one skipped
    assert loader.get("good-flow") is None  # missing name
    assert len(loader.all()) == 1  # only the good flow


def test_empty_directory(tmp_path):
    loader = FlowLoader(tmp_path)
    loader.load()
    assert loader.all() == []


def test_nonexistent_directory():
    loader = FlowLoader("/nonexistent/path")
    loader.load()
    assert loader.all() == []


def test_condition_on_step(flows_dir):
    data = {
        "name": "cond-flow",
        "version": "1.0",
        "description": "Flow with condition",
        "trigger": {"command": "@forge /cond", "target": "merge_request"},
        "steps": [
            {
                "name": "conditional_step",
                "agent": "chat",
                "condition": "review.severity != 'critical'",
                "output_key": "result",
            },
        ],
    }
    _write_flow_yaml(flows_dir, "cond-flow", data)
    loader = FlowLoader(flows_dir)
    loader.load()

    step = loader.get("cond-flow").steps[0]
    assert isinstance(step, FlowStep)
    assert step.condition == "review.severity != 'critical'"
