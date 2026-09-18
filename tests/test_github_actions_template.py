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
        assert set(inputs) == {
            "run_id",
            "attempt_base_oid",
            "driver",
            "model",
            "issue_number",  # the lane fetches its brief from the issue
            # Repair re-dispatches only — an undeclared dispatch input is a
            # dispatch-wide 422 (LIVE-found on db5408f4).
            "repair_context",
        }
        # Strings only: workflow_dispatch inputs lose typing on the wire.
        assert all(spec["type"] == "string" for spec in inputs.values())
        for required in ("run_id", "attempt_base_oid", "driver", "issue_number"):
            assert inputs[required]["required"] is True

    def test_brief_is_rendered_from_the_issue_before_the_driver_runs(self):
        """The approved plan lives as the forge plan comment on the issue —
        the lane fetches it read-only and renders the shared brief. The
        plan TEXT never travels through dispatch inputs (size limits)."""
        workflow = load_template()
        steps = workflow["jobs"]["harness"]["steps"]
        brief_step = next(
            (step for step in steps if step.get("name") == "Render the implementation brief"),
            None,
        )

        assert brief_step is not None
        assert "python -m forge.harness_entry --render-brief" in brief_step["run"]
        assert brief_step["env"]["GITHUB_TOKEN"] == "${{ github.token }}"  # read-only
        assert brief_step["env"]["FORGE_ISSUE_NUMBER"] == "${{ inputs.issue_number }}"
        # The driver step runs AFTER the brief step and only runs the driver.
        names = [step.get("name") for step in steps]
        assert names.index("Render the implementation brief") < names.index("Run harness driver")

    def test_lane_is_read_only(self):
        workflow = load_template()

        assert workflow["jobs"]["harness"]["permissions"] == {
            "contents": "read",
            "issues": "read",
        }

    def test_proposal_only_lane_has_no_write_credentials(self):
        text = TEMPLATE.read_text()
        workflow = load_template()
        job = workflow["jobs"]["harness"]

        assert job["runs-on"] == "ubuntu-latest"  # ephemeral runner
        # Above the run-side FORGE_HARNESS_TIMEOUT_SECONDS (5400s = 90m) so
        # the governed deadline classifies honestly instead of GitHub
        # killing the job as "cancelled" (LIVE-found at 60m).
        assert job["timeout-minutes"] == 120
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
        # R16: the STAGED NON-HIDDEN directory is uploaded — v4.4.0+
        # upload-artifact excludes hidden files by default and the old
        # .forge/ paths are a dot-directory, so a direct upload finds zero
        # files and fails (if-no-files-found: error).
        assert ".forge-output" in upload["with"]["path"]
        assert ".forge/" not in upload["with"]["path"]
        assert upload["with"]["if-no-files-found"] == "error"
        assert upload["if"] == "always()"  # the audit trail survives failures
        # The candidate diff is captured against the FROZEN attempt base.
        assert 'git diff --cached --binary --full-index "${{ inputs.attempt_base_oid }}"' in text

    def test_emit_step_stages_a_clean_dir_and_builds_meta_v2_via_the_entry_point(self):
        """The emit step stages only the candidate diff into a fresh
        non-hidden directory and delegates the meta v2 build (identity,
        manifest digest, usage receipt) to the same pinned forge entry
        point — one schema, one test suite, never a heredoc copy."""
        workflow = load_template()
        steps = workflow["jobs"]["harness"]["steps"]
        emit = next(step for step in steps if step.get("name") == "Emit candidate artifact")

        assert emit["if"] == "always()"
        run = emit["run"]
        # Clean, non-hidden staging: nothing else can ride along.
        assert "rm -rf .forge-output && mkdir -p .forge-output" in run
        assert ".forge-output/candidate.diff" in run
        assert ".forge/candidate.diff" not in run
        # The meta is built by forge's entry point with the dispatched
        # identity (attempt identity rides from the runner's GITHUB_* env).
        assert "python -m forge.harness_entry --emit-meta" in run
        assert '--forge-run-id "${{ inputs.run_id }}"' in run
        assert '--attempt-base-oid "${{ inputs.attempt_base_oid }}"' in run
        assert '--driver "${{ inputs.driver }}"' in run
        assert '--model "${{ inputs.model }}"' in run
        names = [step.get("name") for step in steps]
        assert names.index("Emit candidate artifact") < names.index("Upload candidate")

    def test_staging_directory_is_excluded_from_the_working_tree(self):
        """The staging dir is lane infrastructure: locally excluded so it
        can never leak into a `git add -A`."""
        text = TEMPLATE.read_text()
        assert '.forge-output/" >> .git/info/exclude' in text

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
        # moving branch) — it lives on the pip install (brief step):
        # docs/harness-onboarding.md, "GitHub Actions harness".
        text = TEMPLATE.read_text()
        assert "<PINNED_REF>" in text
        assert "forge @ git+https://github.com/forcewake/forge@<PINNED_REF>" in text

    def test_concurrency_groups_one_run_per_forge_run(self):
        workflow = load_template()

        assert workflow["concurrency"]["group"] == "forge-${{ inputs.run_id }}"
        assert workflow["concurrency"]["cancel-in-progress"] is True
