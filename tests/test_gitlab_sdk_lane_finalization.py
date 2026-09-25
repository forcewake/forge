"""R38-01 (#302): the GitLab SDK lanes' finalization — the ACTUAL packaged
shell, executed.

The shipped defect: the SDK lanes' script block ended with an
unconditional driver-rc exit — on a SUCCESSFUL driver the job exited 0
BEFORE the meta floor, the collection and the machine-readable markers
ever ran (GitLab concatenates ``before_script`` + ``script`` into ONE
shell, so the early exit killed the whole job; the live single-writer run
hit ``harness_artifact_missing`` and needed a manual guarded-exit patch on
the disposable target). Even past the exit, collection staged the ORIGINAL
checkout inline instead of the packaged generation-aware collector — a
resumed agent's work-in-progress sat in the sibling generation while the
uploaded candidate was 0 bytes.

The fix under test: driver / collection / final-status phases in ONE
shell, collection through
``python -m forge.harness_entry --collect-candidate`` (ownership-validated
generation, ``git -C`` on the validated tree, typed errors), the candidate
advertised only when BOTH the driver and the collector succeeded, and a
single guarded exit at the end.

These tests extract the finalization block VERBATIM from each shipped SDK
template (YAML-parsed — never a handwritten copy that could omit the exit)
and run it under REAL Bash in a REAL Git checkout: the DRIVER leg is a
stub interpreter on the ``FORGE_LANE_PYTHON`` seam the template legitimately
declares (a fake turn — no vendor session), while the COLLECTION leg runs
the REAL packaged collector from this repository's environment. Native
job outcome (the bash exit code) and artifact presence are asserted
independently, per the review's negative-test matrix:

- driver rc=0 / rc=7 × collector success / failure;
- a restored generation: the diff carries the GENERATION's changes, the
  original checkout is never collected accidentally;
- a required-resume dispatch without a pointer: zero publication, never a
  checkout fallback;
- stale bytes from a prior attempt never become the new candidate.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from tests.test_candidate_collector import (
    CHECKPOINT_ID,
    WORK_ID,
    make_checkout,
    make_generation,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "ci" / "templates"

#: Every shipped SDK-lane recipe — the same generated fragment must hold
#: on all four (the batch recipes keep their inline one-shot contract and
#: are documented as restore-incapable; see tests/test_templates.py).
SDK_LANE_TEMPLATES = (
    "claude-sdk-lane.gitlab-ci.yml",
    "codex-sdk-lane.gitlab-ci.yml",
    "copilot-sdk-lane.gitlab-ci.yml",
    "opencode-sdk-lane.gitlab-ci.yml",
)

#: A distinctive byte pattern standing in for a PRIOR attempt's candidate —
#: if it ever re-appears in the uploaded artifact, stale bytes won.
STALE_BYTES = b"STALE PRIOR ATTEMPT CANDIDATE BYTES\n"


def finalization_block(template: Path) -> str:
    """The finalization script block, VERBATIM, from the shipped template.

    YAML-parsed (the block scalar yields the exact shell text GitLab
    concatenates into the job's one shell) — never a second handwritten
    implementation of the contract under test.
    """
    doc = yaml.safe_load(template.read_text())
    keys = [key for key in doc if key.startswith("forge-agent")]
    assert len(keys) == 1, f"expected exactly one forge-agent* job, got {keys}"
    blocks = [
        item
        for item in doc[keys[0]]["script"]
        if isinstance(item, str) and "forge.lane_driver" in item
    ]
    assert len(blocks) == 1, "the finalization must be ONE script block"
    return blocks[0]


def write_stub_lane_python(venv_root: Path) -> Path:
    """A stub interpreter on the template's ``FORGE_LANE_PYTHON`` seam.

    ``-m forge.lane_driver`` invocations are scripted from the test env
    (exit code, optional meta, optional workspace edit/delete — a fake
    turn with no vendor session); every OTHER invocation (the collector)
    delegates to this process's REAL interpreter, so the collection leg
    executes the actual packaged ``forge.harness_entry``.

    Exposed as a plain function (R38-16 / #317): the conformance gate
    (``scripts/gate_conformance.py``) reuses THIS stub — the same fake
    driver/fake collector seam, never a second handwritten copy.
    """
    stub = venv_root / "bin" / "python"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            # forge packaged-shell regression stub (R38-01): the DRIVER leg
            # is scripted from the test environment; everything else (the
            # collector) runs on the real interpreter below.
            if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "forge.lane_driver" ]; then
              if [ -n "${{FORGE_STUB_DRIVER_META:-}}" ]; then
                printf '%s\\n' "$FORGE_STUB_DRIVER_META" > .forge/candidate.meta.json
              fi
              if [ -n "${{FORGE_STUB_DRIVER_EDIT:-}}" ]; then
                printf '%s\\n' "${{FORGE_STUB_DRIVER_CONTENT:-agent edit}}" > "$FORGE_STUB_DRIVER_EDIT"
              fi
              if [ -n "${{FORGE_STUB_DRIVER_DELETE:-}}" ]; then
                rm -f "$FORGE_STUB_DRIVER_DELETE"
              fi
              exit "${{FORGE_STUB_DRIVER_RC:-0}}"
            fi
            exec "{sys.executable}" "$@"
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


@pytest.fixture(scope="session")
def stub_lane_python(tmp_path_factory) -> Path:
    return write_stub_lane_python(tmp_path_factory.mktemp("forge-lane-venv"))


def lane_env(stub: Path, base_oid: str, **overrides: str) -> dict[str, str]:
    """The dispatched lane environment for one packaged-shell run.

    Ambient FORGE_* variables are stripped (only what the dispatch
    envelope actually carries may reach the shell), then the template's
    own contract variables are set explicitly.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("FORGE_")}
    env.update(
        FORGE_LANE_PYTHON=str(stub),
        FORGE_RUN_ID=WORK_ID,
        FORGE_ATTEMPT_BASE=base_oid,
        FORGE_PLAN="stub brief",
        FORGE_LANE_RESUME="",
        FORGE_RESUME_CHECKPOINT="",
    )
    env.update(overrides)
    return env


def run_lane(
    template: Path, checkout: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the EXTRACTED finalization block as the job's one shell.

    The block ends with its own guarded ``exit``, so the bash exit code IS
    the native GitLab job outcome — asserted independently from artifact
    presence by the callers.
    """
    return subprocess.run(
        ["bash", "-c", finalization_block(template)],
        cwd=str(checkout),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def outcome(stdout: str) -> dict:
    """The parsed FORGE_LANE_OUTCOME marker (the observability contract:
    driver_exit / collector_exit / candidate_state as separate fields)."""
    line = next(part for part in stdout.splitlines() if part.startswith("FORGE_LANE_OUTCOME:"))
    payload = stdout[stdout.index(line) + len("FORGE_LANE_OUTCOME:") :]
    return json.loads(payload.splitlines()[0])


def load_meta(checkout: Path) -> dict:
    return json.loads((checkout / ".forge" / "candidate.meta.json").read_text())


@pytest.fixture(params=SDK_LANE_TEMPLATES)
def template(request) -> Path:
    return TEMPLATES_DIR / request.param


class TestSdkLaneFinalizationShell:
    """The executed matrix: driver rc × collector outcome → job outcome,
    candidate state and artifact presence."""

    def test_driver_success_collects_and_advertises_the_candidate(
        self, tmp_path: Path, stub_lane_python: Path, template: Path
    ):
        """rc=0 + collector success → job exit 0, the candidate marker AND
        the diff present, the driver's REAL meta preserved (not clobbered
        by the defensive floor)."""
        checkout, base_oid = make_checkout(tmp_path)
        real_meta = json.dumps(
            {
                "attempt_base": base_oid,
                "driver": "stub",
                "exit": "completed",
                "terminal_reason": "stub_real_meta",
            }
        )
        result = run_lane(
            template,
            checkout,
            lane_env(
                stub_lane_python,
                base_oid,
                FORGE_STUB_DRIVER_RC="0",
                FORGE_STUB_DRIVER_META=real_meta,
                FORGE_STUB_DRIVER_EDIT="src/app.py",
                FORGE_STUB_DRIVER_CONTENT="print('agent edit')",
            ),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # The candidate diff exists and carries exactly the driver's edit.
        diff = (checkout / ".forge" / "candidate.diff").read_text()
        assert "src/app.py" in diff and "+print('agent edit')" in diff
        # The machine-readable markers name the artifact and the base.
        assert f'FORGE_CANDIDATE:{{"attempt_base": "{base_oid}"' in result.stdout
        assert '"artifact": "candidate.diff", "exit": "completed"' in result.stdout
        assert "FORGE_RESULT:" in result.stdout
        # Separate observability fields, never collapsed.
        assert outcome(result.stdout) == {
            "driver_exit": "completed",
            "collector_exit": 0,
            "candidate_state": "candidate",
        }
        # The collector's own JSON carries the digest + generation fields.
        assert '"diff_digest"' in result.stdout
        assert '"source": "checkout"' in result.stdout
        # The REAL meta survives — the floor only writes when absent.
        assert load_meta(checkout)["terminal_reason"] == "stub_real_meta"

    def test_green_driver_with_failed_collector_fails_the_job(
        self, tmp_path: Path, stub_lane_python: Path, template: Path
    ):
        """rc=0 + collector failure → the JOB fails with the collector's
        rc (a green turn without its artifact is NOT a success), the meta
        is retained, NOTHING is advertised, no diff is uploaded."""
        checkout, base_oid = make_checkout(tmp_path)
        # A REAL collector failure: the pointer names a FOREIGN work id —
        # the ownership validation refuses, zero artifacts.
        make_generation(checkout, work_id="someone-elses-run")
        result = run_lane(
            template,
            checkout,
            lane_env(stub_lane_python, base_oid, FORGE_STUB_DRIVER_RC="0"),
        )
        assert result.returncode == 1  # the collector's rc fails the job
        assert "FORGE_CANDIDATE:" not in result.stdout
        assert "FORGE_RESULT:" not in result.stdout
        assert not (checkout / ".forge" / "candidate.diff").exists()
        assert outcome(result.stdout) == {
            "driver_exit": "completed",
            "collector_exit": 1,
            "candidate_state": "collection_failed",
        }
        # Driver-failure metadata retained even though collection failed.
        meta = load_meta(checkout)
        assert meta["attempt_base"] == base_oid
        assert meta["exit"] == "completed"

    def test_driver_rc7_fails_the_job_with_diagnostics_retained(
        self, tmp_path: Path, stub_lane_python: Path, template: Path
    ):
        """rc=7 → the job fails with EXACTLY rc=7 (the first meaningful
        failure wins), the meta floor writes an honest failed meta,
        collection still runs, and the marker is never a VALID candidate
        (exit=failed)."""
        checkout, base_oid = make_checkout(tmp_path)
        result = run_lane(
            template,
            checkout,
            lane_env(
                stub_lane_python,
                base_oid,
                FORGE_STUB_DRIVER_RC="7",  # killed mid-turn, after an edit
                FORGE_STUB_DRIVER_EDIT="src/app.py",
            ),
        )
        assert result.returncode == 7
        # The floor meta is honest about the crash.
        meta = load_meta(checkout)
        assert meta["exit"] == "failed"
        assert meta["terminal_reason"] == "lane_driver_no_meta"
        assert meta["attempt_base"] == base_oid
        # Collection still ran (diagnostics): the diff carries the partial
        # work but the marker says exit=failed — never advertised as valid.
        diff = (checkout / ".forge" / "candidate.diff").read_text()
        assert "src/app.py" in diff
        assert '"exit": "failed"' in result.stdout
        assert outcome(result.stdout) == {
            "driver_exit": "failed",
            "collector_exit": 0,
            "candidate_state": "driver_failed",
        }

    def test_green_driver_zero_change_is_a_pass_with_a_distinct_state(
        self, tmp_path: Path, stub_lane_python: Path, template: Path
    ):
        """rc=0 + collector zero_change → the job stays GREEN (a no-op
        turn is classified at the run level) and candidate_state says
        zero_change — distinguishable from candidate and from failure."""
        checkout, base_oid = make_checkout(tmp_path)
        result = run_lane(
            template,
            checkout,
            lane_env(stub_lane_python, base_oid, FORGE_STUB_DRIVER_RC="0"),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        diff = checkout / ".forge" / "candidate.diff"
        assert diff.exists() and diff.read_bytes() == b""  # an honest empty diff
        assert outcome(result.stdout) == {
            "driver_exit": "completed",
            "collector_exit": 0,
            "candidate_state": "zero_change",
        }
        assert '"zero_change": true' in result.stdout  # the collector agrees
        assert load_meta(checkout)["exit"] == "completed"  # the floor ran

    def test_a_restored_generation_ships_as_the_candidate(
        self, tmp_path: Path, stub_lane_python: Path, template: Path
    ):
        """A required-resume dispatch: the agent worked in the SIBLING
        generation (modified + new + deleted files) — the diff carries
        exactly those changes and the ORIGINAL checkout is untouched."""
        checkout, base_oid = make_checkout(tmp_path)
        generation = make_generation(checkout)
        (generation / "src" / "app.py").write_text("print('restored wip')\n")
        (generation / "notes").mkdir()
        (generation / "notes" / "new-file.md").write_text("agent edit\n")
        (generation / "run.sh").unlink()  # a deleted file, not just edits
        result = run_lane(
            template,
            checkout,
            lane_env(
                stub_lane_python,
                base_oid,
                FORGE_LANE_RESUME="1",
                FORGE_RESUME_CHECKPOINT=CHECKPOINT_ID,
                FORGE_STUB_DRIVER_RC="0",
            ),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        diff = (checkout / ".forge" / "candidate.diff").read_text()
        assert "src/app.py" in diff and "+print('restored wip')" in diff
        assert "notes/new-file.md" in diff and "+agent edit" in diff
        assert "-#!/bin/sh" in diff  # the deletion rides the diff
        # The collector picked the GENERATION, not the checkout.
        assert '"source": "generation"' in result.stdout
        assert generation.name in result.stdout  # generation_path names it
        assert outcome(result.stdout)["candidate_state"] == "candidate"
        # The original checkout is never collected accidentally.
        assert (checkout / "src" / "app.py").read_text() == "print('base')\n"
        assert (checkout / "run.sh").exists()

    def test_required_resume_without_a_pointer_publishes_nothing(
        self, tmp_path: Path, stub_lane_python: Path, template: Path
    ):
        """The ownership-removed generation (no pointer at all) on a
        required-resume dispatch: zero publication, no checkout fallback —
        the job fails on the collector's typed refusal."""
        checkout, base_oid = make_checkout(tmp_path)
        result = run_lane(
            template,
            checkout,
            lane_env(
                stub_lane_python,
                base_oid,
                FORGE_LANE_RESUME="1",
                FORGE_RESUME_CHECKPOINT=CHECKPOINT_ID,
                FORGE_STUB_DRIVER_RC="0",
            ),
        )
        assert result.returncode == 1
        assert not (checkout / ".forge" / "candidate.diff").exists()
        assert "FORGE_CANDIDATE:" not in result.stdout
        assert outcome(result.stdout)["candidate_state"] == "collection_failed"

    def test_stale_prior_attempt_bytes_never_become_the_candidate(
        self, tmp_path: Path, stub_lane_python: Path, template: Path
    ):
        """The driver is killed before its metadata with a PRIOR attempt's
        candidate bytes already sitting at both upload locations. A
        SUCCESSFUL collection overwrites them with THIS attempt's bytes;
        a FAILED collection removes them — stale bytes are never uploaded
        as the new candidate."""
        checkout, base_oid = make_checkout(tmp_path)
        (checkout / ".forge").mkdir()  # the lane's before_script shape
        (checkout / ".forge" / "candidate.diff").write_bytes(STALE_BYTES)
        (checkout / "forge-output").mkdir()
        (checkout / "forge-output" / "candidate.diff").write_bytes(STALE_BYTES)
        # (a) collection succeeds → the stale bytes are REPLACED by this
        # attempt's honest (here: empty) diff.
        result = run_lane(
            template,
            checkout,
            lane_env(stub_lane_python, base_oid, FORGE_STUB_DRIVER_RC="7"),
        )
        assert result.returncode == 7
        fresh = (checkout / ".forge" / "candidate.diff").read_bytes()
        assert fresh == b"" and fresh != STALE_BYTES
        assert STALE_BYTES not in (checkout / "forge-output" / "candidate.diff").read_bytes()
        # (b) collection fails → NOTHING sits at the upload location.
        checkout_b, base_b = make_checkout(tmp_path / "case-b")
        (checkout_b / ".forge").mkdir()
        make_generation(checkout_b, work_id="someone-elses-run")  # foreign pointer
        (checkout_b / ".forge" / "candidate.diff").write_bytes(STALE_BYTES)
        result_b = run_lane(
            template,
            checkout_b,
            lane_env(stub_lane_python, base_b, FORGE_STUB_DRIVER_RC="7"),
        )
        assert result_b.returncode == 7
        assert not (checkout_b / ".forge" / "candidate.diff").exists()

    def test_the_extracted_block_is_the_shipped_one(self, template: Path):
        """The regression cannot rot into a copy: the block carries the
        phased contract markers and exits exactly once, at the end."""
        block = finalization_block(template)
        assert 'exit "$_job_rc"' in block
        assert block.count('exit "') == 1
        assert block.rstrip().endswith('exit "$_job_rc"')
        bash = subprocess.run(
            ["bash", "-n"], input=block.encode(), capture_output=True, check=False
        )
        assert bash.returncode == 0, bash.stderr.decode(errors="replace")
