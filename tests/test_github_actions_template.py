"""The forge-harness Actions workflow template contract (E3b, ADR-0020).

The template is human-applied to the TARGET repo, so it must keep the exact
shape forge's executor depends on: workflow_dispatch with the four inputs,
the factory-branch ref, the entry-point invocation, and the candidate
artifact contract (name + files) the trusted publisher consumes.
"""

from pathlib import Path

import yaml

TEMPLATE = Path(__file__).parents[1] / "ci" / "templates" / "forge-harness.github.yml"


def load_template() -> dict:
    """Parse the template. YAML 1.1 (pyyaml) reads the `on:` key as the
    boolean True — normalize it away."""
    workflow = yaml.safe_load(TEMPLATE.read_text())
    workflow["_dispatch"] = (workflow["on"] if "on" in workflow else workflow[True])[
        "workflow_dispatch"
    ]
    return workflow


class TestWorkflowTemplateContract:
    def test_dispatch_surface_matches_the_executor(self):
        """What forge dispatches (forge.execution.github_actions.launch)
        must be exactly what the template declares."""
        workflow = load_template()
        dispatch = workflow["_dispatch"]

        assert workflow["name"] == "forge-harness"
        inputs = dispatch["inputs"]
        assert set(inputs) == {"run_id", "attempt_base_oid", "driver", "model"}
        # Strings only: workflow_dispatch inputs lose typing on the wire.
        assert all(spec["type"] == "string" for spec in inputs.values())
        assert inputs["run_id"]["required"] is True
        assert inputs["attempt_base_oid"]["required"] is True
        assert inputs["driver"]["required"] is True

    def test_proposal_only_lane_has_no_write_credentials(self):
        text = TEMPLATE.read_text()
        workflow = load_template()
        job = workflow["jobs"]["harness"]

        assert job["runs-on"] == "ubuntu-latest"  # ephemeral runner
        assert job["timeout-minutes"] <= 60
        assert "persist-credentials: false" in text  # nothing actionable left behind
        assert "git remote set-url --push origin FORBIDDEN" in text  # ADR-0016
        # No forge-side secrets ever reach the lane: only harness provider keys.
        for forbidden in ("FORGE_GITHUB_TOKEN", "GITLAB_TOKEN", "FORGE_BOT_TOKEN"):
            assert forbidden not in text

    def test_candidate_artifact_contract(self):
        """The artifact name and files are what the executor looks for."""
        text = TEMPLATE.read_text()
        workflow = load_template()
        upload = workflow["jobs"]["harness"]["steps"][-1]

        assert upload["uses"].startswith("actions/upload-artifact@v4")
        assert upload["with"]["name"] == "forge-candidate-${{ inputs.run_id }}"
        assert ".forge/candidate.diff" in upload["with"]["path"]
        assert ".forge/candidate.meta.json" in upload["with"]["path"]
        assert upload["if"] == "always()"  # the audit trail survives failures
        # The candidate diff is captured against the FROZEN attempt base.
        assert 'git diff --cached --binary --full-index "${{ inputs.attempt_base_oid }}"' in text

    def test_driver_step_runs_the_harness_entry_point(self):
        workflow = load_template()
        driver_step = next(
            step
            for step in workflow["jobs"]["harness"]["steps"]
            if step.get("name") == "Run harness driver"
        )

        run = driver_step["run"]
        assert "python -m forge.harness_entry" in run
        # The pin is a placeholder the onboarding must replace (never a
        # moving branch): docs/harness-onboarding.md, "GitHub Actions harness".
        assert "<PINNED_REF>" in run
        assert "git+https://github.com/" in run

    def test_concurrency_groups_one_run_per_forge_run(self):
        workflow = load_template()

        assert workflow["concurrency"]["group"] == "forge-${{ inputs.run_id }}"
        assert workflow["concurrency"]["cancel-in-progress"] is True
