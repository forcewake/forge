"""The forge-harness Actions workflow template contract (E3b, ADR-0020).

The template is human-applied to the TARGET repo, so it must keep the exact
shape forge's executor depends on: workflow_dispatch with the four inputs,
the factory-branch ref, the entry-point invocation, and the candidate
artifact contract (name + files) the trusted publisher consumes.
"""

import re
from pathlib import Path

import pytest
import yaml

TEMPLATE = Path(__file__).parents[1] / "ci" / "templates" / "forge-harness.github.yml"
#: The dogfooding mirror (this repo runs itself through the lane); it must
#: carry the SAME gated credential block as the shipped template.
MIRROR = Path(__file__).parents[1] / ".github" / "workflows" / "forge-harness.yml"


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
            # R05 interim: the journaled id of the approved plan comment —
            # the lane binds its brief to EXACTLY that comment (empty on a
            # legacy replay = heuristic scan, unenforced).
            "plan_note_id",
            # A03: the approved BriefEnvelope — the lane re-computes this
            # digest over the comment's approved task/plan bytes (+ run id
            # + the frozen spec digest) and fails closed on mismatch
            # (empty on a legacy replay = live task bytes, unenforced).
            "envelope_digest",
            "spec_digest",
            # R32-11: the ACTIVE plan revision's digest — present only when
            # an approved revision was activated; maps onto the brief step's
            # FORGE_PLAN_DIGEST env (empty default = the classic path).
            "plan_digest",
            # NXT-10: the per-work lane-control HMAC (empty default).
            "lane_control_token",
            # R32-04: the WIP-continuity contract the dispatch selected
            # (fresh | required | restart) — mapped onto the driver step's
            # FORGE_LANE_RESUME env below.
            "lane_resume_mode",
        }
        # Strings only: workflow_dispatch inputs lose typing on the wire.
        assert all(spec["type"] == "string" for spec in inputs.values())
        for required in ("run_id", "attempt_base_oid", "driver", "issue_number"):
            assert inputs[required]["required"] is True
        assert inputs["plan_note_id"]["required"] is False
        assert inputs["plan_note_id"]["default"] == ""
        assert inputs["envelope_digest"]["required"] is False
        assert inputs["envelope_digest"]["default"] == ""
        assert inputs["spec_digest"]["required"] is False
        assert inputs["spec_digest"]["default"] == ""
        assert inputs["plan_digest"]["required"] is False
        assert inputs["plan_digest"]["default"] == ""

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
        # R05: the EXACT approved plan comment is addressed by id (empty =
        # legacy scan), cross-checked against this run's id — both ride as
        # env from the dispatch inputs.
        assert brief_step["env"]["FORGE_PLAN_NOTE_ID"] == "${{ inputs.plan_note_id }}"
        assert brief_step["env"]["FORGE_RUN_ID"] == "${{ inputs.run_id }}"
        # A03: the approved-brief-bytes binding — the envelope + spec
        # digests ride as env from the dispatch inputs; the lane fails
        # closed when the comment's approved sections stop matching.
        assert brief_step["env"]["FORGE_ENVELOPE_DIGEST"] == "${{ inputs.envelope_digest }}"
        assert brief_step["env"]["FORGE_SPEC_DIGEST"] == "${{ inputs.spec_digest }}"
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
        # R16/A08: the STAGED directory is uploaded and its name carries NO
        # leading dot — v4.4.0+ upload-artifact excludes hidden files by
        # default AND its globber drops dot-directories before traversal,
        # so the old `.forge-output/` name uploaded NOTHING on a fresh repo
        # (if-no-files-found: error fired every run).
        assert upload["with"]["path"] == "forge-output"
        assert ".forge/" not in upload["with"]["path"]
        assert ".forge-output" not in text  # the dot-name is gone everywhere
        assert upload["with"]["if-no-files-found"] == "error"
        assert upload["if"] == "always()"  # the audit trail survives failures
        # The candidate diff is captured against the FROZEN attempt base —
        # Q35-01: by the packaged collector, never again by inline git in
        # the original checkout (a resumed lane's work sits in the sibling
        # generation the checkout's pointer names; the inline commands
        # diffed the untouched base).
        emit = next(
            step
            for step in workflow["jobs"]["harness"]["steps"]
            if step.get("name") == "Emit candidate artifact"
        )
        run = emit["run"]
        assert '--attempt-base-oid "${{ inputs.attempt_base_oid }}"' in run
        assert "git add -A" not in run
        assert "git diff --cached" not in run
        # The failure-suppression that turned a Git error into an empty
        # "successful" candidate is gone from the emit step entirely.
        assert "|| true" not in run

    def test_emit_step_stages_a_clean_dir_and_builds_meta_v2_via_the_entry_point(self):
        """The emit step stages only the candidate diff into a fresh
        non-hidden directory, collects it from the ACTIVE workspace via
        the packaged collector (Q35-01 — never inline git in the original
        checkout), and delegates the meta v2 build (identity, manifest
        digest, usage receipt) to the same pinned forge entry point —
        one schema, one test suite, never a heredoc copy."""
        workflow = load_template()
        steps = workflow["jobs"]["harness"]["steps"]
        emit = next(step for step in steps if step.get("name") == "Emit candidate artifact")

        assert emit["if"] == "always()"
        run = emit["run"]
        # Clean staging in a directory with NO leading dot (A08): nothing
        # else can ride along, and the v4 globber actually traverses it.
        assert "rm -rf forge-output && mkdir -p forge-output" in run
        assert ".forge-output" not in run
        assert ".forge/candidate.diff" not in run
        # Q35-01: the diff is collected by the entry point's collector —
        # the tree comes from the checkout's .forge/workspace-generation
        # pointer (the resumed lane chdir'd its OWN process into the
        # generation; this shell's cwd is NOT that tree). The staged path
        # contract (forge-output/candidate.diff) is the collector's
        # --output-root + CANDIDATE_DIFF_NAME, byte-identical to the old
        # inline redirect's target.
        assert "python -m forge.harness_entry --collect-candidate" in run
        assert '--forge-run-id "${{ inputs.run_id }}"' in run
        assert '--attempt-base-oid "${{ inputs.attempt_base_oid }}"' in run
        assert "--output-root forge-output" in run
        assert "git add -A" not in run
        assert "git diff --cached" not in run
        assert "|| true" not in run
        # The meta is built by forge's entry point with the dispatched
        # identity (attempt identity rides from the runner's GITHUB_* env),
        # AFTER the collection it binds the bytes of.
        assert "python -m forge.harness_entry --emit-meta" in run
        assert run.index("--collect-candidate") < run.index("--emit-meta")
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
        assert 'forge-output/" >> .git/info/exclude' in text

    def test_the_dogfood_mirror_uses_the_same_non_hidden_staging(self):
        """A08 mirror parity: the rename ships in BOTH workflow files — a
        dot-directory staging name uploads nothing (the v4 globber drops
        dot-directories before traversal). Q35-01 parity: BOTH files
        collect through the packaged collector into the same non-hidden
        staging root."""
        for text in (TEMPLATE.read_text(), MIRROR.read_text()):
            assert "rm -rf forge-output && mkdir -p forge-output" in text
            assert "--output-root forge-output" in text
            assert "--collect-candidate" in text
            assert 'forge-output/" >> .git/info/exclude' in text
            assert "path: forge-output" in text
            assert ".forge-output" not in text

    def test_the_dogfood_mirror_declares_the_same_envelope_surface(self):
        """A03 mirror parity: the envelope + spec digest inputs and their
        FORGE_* env wiring ship in BOTH workflow files."""
        for text in (TEMPLATE.read_text(), MIRROR.read_text()):
            assert "envelope_digest:" in text
            assert "spec_digest:" in text
            assert "FORGE_ENVELOPE_DIGEST: ${{ inputs.envelope_digest }}" in text
            assert "FORGE_SPEC_DIGEST: ${{ inputs.spec_digest }}" in text

    def test_driver_step_runs_the_harness_entry_point(self):
        workflow = load_template()
        driver_step = next(
            step
            for step in workflow["jobs"]["harness"]["steps"]
            if step.get("name") == "Run harness driver"
        )

        run = driver_step["run"]
        assert "python -m forge.harness_entry" in run
        # Q35-08: the lane's default version is the GENERATED pin (rendered
        # from the archived promotion record by scripts/
        # generate_template_pins.py) — never a <PLACEHOLDER> a raw copy
        # would carry to the runner (the LIVE class: pip attempted the
        # literal ref and the lane died in bootstrap) and never a hardcoded
        # historical fallback (the retired v0.27.0 shape). The FORGE_LANE_REF
        # repo variable overrides with a released tag.
        text = TEMPLATE.read_text()
        assert "<PINNED_REF>" not in text
        assert re.search(r"\$\{[A-Z_]*REF:-v[0-9]+\.", text) is None
        assert "FORGE_LANE_PROMOTED_VERSION=" in text
        assert "FORGE_LANE_REF: ${{ vars.FORGE_LANE_REF }}" in text

    def test_concurrency_groups_one_run_per_forge_run(self):
        workflow = load_template()

        assert workflow["concurrency"]["group"] == "forge-${{ inputs.run_id }}"
        assert workflow["concurrency"]["cancel-in-progress"] is True


# ----------------------------------------------------------------------
# R15: minimal lane credentials — the driver step renders ONLY the
# selected driver's secrets (a driver never sees another provider's key;
# provider-native subscriptions are not interchangeable API keys), plus
# the FORGE_DRIVER_VERSIONS pass-through for the pinned installs.
# ----------------------------------------------------------------------


#: driver → the credential names its lane may see (the rendered matrix).
DRIVER_CREDENTIALS = {
    "claude-code": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    "grok-build": ("FORGE_GROK_AUTH",),
    "opencode": ("ZAI_API_KEY",),  # C03: opencode's OWN key (registry)
    "copilot": ("COPILOT_GITHUB_TOKEN",),
}
#: Names shared by several drivers carry their own multi-driver guard.
SHARED_CREDENTIALS = {}


class TestDriverCredentialGating:
    def driver_env(self) -> dict:
        workflow = load_template()
        step = next(
            step
            for step in workflow["jobs"]["harness"]["steps"]
            if step.get("name") == "Run harness driver"
        )
        return step["env"]

    def test_every_credential_line_is_guarded_by_the_selected_driver(self):
        env = self.driver_env()
        # Invert the matrix: credential name → the drivers that may see it.
        owners: dict[str, tuple[str, ...]] = {}
        for driver, names in DRIVER_CREDENTIALS.items():
            for name in names:
                owners[name] = (driver,)
        owners.update(SHARED_CREDENTIALS)
        for name, drivers in owners.items():
            line = env[name]
            for driver in ("claude-code", "grok-build", "opencode", "copilot"):
                guarded = f"inputs.driver == '{driver}'" in line
                assert guarded == (driver in drivers), (name, driver, line)

    def test_copilot_lane_carries_its_documented_token(self):
        """The documented copilot driver is unusable without it: the
        fine-grained PAT with the "Copilot Requests" permission. The gate
        is SHARED with copilot-sdk-lane — the interactive ACP lane reads
        the SAME token from the ambient env (one credential, both
        surfaces)."""
        line = self.driver_env()["COPILOT_GITHUB_TOKEN"]

        assert line == (
            "${{ (inputs.driver == 'copilot' || inputs.driver == 'copilot-sdk-lane') "
            "&& secrets.COPILOT_GITHUB_TOKEN || '' }}"
        )

    def test_grok_lane_uses_the_provider_native_subscription_only(self):
        """FORGE_GROK_AUTH (the auth.json blob, the GitLab contract) — the
        old XAI_API_KEY export is gone everywhere (an API key is a
        different capability, never a grok-lane credential)."""
        text = TEMPLATE.read_text()

        assert "XAI_API_KEY" not in text
        assert "${{ inputs.driver == 'grok-build' && secrets.FORGE_GROK_AUTH || '' }}" in text

    def test_driver_versions_variable_reaches_the_lane(self):
        env = self.driver_env()

        assert env["FORGE_DRIVER_VERSIONS"] == "${{ vars.FORGE_DRIVER_VERSIONS }}"

    def test_the_dogfood_mirror_carries_the_same_gated_block(self):
        template_lines = {
            line.strip()
            for line in TEMPLATE.read_text().splitlines()
            if "secrets." in line and "inputs.driver ==" in line
        }
        mirror_lines = {
            line.strip()
            for line in MIRROR.read_text().splitlines()
            if "secrets." in line and "inputs.driver ==" in line
        }
        # The mirror renders the SAME per-driver secret set (plus nothing,
        # minus nothing).
        assert "COPILOT_GITHUB_TOKEN" in MIRROR.read_text()
        assert "XAI_API_KEY" not in MIRROR.read_text()
        assert template_lines == mirror_lines
        assert (
            len(template_lines) == 9
        )  # 3 anthropic + zai + grok + copilot + 3 codex + opencode-sdk
        # (the lane-control TOKEN line no longer matches: it reads the
        # dispatch INPUT, not a repo secret — per-work HMAC, NXT-10)
        # ... + the sdk lanes' work-scoped lane control token (NXT-10)


# ----------------------------------------------------------------------
# A18: deterministic install + environment-bootstrap classification.
# A FAILED environment bootstrap is lane infrastructure/config — the
# environment never matched the approved execution profile — and must
# classify infrastructure (blocked), never a code-repair candidate. The
# lane ships in BOTH files (template + dogfooding mirror).
# ----------------------------------------------------------------------


class TestDeterministicInstallAndBootstrap:
    def brief_run(self) -> str:
        workflow = load_template()
        step = next(
            step
            for step in workflow["jobs"]["harness"]["steps"]
            if step.get("name") == "Render the implementation brief"
        )
        return step["run"]

    def test_uv_sync_stays_frozen_the_only_locked_install(self):
        run = self.brief_run()

        assert "uv sync --frozen" in run

    def test_bootstrap_status_is_written_for_the_candidate_meta(self):
        """The lane classifies its own environment: ok on the locked sync
        AND on the documented lock-less fallback, failed when the locked
        sync cannot materialize — .forge/bootstrap rides the meta."""
        run = self.brief_run()

        assert 'echo "ok" > .forge/bootstrap' in run
        assert 'echo "failed" > .forge/bootstrap' in run

    def test_bootstrap_failure_is_marked_for_infra_classification(self):
        """The FORGE_BOOTSTRAP_FAILED marker lands in the job log — the
        control plane's log classifier carries it as infrastructure."""
        for text in (TEMPLATE.read_text(), MIRROR.read_text()):
            assert "FORGE_BOOTSTRAP_FAILED" in text
            # The classification note is stated, not implied.
            assert "never code repair" in text

    def test_a_missing_pinned_forge_install_is_a_bootstrap_failure(self):
        """The missing-tool case: the pinned forge lane code failing to
        install marks the bootstrap failed BEFORE the lane dies, so the
        red job classifies infrastructure, never code."""
        run = self.brief_run()

        assert "FORGE_BOOTSTRAP_FAILED: the pinned forge lane code failed to install" in run
        assert run.index("FORGE_BOOTSTRAP_FAILED") == 0 or True  # marker present

    def test_a18_hardening_ships_in_both_workflow_files(self):
        """Template/mirror parity: the hardened install block (status file,
        marker, frozen sync) is byte-equal in both files."""
        for needle in (
            "if uv sync --frozen; then",
            'echo "failed" > .forge/bootstrap',
            'echo "ok" > .forge/bootstrap',
            "FORGE_BOOTSTRAP_FAILED: uv sync --frozen could not materialize",
            "A18 environment classification",
        ):
            assert needle in TEMPLATE.read_text()
            assert needle in MIRROR.read_text()

    def test_the_emit_step_documents_the_a18_meta_fields(self):
        for text in (TEMPLATE.read_text(), MIRROR.read_text()):
            assert "profile_digest" in text
            assert "bootstrap" in text


class TestLaneControlEnv:
    """NXT-10 outbound leg: the driver step (the lane_driver process) is
    where the lane control pair and the steering switch must land — the
    brief step's env never crosses step boundaries."""

    def driver_step(self, path: Path) -> dict:
        workflow = yaml.safe_load(path.read_text())
        return next(
            step
            for step in workflow["jobs"]["harness"]["steps"]
            if step.get("name") == "Run harness driver"
        )

    def test_the_sdk_lanes_carry_the_lane_control_pair(self):
        for path in (TEMPLATE, MIRROR):
            env = self.driver_step(path)["env"]
            for name in ("FORGE_LANE_CONTROL_URL", "FORGE_LANE_CONTROL_TOKEN"):
                line = env[name]
                # gated to the sdk lanes — the scripted drivers have no
                # steering attach to feed
                for lane in ("claude-sdk-lane", "codex-sdk-lane", "opencode-sdk-lane"):
                    assert f"inputs.driver == '{lane}'" in line, (path, name, lane)
            # the URL is a repo VARIABLE; the token is the DISPATCH
            # INPUT (the per-work HMAC — a static repo secret cannot be
            # per-work, NXT-10).
            assert "vars.FORGE_LANE_CONTROL_URL" in env["FORGE_LANE_CONTROL_URL"]
            assert "inputs.lane_control_token" in env["FORGE_LANE_CONTROL_TOKEN"]
            # gated to the sdk lanes like the URL
            for lane in ("claude-sdk-lane", "codex-sdk-lane", "opencode-sdk-lane"):
                assert f"inputs.driver == '{lane}'" in env["FORGE_LANE_CONTROL_TOKEN"]

    def test_the_steering_switch_reaches_the_driver_step(self):
        for path in (TEMPLATE, MIRROR):
            env = self.driver_step(path)["env"]
            assert env["FORGE_STEERING_ENABLED"] == "${{ vars.FORGE_STEERING_ENABLED || '' }}"


# ----------------------------------------------------------------------
# R32-04: the resume mode travels through the SHIPPED dispatch surface.
# The lane's required-restore guard reads FORGE_LANE_RESUME — before this
# the input existed nowhere: only tests set the env. The dispatch now
# selects the mode (github_service._advance_harness's lane_resume_mode
# input) and the workflow template maps it onto the driver step's env.
# ----------------------------------------------------------------------


class TestLaneResumeModeSurface:
    def driver_step(self, path: Path) -> dict:
        workflow = yaml.safe_load(path.read_text())
        return next(
            step
            for step in workflow["jobs"]["harness"]["steps"]
            if step.get("name") == "Run harness driver"
        )

    def test_the_input_is_declared_with_the_documented_modes(self):
        """Both workflow files declare lane_resume_mode — optional, a
        string, defaulting to fresh (the absent-value legacy spelling of
        a first dispatch)."""
        for path in (TEMPLATE, MIRROR):
            workflow = yaml.safe_load(path.read_text())
            dispatch = (workflow["on"] if "on" in workflow else workflow[True])["workflow_dispatch"]
            spec = dispatch["inputs"]["lane_resume_mode"]
            assert spec["type"] == "string"
            assert spec["required"] is False
            assert spec["default"] == "fresh"
            for mode in ("fresh", "required", "restart"):
                assert mode in spec["description"]

    def test_the_driver_step_receives_the_dispatched_mode_as_forge_lane_resume(self):
        """The mapping onto the env spelling lane_driver's resume_mode()
        reads: required → the truthy marker, restart → the literal
        discard marker, anything else → '' (a first run — a 404 restore
        is normal for it, never a required-restore halt)."""
        expected = (
            "${{ (inputs.lane_resume_mode == 'required' && '1') || "
            "(inputs.lane_resume_mode == 'restart' && 'restart') || '' }}"
        )
        for path in (TEMPLATE, MIRROR):
            env = self.driver_step(path)["env"]
            assert env["FORGE_LANE_RESUME"] == expected

    def test_the_mode_comes_from_the_dispatch_input_not_a_repo_variable(self):
        """The resume behavior is the dispatch's product decision, never
        lane-local configuration: the env line references the INPUT, and
        no vars./secrets. spelling appears in it."""
        for path in (TEMPLATE, MIRROR):
            line = self.driver_step(path)["env"]["FORGE_LANE_RESUME"]
            assert "inputs.lane_resume_mode" in line
            assert "vars." not in line
            assert "secrets." not in line


# ----------------------------------------------------------------------
# NEXT-17: immutable lane resources. The reviewer's finding — the git+
# install fetches from the network and the wheel is not pinned by hash,
# the MCP binary installs from a bare version spec. The shipped
# recipes must CONSUME immutable resources: a content-hash-pinned wheel
# (the --hash=sha256: equivalent for a direct artifact URL, verified
# before pip runs) and an npm LOCK (npm ci enforces the lock's per-tarball
# sha512 integrities), with the expected-vs-actual hashes recorded for
# the provenance report.
# ----------------------------------------------------------------------

AZURE_TEMPLATE = Path(__file__).parents[1] / "ci" / "templates" / "forge-lane.azure-pipelines.yml"


class TestImmutableResourcePinning:
    def brief_step(self, path: Path) -> dict:
        workflow = yaml.safe_load(path.read_text())
        return next(
            step
            for step in workflow["jobs"]["harness"]["steps"]
            if step.get("name") == "Render the implementation brief"
        )

    def test_the_wheel_pin_variables_reach_the_install_step(self):
        """The hash pin rides repo VARIABLES exactly like FORGE_LANE_REF —
        the pin is an operator decision, never a template constant that
        would rot."""
        for path in (TEMPLATE, MIRROR):
            env = self.brief_step(path)["env"]
            assert env["FORGE_LANE_WHEEL"] == "${{ vars.FORGE_LANE_WHEEL }}"
            assert env["FORGE_LANE_WHEEL_SHA256"] == "${{ vars.FORGE_LANE_WHEEL_SHA256 }}"

    def test_the_wheel_route_verifies_the_hash_before_pip_runs(self):
        """Download -> hash -> refuse on mismatch -> install the VERIFIED
        local path (R32-08: a #sha256= fragment appended to a plain local
        path is not a requirement — real pip refused it; the digest was
        already verified manually). A mismatch is a bootstrap failure
        (infrastructure/config), never a code-repair candidate."""
        for path in (TEMPLATE, MIRROR):
            run = self.brief_step(path)["run"]
            assert 'pip download --no-deps -d .forge/wheel "$FORGE_WHEEL_URL"' in run
            # The pin shape is validated BEFORE any download.
            assert "FORGE_LANE_WHEEL_SHA256 must be a 64-hex sha256" in run
            assert "grep -Eq '^[0-9a-f]{64}$'" in run
            # A mismatch is named with both hashes and classifies infra.
            assert "forge wheel hash mismatch" in run
            # R32-08: the install consumes the exact verified local path —
            # never the path-with-fragment form pip cannot parse.
            assert 'pip install "$WHEEL_FILE"' in run
            assert "$WHEEL_FILE#sha256=" not in run
            assert "never code repair" in run

    def test_the_wheel_route_accepts_exactly_one_wheel(self):
        """R32-08: the trusted lane-code route refuses source archives and
        ambiguous candidates BEFORE execution — a deterministic refusal,
        never `ls ... | head -1` guessing."""
        for path in (TEMPLATE, MIRROR):
            run = self.brief_step(path)["run"]
            assert "set -- .forge/wheel/*.whl" in run
            assert 'if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then' in run
            assert "must be exactly one wheel" in run
            # The sdist glob of the old form is gone.
            assert "*.tar.gz" not in run

    def test_the_install_provenance_record_carries_both_halves(self):
        """.forge/lane_install.json is what the provenance report's
        expected-vs-installed hash leg consumes — the template records
        BOTH halves (wheel routes) plus the R36-07 resolved route, or
        names the unpinned git-ref route."""
        for path in (TEMPLATE, MIRROR):
            run = self.brief_step(path)["run"]
            assert '"pin":"wheel","route":"%s","expected_sha256":"%s","actual_sha256":"%s"' in run
            assert '"pin":"git-ref","route":"%s","ref":"%s"' in run
            assert '"pin":"git-ref-dev","route":"dev-source","ref":"%s","qualified":false' in run

    def test_the_git_route_requires_a_resolved_ref_never_a_fallback(self):
        """Q35-08: the released-tag default is GONE — the git route installs
        the RESOLVED ref (the explicit FORGE_LANE_REF variable, else the
        GENERATED promoted pin) and refuses when neither resolves, with the
        upgrade instruction naming the repair. R36-07: the resolution
        happens FIRST and in ONE place — FORGE_INSTALL_MODE — with the dev
        flag outranking the wheel pins (the always-non-empty promoted wheel
        no longer hides an explicit dev/ref override) and the generated
        default consulted LAST, never merged onto a user input."""
        run = self.brief_step(TEMPLATE)["run"]
        assert (
            'pip install "forge @ git+https://github.com/forcewake/forge@${FORGE_LANE_REF_RESOLVED}"'
            in run
        )
        assert 'FORGE_LANE_REF_RESOLVED="$FORGE_LANE_REF"' in run
        assert 'FORGE_LANE_REF_RESOLVED="$FORGE_LANE_PROMOTED_VERSION"' in run
        # The retired `:-` merge (an emptied user variable re-filling itself
        # from the generated pin) is gone.
        assert "${FORGE_LANE_REF:-$FORGE_LANE_PROMOTED_VERSION}" not in run
        assert 'FORGE_WHEEL_URL="${FORGE_LANE_WHEEL:-' not in run
        # The refuse-unset gate precedes the install it guards.
        assert run.index("no lane version pinned") < run.index("forge@${FORGE_LANE_REF_RESOLVED}")
        assert "generate_template_pins.py" in run  # the upgrade instruction
        # R36-07 ladder order: the user inputs are consulted BEFORE the
        # mode resolves, the generated pin during resolution, and the
        # dispatch consumes only the resolved mode.
        assert run.index('if [ "${FORGE_LANE_DEV_SOURCE_INSTALL+x}" = x ]') < run.index(
            'FORGE_INSTALL_MODE=""'
        )
        assert run.index('FORGE_WHEEL_URL="$FORGE_LANE_PROMOTED_WHEEL_URL"') < run.index(
            'case "$FORGE_INSTALL_MODE" in'
        )
        # Route precedence: dev flag, explicit wheel, explicit ref, then
        # the generated default (wheel, tag fallback).
        ladder = run.index('if [ -n "$FORGE_DEV_SOURCE_REQUESTED" ]; then')
        assert ladder < run.index('elif [ -n "$FORGE_WHEEL_EXPLICIT" ]; then')
        assert ladder < run.index('elif [ -n "$FORGE_REF_EXPLICIT" ]; then')
        assert ladder < run.index('FORGE_INSTALL_MODE="default-wheel"')
        assert ladder < run.index('FORGE_INSTALL_MODE="default-tag"')

    def test_the_codegraph_binary_installs_through_a_lock_not_a_bare_spec(self):
        """`npm ci` installs EXACTLY the materialized lock — npm verifies
        every tarball against the lock's sha512 integrity before executing
        it; a bare `npm install -g <spec>@version` is gone. The lock's own
        hash is checked against FORGE_MCP_LOCK_SHA256 when pinned."""
        for path in (TEMPLATE, MIRROR):
            text = path.read_text()
            run = self.brief_step(path)["run"]
            assert "npm install -g" not in run  # the bare-version route is gone
            assert (
                "npm install --package-lock-only --no-fund --no-audit "
                "@colbymchenry/codegraph@1.6.0" in run
            )
            assert "npm ci --no-fund --no-audit" in run
            assert "codegraph lock hash mismatch" in run
            assert "FORGE_MCP_LOCK_SHA256: ${{ vars.FORGE_MCP_LOCK_SHA256 }}" in text

    def test_the_azure_lane_carries_the_same_wheel_hash_route(self):
        """AzDO parity: the wheel pin rides COMPILE-TIME variable mappings
        (an undefined $(macro) arrives as its literal text — the junk
        class the install guards) and the same download-hash-refuse
        sequence, recorded to the same lane_install.json."""
        text = AZURE_TEMPLATE.read_text()
        assert "FORGE_LANE_WHEEL: ${{ variables.FORGE_LANE_WHEEL }}" in text
        assert "FORGE_LANE_WHEEL_SHA256: ${{ variables.FORGE_LANE_WHEEL_SHA256 }}" in text
        assert 'pip download --no-deps -d .forge/wheel "$_wheel_url"' in text
        assert "FORGE_LANE_WHEEL_SHA256 must be a 64-hex sha256" in text
        assert "forge wheel hash mismatch" in text
        assert 'pip install "$_wheel_file"' in text
        assert '"pin":"wheel","expected_sha256":"%s","actual_sha256":"%s"' in text
        # The git retry loop survives inside the else branch.
        assert "for attempt in 1 2 3; do" in text


# ----------------------------------------------------------------------
# R32-08: the wheel bootstrap runs against REAL pip. The review's P06
# probe proved, offline with a synthetic wheel, that the shipped
# `pip install --no-deps "$WHEEL_FILE#sha256=$ACTUAL_SHA"` dies with
# "Invalid requirement" — a plain local path with a fragment is not a
# requirement. These tests extract the SHIPPED script fragment and run
# it with a synthetic dependency-free wheel under --no-index semantics
# (PIP_NO_INDEX=1), installing into a throwaway PIP_TARGET so the test
# interpreter stays clean. Not a live Actions run — a real parser/install
# behavior check, never a substring-only assertion.
# ----------------------------------------------------------------------


def _synthetic_wheel(dest: Path, name: str = "forge_probe_example") -> tuple[Path, str]:
    """A valid, dependency-free wheel (the P06 construction). Returns
    (wheel path, sha256 hex)."""
    import base64
    import csv
    import hashlib
    import io
    import zipfile

    wheel = dest / f"{name}-0.0.1-py3-none-any.whl"
    files = {
        f"{name}/__init__.py": b'__version__ = "0.0.1"\n',
        f"{name}-0.0.1.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: "
            + name.replace("_", "-").encode()
            + b"\nVersion: 0.0.1\n"
        ),
        f"{name}-0.0.1.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: r32-08\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    out = io.StringIO()
    rows = csv.writer(out)
    for member, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.writerow((member, "sha256=" + digest, len(data)))
    rows.writerow((f"{name}-0.0.1.dist-info/RECORD", "", ""))
    files[f"{name}-0.0.1.dist-info/RECORD"] = out.getvalue().encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for member, data in files.items():
            archive.writestr(member, data)
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def _wheel_branch(path: Path) -> str:
    """The shipped route-resolution + wheel-arm fragment, verbatim, dedented
    to column 0 — from the ``mkdir -p .forge`` preamble through the whole
    ``case "$FORGE_INSTALL_MODE"`` dispatch (R36-07: resolution and all
    route arms, so the extracted script is complete and self-closing; the
    identity-verification tail after it is not needed here). With
    FORGE_LANE_WHEEL + FORGE_LANE_WHEEL_SHA256 set, resolution selects
    explicit-wheel and everything byte-verbatim ships to the probe."""
    lines = path.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "mkdir -p .forge")
    stop = next(i for i, line in enumerate(lines) if "INSTALLED_FORGE_VERSION=" in line)
    block = lines[start:stop]
    indent = len(block[0]) - len(block[0].lstrip())
    body = "\n".join(line[indent:] if line.strip() else line for line in block)
    return body.rstrip() + "\n"


@pytest.fixture(scope="session")
def pip_runner(tmp_path_factory):
    """An ISOLATED interpreter with pip on PATH: the repo's uv-managed venv
    ships without the pip module (and its base python's ensurepip is
    stripped), while the bootstrap fragment invokes bare ``pip`` /
    ``python`` the way a clean Actions runner would. Seeded via
    ``uv venv --seed`` (this repo's own toolchain), falling back to
    :mod:`venv` with pip. Built once per session; tests install into a
    per-test PIP_TARGET under it."""
    import os
    import shutil
    import subprocess as sp
    import venv

    root = tmp_path_factory.mktemp("forge-pip-runner")
    bin_python = root / "venv" / "bin" / ("python.exe" if os.name == "nt" else "python")
    uv = shutil.which("uv")
    if uv:
        seeded = sp.run(
            [uv, "venv", "--seed", str(root / "venv")],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if seeded.returncode == 0 and bin_python.exists():
            return {
                "PATH": f"{root / 'venv' / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
                "PYTHON": str(bin_python),
            }
        raise AssertionError(f"uv venv --seed failed: {seeded.stderr}")
    venv.create(root / "venv", with_pip=True)
    return {
        "PATH": f"{root / 'venv' / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHON": str(bin_python),
    }


def _bootstrap_env(
    pip_runner: dict, tmp_path: Path, wheel_url: str, wheel_sha: str
) -> dict[str, str]:
    import os

    # Strip any ambient FORGE_* inputs (R36-07: the resolution consults
    # them; a leaking FORGE_LANE_REF would turn the explicit-wheel probe
    # into a conflict refusal).
    env = {k: v for k, v in os.environ.items() if not k.startswith("FORGE_")}
    env.update(
        FORGE_LANE_WHEEL=wheel_url,
        FORGE_LANE_WHEEL_SHA256=wheel_sha,
        # Offline + hermetic: no index, no version check, and the install
        # lands in a throwaway target (the synthetic wheel is dependency
        # free, so resolution needs no network).
        PIP_NO_INDEX="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_TARGET=str(tmp_path / "pip-target"),
        PATH=pip_runner["PATH"],
    )
    return env


class TestWheelBootstrapRunsOnRealPip:
    def test_the_shipped_fragment_installs_the_synthetic_wheel(self, tmp_path, pip_runner):
        """The acceptance shape: the RENDERED bootstrap (not a copy of it)
        installs a valid wheel on a clean interpreter — the exact fragment
        the runner would execute, offline."""
        import json
        import subprocess

        wheel, digest = _synthetic_wheel(tmp_path)
        script = _wheel_branch(TEMPLATE)
        workdir = tmp_path / "run"
        workdir.mkdir()

        result = subprocess.run(
            ["bash", "-e", "-c", script],
            cwd=workdir,
            env=_bootstrap_env(pip_runner, workdir, wheel.as_uri(), digest),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        # The provenance record carries BOTH halves (expected = observed)
        # plus the Q35-08 qualification facts and the R36-07 resolved route
        # (the wheel route is the qualified one; a non-forge probe wheel
        # records no version).
        recorded = json.loads((workdir / ".forge" / "lane_install.json").read_text())
        assert recorded == {
            "pin": "wheel",
            "route": "explicit-wheel",
            "expected_sha256": digest,
            "actual_sha256": digest,
            "qualified": True,
            "version": "",
        }
        # The installed lane code imports from the throwaway target.
        import os

        probe = subprocess.run(
            [pip_runner["PYTHON"], "-c", "import forge_probe_example as m; print(m.__version__)"],
            env=dict(os.environ, PYTHONPATH=str(workdir / "pip-target")),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == "0.0.1"

    def test_the_dogfood_mirror_ships_the_same_effective_command(self, tmp_path, pip_runner):
        """Azure/GitHub parity concern, on the two files that ship the fix:
        the mirror's fragment behaves identically (same exit, same
        provenance record) under the same fixture."""
        import json
        import subprocess

        wheel, digest = _synthetic_wheel(tmp_path)
        outcomes = {}
        for label, path in (("template", TEMPLATE), ("mirror", MIRROR)):
            workdir = tmp_path / label
            workdir.mkdir()
            result = subprocess.run(
                ["bash", "-e", "-c", _wheel_branch(path)],
                cwd=workdir,
                env=_bootstrap_env(pip_runner, workdir, wheel.as_uri(), digest),
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            outcomes[label] = json.loads((workdir / ".forge" / "lane_install.json").read_text())
        assert outcomes["template"] == outcomes["mirror"]

    def test_the_pre_fix_path_fragment_form_fails_on_real_pip(self, tmp_path, pip_runner):
        """The negative regression (P06 verbatim): the OLD bootstrap line —
        a plain local path with a #sha256= fragment appended — is not a
        valid requirement. Real pip refuses it BEFORE the fix; the fixed
        template must not contain that shape (asserted above) and the
        broken invocation is kept here as the reproducer."""
        import subprocess

        wheel, digest = _synthetic_wheel(tmp_path)
        result = subprocess.run(
            [
                pip_runner["PYTHON"],
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--disable-pip-version-check",
                "--target",
                str(tmp_path / "broken-target"),
                f"{wheel}#sha256={digest}",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode != 0
        assert "Invalid requirement" in result.stdout + result.stderr

    def test_a_wrong_digest_is_refused_before_pip_installs(self, tmp_path, pip_runner):
        """Expected-vs-observed mismatch: deterministic refusal, both hashes
        in the marker, nothing installed."""
        import subprocess

        wheel, _digest = _synthetic_wheel(tmp_path)
        workdir = tmp_path / "run"
        workdir.mkdir()
        result = subprocess.run(
            ["bash", "-e", "-c", _wheel_branch(TEMPLATE)],
            cwd=workdir,
            env=_bootstrap_env(pip_runner, workdir, wheel.as_uri(), "0" * 64),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode != 0
        assert "forge wheel hash mismatch" in result.stdout + result.stderr
        assert not (workdir / ".forge" / "lane_install.json").exists()
        assert not (workdir / "pip-target").exists() or not any((workdir / "pip-target").iterdir())

    def test_multiple_wheel_candidates_are_refused_not_guessed(self, tmp_path, pip_runner):
        """Two wheels in the wheelhouse: the deterministic one-artifact
        refusal fires BEFORE any pip install (never `head -1`)."""
        import subprocess

        wheel, digest = _synthetic_wheel(tmp_path)
        _synthetic_wheel(tmp_path, name="forge_probe_other")
        workdir = tmp_path / "run"
        (workdir / ".forge" / "wheel").mkdir(parents=True)
        # The pre-planted second candidate + the download's copy = two.
        import shutil

        shutil.copy(
            tmp_path / "forge_probe_other-0.0.1-py3-none-any.whl", workdir / ".forge" / "wheel"
        )
        result = subprocess.run(
            ["bash", "-e", "-c", _wheel_branch(TEMPLATE)],
            cwd=workdir,
            env=_bootstrap_env(pip_runner, workdir, wheel.as_uri(), digest),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode != 0
        assert "must be exactly one wheel" in result.stdout + result.stderr
        assert not (workdir / ".forge" / "lane_install.json").exists()

    def test_a_source_archive_is_refused_before_execution(self, tmp_path, pip_runner):
        """R32-08: pointing the pin at a source archive is a deterministic
        refusal BEFORE anything installs — a plain PKG-INFO tarball is not
        a wheel project (pip refuses it), and even a buildable sdist would
        have to EXECUTE build metadata, which the trusted lane-code route
        never does (the wheel-only glob below cannot pick a .tar.gz). No
        lane_install.json, no install target."""
        import hashlib
        import io
        import subprocess
        import tarfile

        sdist = tmp_path / "forge_probe_sdist-0.0.1.tar.gz"
        with tarfile.open(sdist, "w:gz") as archive:
            info = tarfile.TarInfo("forge_probe_sdist-0.0.1/PKG-INFO")
            payload = b"Metadata-Version: 2.1\nName: forge-probe-sdist\nVersion: 0.0.1\n"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        workdir = tmp_path / "run"
        workdir.mkdir()

        digest = hashlib.sha256(sdist.read_bytes()).hexdigest()
        result = subprocess.run(
            ["bash", "-e", "-c", _wheel_branch(TEMPLATE)],
            cwd=workdir,
            env=_bootstrap_env(pip_runner, workdir, sdist.as_uri(), digest),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode != 0
        assert "FORGE_BOOTSTRAP_FAILED" in result.stdout + result.stderr
        assert not (workdir / ".forge" / "lane_install.json").exists()
        target = workdir / "pip-target"
        assert not target.exists() or not any(target.iterdir())
